"""
Tesla Supercharger site matching (community directory).

Source: https://supercharge.info/service/supercharge/allSites
(public JSON, CORS-friendly; Tesla’s official findus API is not usable server-side).

Cached for many hours so day-map pages stay fast. Matching is opt-in / async —
never block the initial HTML render on a network fetch.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

SUPERCHARGE_INFO_URL = "https://supercharge.info/service/supercharge/allSites"
# Full directory is ~10k sites — refresh infrequently
CACHE_KEY = "matesla:superchargers:v1:all"
CACHE_SECONDS = 12 * 3600  # 12 h
# Match only if a Supercharger is essentially at the stop (not a nearby highway SC)
MATCH_MAX_METERS = 400.0
# HTTP budget when cache is cold (async endpoint only)
FETCH_TIMEOUT_S = 25


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def _normalize_sites(raw: list) -> list[dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or "").upper()
        # Skip closed / construction noise for matching
        if status and status not in ("OPEN", "EXPANDING", "CONSTRUCTION", "PERMIT", "PLAN"):
            # Still allow OPEN and near-open; skip CLOSED
            if status in ("CLOSED", "DECOMMISSIONED"):
                continue
        gps = row.get("gps") or {}
        try:
            lat = float(gps.get("latitude"))
            lon = float(gps.get("longitude"))
        except (TypeError, ValueError):
            continue
        site_id = row.get("id")
        location_id = (row.get("locationId") or "").strip() or None
        name = (row.get("name") or "").strip() or "Tesla Supercharger"
        try:
            power = row.get("powerKilowatt")
            power_kw = int(power) if power is not None else None
        except (TypeError, ValueError):
            power_kw = None
        try:
            stalls = int(row.get("stallCount") or 0) or None
        except (TypeError, ValueError):
            stalls = None
        sites.append(
            {
                "id": site_id,
                "location_id": location_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "power_kw": power_kw,
                "stalls": stalls,
                "status": status or None,
            }
        )
    return sites


def fetch_supercharger_sites(*, force: bool = False) -> list[dict[str, Any]]:
    """Return normalized Supercharger list (cached). Empty list on failure."""
    if not force:
        try:
            hit = cache.get(CACHE_KEY)
            if hit is not None:
                return hit
        except Exception:
            pass
    try:
        response = requests.get(
            SUPERCHARGE_INFO_URL,
            timeout=FETCH_TIMEOUT_S,
            headers={"Accept": "application/json", "User-Agent": "MaTesla/1.0"},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list):
            logger.warning("supercharge.info: unexpected payload type %s", type(raw))
            return []
        sites = _normalize_sites(raw)
        try:
            cache.set(CACHE_KEY, sites, CACHE_SECONDS)
        except Exception:
            pass
        logger.info("supercharge.info: cached %s sites", len(sites))
        return sites
    except Exception as exc:
        logger.warning("supercharge.info fetch failed: %s", exc)
        # Negative cache short — avoid hammering on outage
        try:
            cache.set(CACHE_KEY, [], 300)
        except Exception:
            pass
        return []


def nearest_supercharger(
    lat: float,
    lon: float,
    *,
    max_meters: float = MATCH_MAX_METERS,
) -> dict[str, Any] | None:
    """
    Closest Supercharger within max_meters, or None.

    Links prefer Tesla findus (works in browsers) with supercharge.info map fallback.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None

    sites = fetch_supercharger_sites()
    if not sites:
        return None

    best = None
    best_d = max_meters + 1.0
    for site in sites:
        # Cheap reject box ~0.01° ≈ 1 km before haversine
        if abs(site["lat"] - lat_f) > 0.02 or abs(site["lon"] - lon_f) > 0.03:
            continue
        dist = _haversine_m(lat_f, lon_f, site["lat"], site["lon"])
        if dist <= max_meters and dist < best_d:
            best_d = dist
            best = site

    if best is None:
        return None

    location_id = best.get("location_id")
    site_id = best.get("id")
    # Official Tesla location page (browser); community map as reliable fallback
    if location_id:
        tesla_url = (
            f"https://www.tesla.com/findus/location/supercharger/{location_id}"
        )
    else:
        tesla_url = None
    if site_id is not None:
        info_url = f"https://supercharge.info/map?siteId={site_id}"
    else:
        info_url = None

    return {
        "name": best["name"],
        "power_kw": best.get("power_kw"),
        "stalls": best.get("stalls"),
        "distance_m": round(best_d),
        "lat": best["lat"],
        "lon": best["lon"],
        "location_id": location_id,
        "site_id": site_id,
        # Prefer Tesla map when we have a locationId; else supercharge.info
        "url": tesla_url or info_url,
        "tesla_url": tesla_url,
        "info_url": info_url,
    }
