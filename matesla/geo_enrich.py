"""
Gentle geo enrichment for the capture cron.

- Elevation: Open-Meteo DEM (batch up to 100 grids) → AddressFromLatLong cache
  → denormalise onto TeslaCarDataSnapshot.elevation (only where NULL).
- Address: Nominatim (existing quota) for high-value grids (parked / endpoints),
  never every drive sample.

Performance:
- If no null-elev GPS rows remain → immediate noop (no HTTP, no UPDATE).
- Otherwise one scan of null-elev rows, group by grid, UPDATE by primary key
  only (never re-SCAN the whole table by lat/lon range).
- Aggressive: large scan window + all scanned ids updated; Open-Meteo still
  capped at 100 unknown grids per tick.

Does not call Tesla Fleet. Safe to run every minute after capture.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import timedelta

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from matesla.models.AddressFromLatLong import (
    NOMINATIM_PURPOSE_BACKFILL,
    AddressFromLatLong,
    GetAddressFromLatLong,
    LookupCachedAddress,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

logger = logging.getLogger(__name__)

GEO_DECIMALS = 4
# Half-step of round(4) — used only by range helper / tests.
_GRID_EPS = 0.5 * (10 ** (-GEO_DECIMALS))  # 0.00005

OPEN_METEO_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OPEN_METEO_TIMEOUT_SEC = 12

# Per capture tick (override in settings if needed).
DEFAULT_ELEV_BATCH = 100  # Open-Meteo max coords / request
# How many null-elev snapshot rows to load per tick (aggressive backlog drain).
DEFAULT_ELEV_SCAN = 20000
# SQLite variable limit comfort for WHERE id IN (...)
_ID_UPDATE_CHUNK = 500

DEFAULT_ADDR_PER_TICK = 1
DEFAULT_ADDR_CANDIDATE_SCAN = 400
DEFAULT_ADDR_LOOKBACK_DAYS = 30


def round_grid(lat, lon) -> tuple[float, float]:
    return round(float(lat), GEO_DECIMALS), round(float(lon), GEO_DECIMALS)


def _elev_batch_size() -> int:
    return int(getattr(settings, "GEO_ELEV_BATCH_SIZE", DEFAULT_ELEV_BATCH))


def _elev_scan_limit() -> int:
    return int(getattr(settings, "GEO_ELEV_SCAN_LIMIT", DEFAULT_ELEV_SCAN))


def _addr_per_tick() -> int:
    return int(getattr(settings, "GEO_ADDR_PER_TICK", DEFAULT_ADDR_PER_TICK))


def lookup_cached_elevation(lat, lon) -> float | None:
    """Return elevation_m from the grid cache, or None."""
    try:
        lat4, lon4 = round_grid(lat, lon)
    except (TypeError, ValueError):
        return None
    row = (
        AddressFromLatLong.objects.filter(latitude=lat4, longitude=lon4)
        .only("elevation")
        .first()
    )
    if row is None or row.elevation is None:
        return None
    return float(row.elevation)


def apply_cached_elevation_to_snapshot(snap) -> bool:
    """
    If snap has GPS but no elevation, copy from geo cache when known.
    Mutates snap in memory only (caller saves). Returns True if set.
    """
    if getattr(snap, "elevation", None) is not None:
        return False
    lat = getattr(snap, "latitude", None)
    lon = getattr(snap, "longitude", None)
    if lat is None or lon is None:
        return False
    elev = lookup_cached_elevation(lat, lon)
    if elev is None:
        return False
    snap.elevation = elev
    return True


def upsert_grid_elevation(lat4: float, lon4: float, elev_m: float) -> AddressFromLatLong:
    """Create or update cache elevation; never clear an existing address."""
    now = timezone.now()
    row, created = AddressFromLatLong.objects.get_or_create(
        latitude=lat4,
        longitude=lon4,
        defaults={
            "address": "",
            "date": now.date(),
            "elevation": elev_m,
            "elevation_fetched_at": now,
        },
    )
    if not created:
        fields = []
        if row.elevation is None:
            row.elevation = elev_m
            fields.append("elevation")
        if row.elevation_fetched_at is None or "elevation" in fields:
            row.elevation_fetched_at = now
            fields.append("elevation_fetched_at")
        if fields:
            row.save(update_fields=fields)
    return row


def fetch_open_meteo_elevations(
    coords: list[tuple[float, float]],
    session: requests.Session | None = None,
) -> list[float | None]:
    """
    Batch DEM lookup. coords = [(lat, lon), ...] max ~100.
    Returns parallel list of float metres or None on failure per point.
    """
    if not coords:
        return []
    if len(coords) > 100:
        raise ValueError("Open-Meteo elevation accepts at most 100 coordinates")

    lats = ",".join(f"{lat:.5f}" for lat, _ in coords)
    lons = ",".join(f"{lon:.5f}" for _, lon in coords)
    http = session or requests
    try:
        resp = http.get(
            OPEN_METEO_ELEVATION_URL,
            params={"latitude": lats, "longitude": lons},
            timeout=OPEN_METEO_TIMEOUT_SEC,
            headers={"User-Agent": "matesla-personal-tesla-stats/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Open-Meteo elevation request failed: %s", exc)
        return [None] * len(coords)

    elevs = data.get("elevation")
    if not isinstance(elevs, list) or len(elevs) != len(coords):
        logger.warning("Open-Meteo elevation unexpected payload: %r", data)
        return [None] * len(coords)

    out: list[float | None] = []
    for value in elevs:
        if value is None:
            out.append(None)
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(None)
    return out


def propagate_elevation_to_ids(ids: list[int], elev_m: float) -> int:
    """
    Set elevation on explicit snapshot PKs only (fast path).
    Does not overwrite non-null elevation. Chunks id lists for SQLite.
    """
    if not ids:
        return 0
    total = 0
    for i in range(0, len(ids), _ID_UPDATE_CHUNK):
        chunk = ids[i : i + _ID_UPDATE_CHUNK]
        total += TeslaCarDataSnapshot.objects.filter(
            id__in=chunk, elevation__isnull=True
        ).update(elevation=elev_m)
    return total


def propagate_elevation_to_snapshots(lat4: float, lon4: float, elev_m: float) -> int:
    """
    Set snapshot.elevation where NULL near this grid cell.

    Uses lat/lon range (helped by index on latitude, longitude). Prefer
    ``propagate_elevation_to_ids`` from the enrich tick.
    """
    return TeslaCarDataSnapshot.objects.filter(
        elevation__isnull=True,
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__gte=lat4 - _GRID_EPS,
        latitude__lt=lat4 + _GRID_EPS,
        longitude__gte=lon4 - _GRID_EPS,
        longitude__lt=lon4 + _GRID_EPS,
    ).update(elevation=elev_m)


def _cached_elev_map(
    keys: list[tuple[float, float]],
) -> dict[tuple[float, float], float]:
    """Bulk-load known elevations for a set of grid keys."""
    if not keys:
        return {}
    lats = {k[0] for k in keys}
    lons = {k[1] for k in keys}
    out: dict[tuple[float, float], float] = {}
    for lat, lon, elev in AddressFromLatLong.objects.filter(
        latitude__in=lats,
        longitude__in=lons,
        elevation__isnull=False,
    ).values_list("latitude", "longitude", "elevation"):
        out[(float(lat), float(lon))] = float(elev)
    return out


def enrich_elevations_once(
    batch_size: int | None = None,
    session: requests.Session | None = None,
) -> dict:
    """
    One tick of elevation backfill.

    1. If no null-elev GPS rows → stop immediately.
    2. Scan up to GEO_ELEV_SCAN_LIMIT null-elev rows (id, lat, lon).
    3. Group ids by round4 grid; apply cache hits via PK update.
    4. Open-Meteo for up to 100 unknown grids; cache + PK update those ids.
    """
    limit = batch_size if batch_size is not None else _elev_batch_size()
    limit = max(1, min(100, limit))
    stats = {
        "elev_grids_requested": 0,
        "elev_grids_filled": 0,
        "elev_snapshots_updated": 0,
        "elev_http_ok": False,
        "elev_noop": False,
    }

    null_qs = TeslaCarDataSnapshot.objects.filter(
        elevation__isnull=True,
        latitude__isnull=False,
        longitude__isnull=False,
    )
    # Cheap exit: no backlog → do not ORDER BY / fetch thousands of rows.
    if not null_qs.exists():
        stats["elev_noop"] = True
        return stats

    scan = _elev_scan_limit()
    rows = list(
        null_qs.order_by("-Date").values_list("id", "latitude", "longitude")[:scan]
    )
    if not rows:
        stats["elev_noop"] = True
        return stats

    # grid → [snapshot ids] (newest first within each grid)
    ids_by_grid: OrderedDict[tuple[float, float], list[int]] = OrderedDict()
    for sid, lat, lon in rows:
        try:
            key = round_grid(lat, lon)
        except (TypeError, ValueError):
            continue
        ids_by_grid.setdefault(key, []).append(int(sid))

    if not ids_by_grid:
        stats["elev_noop"] = True
        return stats

    keys = list(ids_by_grid.keys())
    cached = _cached_elev_map(keys)

    # 1) Grids already in cache → update only the scanned ids (no full-table work)
    for key, elev in cached.items():
        ids = ids_by_grid.get(key) or []
        if not ids:
            continue
        stats["elev_snapshots_updated"] += propagate_elevation_to_ids(ids, elev)

    # 2) Unknown grids → Open-Meteo (aggressive batch up to 100)
    need_http = [k for k in keys if k not in cached][:limit]
    if not need_http:
        return stats

    stats["elev_grids_requested"] = len(need_http)
    elevs = fetch_open_meteo_elevations(need_http, session=session)
    if any(e is not None for e in elevs):
        stats["elev_http_ok"] = True

    for key, elev in zip(need_http, elevs):
        if elev is None:
            continue
        lat4, lon4 = key
        upsert_grid_elevation(lat4, lon4, elev)
        n = propagate_elevation_to_ids(ids_by_grid.get(key) or [], elev)
        stats["elev_grids_filled"] += 1
        stats["elev_snapshots_updated"] += n
    return stats


def _is_parked_sample(shift_state, speed) -> bool:
    if shift_state in (None, "", "P"):
        if speed is None or float(speed) < 1.0:
            return True
    return False


def _collect_grids_missing_address(max_candidates: int) -> list[tuple[float, float]]:
    """
    High-value grids without a usable address: recent parked / low-speed GPS
    (typical trip endpoints), not mid-drive highway points.
    """
    lookback = int(
        getattr(settings, "GEO_ADDR_LOOKBACK_DAYS", DEFAULT_ADDR_LOOKBACK_DAYS)
    )
    scan = int(
        getattr(settings, "GEO_ADDR_CANDIDATE_SCAN", DEFAULT_ADDR_CANDIDATE_SCAN)
    )
    since = timezone.now() - timedelta(days=lookback)
    rows = (
        TeslaCarDataSnapshot.objects.filter(
            Date__gte=since,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .filter(Q(shift_state__isnull=True) | Q(shift_state="P") | Q(shift_state=""))
        .order_by("-Date")
        .values_list("latitude", "longitude", "shift_state", "speed")[:scan]
    )
    grids: OrderedDict[tuple[float, float], None] = OrderedDict()
    for lat, lon, shift, speed in rows:
        if not _is_parked_sample(shift, speed):
            continue
        try:
            key = round_grid(lat, lon)
        except (TypeError, ValueError):
            continue
        if key in grids:
            continue
        if LookupCachedAddress(key[0], key[1]) is not None:
            continue
        grids[key] = None
        if len(grids) >= max_candidates:
            break

    if len(grids) < max_candidates:
        for row in (
            AddressFromLatLong.objects.filter(Q(address="") | Q(address__isnull=True))
            .exclude(elevation__isnull=True)
            .order_by("-elevation_fetched_at", "-id")[: max_candidates * 2]
        ):
            key = (float(row.latitude), float(row.longitude))
            if LookupCachedAddress(key[0], key[1]) is not None:
                continue
            grids.setdefault(key, None)
            if len(grids) >= max_candidates:
                break
    return list(grids.keys())


def enrich_addresses_once(max_calls: int | None = None) -> dict:
    """
    Fill a few missing addresses via Nominatim (backfill budget only).

    Uses purpose=backfill so capture cannot exhaust the hard daily cap that
    day-map ResolveAddress needs for on-demand reverse-geocode.
    """
    n = max_calls if max_calls is not None else _addr_per_tick()
    n = max(0, n)
    stats = {"addr_attempted": 0, "addr_resolved": 0}
    if n <= 0:
        return stats

    candidates = _collect_grids_missing_address(max_candidates=max(n * 3, n))
    for lat4, lon4 in candidates:
        if stats["addr_attempted"] >= n:
            break
        stats["addr_attempted"] += 1
        try:
            result = GetAddressFromLatLong(
                lat4, lon4, purpose=NOMINATIM_PURPOSE_BACKFILL
            )
        except Exception as exc:
            logger.warning("Address enrich failed for %s,%s: %s", lat4, lon4, exc)
            continue
        if result and result != "Unknown":
            stats["addr_resolved"] += 1
    return stats


def geo_enrichment_tick(
    elev_batch: int | None = None,
    addr_calls: int | None = None,
    session: requests.Session | None = None,
) -> dict:
    """
    One capture-cron slice: elevation batch then a few address lookups.
    Always safe: network errors are swallowed into stats / logs.
    """
    stats: dict = {}
    try:
        stats.update(enrich_elevations_once(batch_size=elev_batch, session=session))
    except Exception:
        logger.exception("Elevation enrichment tick failed")
        stats.setdefault("elev_grids_filled", 0)
        stats["elev_error"] = True
    try:
        stats.update(enrich_addresses_once(max_calls=addr_calls))
    except Exception:
        logger.exception("Address enrichment tick failed")
        stats.setdefault("addr_resolved", 0)
        stats["addr_error"] = True
    return stats
