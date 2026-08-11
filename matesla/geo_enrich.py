"""
Gentle geo enrichment for the capture cron.

- Elevation: Open-Meteo DEM (batch up to 100 grids) → AddressFromLatLong cache
  → denormalise onto TeslaCarDataSnapshot.elevation (only where NULL).
- Address: Nominatim (existing quota) for high-value grids (parked / endpoints),
  never every drive sample.

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
    AddressFromLatLong,
    GetAddressFromLatLong,
    LookupCachedAddress,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

logger = logging.getLogger(__name__)

GEO_DECIMALS = 4
# Half-step of round(4) for range propagation on raw snapshot coords.
_GRID_EPS = 0.5 * (10 ** (-GEO_DECIMALS))  # 0.00005

OPEN_METEO_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OPEN_METEO_TIMEOUT_SEC = 12

# Per capture tick (override in settings if needed).
DEFAULT_ELEV_BATCH = 100
DEFAULT_ELEV_SCAN = 2500
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
        # Refresh stamp when we re-fetch or first-set
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


def propagate_elevation_to_snapshots(lat4: float, lon4: float, elev_m: float) -> int:
    """
    Set snapshot.elevation only where NULL for GPS near this grid cell.
    Does not overwrite TeslaFi / prior values.
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


def _collect_grids_missing_elevation(max_grids: int) -> list[tuple[float, float]]:
    """
    Distinct round4 grids from recent snapshots lacking elevation.
    Ordered by recency of first sighting in the scan window.
    """
    scan = _elev_scan_limit()
    rows = (
        TeslaCarDataSnapshot.objects.filter(
            elevation__isnull=True,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .order_by("-Date")
        .values_list("latitude", "longitude")[:scan]
    )
    grids: OrderedDict[tuple[float, float], None] = OrderedDict()
    for lat, lon in rows:
        try:
            key = round_grid(lat, lon)
        except (TypeError, ValueError):
            continue
        if key in grids:
            continue
        # Skip grids already cached with elevation (snapshot lag before propagate)
        if (
            AddressFromLatLong.objects.filter(
                latitude=key[0], longitude=key[1], elevation__isnull=False
            ).exists()
        ):
            # Propagate stale snapshots for this known elev and continue
            elev = lookup_cached_elevation(key[0], key[1])
            if elev is not None:
                propagate_elevation_to_snapshots(key[0], key[1], elev)
            continue
        grids[key] = None
        if len(grids) >= max_grids:
            break
    return list(grids.keys())


def enrich_elevations_once(
    batch_size: int | None = None,
    session: requests.Session | None = None,
) -> dict:
    """One Open-Meteo batch + cache upsert + snapshot propagate."""
    limit = batch_size if batch_size is not None else _elev_batch_size()
    limit = max(1, min(100, limit))
    stats = {
        "elev_grids_requested": 0,
        "elev_grids_filled": 0,
        "elev_snapshots_updated": 0,
        "elev_http_ok": False,
    }
    grids = _collect_grids_missing_elevation(limit)
    if not grids:
        return stats

    stats["elev_grids_requested"] = len(grids)
    elevs = fetch_open_meteo_elevations(grids, session=session)
    if any(e is not None for e in elevs):
        stats["elev_http_ok"] = True

    for (lat4, lon4), elev in zip(grids, elevs):
        if elev is None:
            continue
        upsert_grid_elevation(lat4, lon4, elev)
        n = propagate_elevation_to_snapshots(lat4, lon4, elev)
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

    # Also elev-only cache rows (created by DEM batch) still missing address
    if len(grids) < max_candidates:
        for row in (
            AddressFromLatLong.objects.filter(
                Q(address="") | Q(address__isnull=True)
            )
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
    Fill a few missing addresses via Nominatim (existing daily quota + interval).
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
            result = GetAddressFromLatLong(lat4, lon4)
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
