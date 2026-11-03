"""
Maintained Atlante DC site coordinates (Belgium and nearby).

Source: OpenStreetMap charging stations tagged operator/brand/network Atlante
(ODbL). Belgium currently has no OSM Atlante sites; the list covers Hauts-de-France
and the rest of metropolitan France, Luxembourg and the Netherlands, plus sites
that match past household DC charges (Calais, Caen, Chanas, Grenoble).

A session is Atlante only when it is DC, within ATLANTE_MATCH_METERS of a site
here, and has no Tesla Supercharger invoice or Supercharger directory match.
"""

from __future__ import annotations

import math
from typing import Any

# Default GPS tolerance. MATESLA_ATLANTE_MATCH_METERS is clamped to this range.
ATLANTE_MATCH_METERS_DEFAULT = 300.0
ATLANTE_MATCH_METERS_MIN = 200.0
ATLANTE_MATCH_METERS_MAX = 500.0
ATLANTE_EUR_PER_KWH = 0.20

# (latitude, longitude, display name)
ATLANTE_SITES: tuple[tuple[float, float, str], ...] = (
    (51.015381, 2.381912, "Atlante"),
    (50.936571, 1.862955, "Atlante · Calais · Kiabi"),
    (50.731261, 2.277631, "Atlante · Arques · CCV Saint-Omer"),
    (50.726393, 1.645612, "Atlante · Boulogne-sur-Mer"),
    (50.724255, 2.747945, "Atlante"),
    (50.616614, 3.125385, "Atlante · Lille"),
    (50.378776, 3.474802, "Atlante · Petite-Forêt · Darty Valenciennes"),
    (50.283826, 3.970097, "Atlante · Maubeuge · Kia"),
    (50.176471, 3.206293, "Atlante"),
    (49.923375, 2.308698, "Atlante · Amiens · CCV Amiens"),
    (49.898201, 1.078268, "Atlante"),
    (49.865388, 2.378158, "Atlante · Glisy · CHAUSSEA"),
    (49.530532, 0.973515, "Atlante"),
    (49.529065, 0.972609, "Atlante · Barentin · Ixina Rouen"),
    (49.348791, 4.209005, "Atlante · Warmeriville"),
    (49.348383, 6.130719, "Atlante · Metz"),
    (49.240466, 2.467169, "Atlante"),
    (49.165325, -0.425455, "Atlante · Caen"),
    (49.108816, -1.133683, "Atlante"),
    (49.081240, 6.110942, "Atlante"),
    (48.803410, 3.089470, "Atlante"),
    (48.750215, 1.348574, "Atlante"),
    (48.624747, -1.344136, "Atlante"),
    (48.573877, -3.717242, "Atlante"),
    (48.554114, 3.286855, "Atlante"),
    (48.549436, -3.449309, "Atlante · Plounévez-Moëdec · L'Étape"),
    (48.549379, -3.110371, "Atlante"),
    (48.488150, 2.187060, "Atlante · Etréchy · Intermarché"),
    (48.127224, 7.364776, "Atlante"),
    (47.942968, 0.181268, "Atlante"),
    (47.672179, 3.876145, "Atlante"),
    (47.662774, -2.786640, "Atlante"),
    (47.631455, 6.140269, "Atlante · Vesoul · Cinéma Majestic"),
    (47.570458, 1.368817, "Atlante"),
    (47.487226, 6.840730, "Atlante · Audincourt"),
    (47.330820, 0.706246, "Atlante"),
    (47.306770, 4.814703, "Atlante"),
    (47.118605, 2.377440, "Atlante"),
    (47.117665, 2.376129, "Atlante"),
    (46.368314, 2.592897, "Atlante · Montluçon · Peugeot Autosphere"),
    (46.352649, 6.442323, "Atlante"),
    (46.255160, 5.059176, "Atlante"),
    (46.063770, 4.058316, "Atlante"),
    (45.781034, 3.125020, "Atlante"),
    (45.775278, 3.186176, "Atlante"),
    (45.747743, -0.676113, "Atlante"),
    (45.622227, 2.457976, "Atlante"),
    (
        45.609331,
        -0.599344,
        "Atlante · A10 · Saint-Léger Ouest",
    ),
    (45.522175, 3.258414, "Atlante"),
    (
        45.315872,
        4.810447,
        "Atlante · Chanas · Hôtel initial by Balladins",
    ),
    (45.146604, 5.691227, "Atlante · Grenoble"),
    (45.058073, 3.109270, "Atlante"),
    (44.880444, -0.629167, "Atlante"),
    (44.870361, 3.251429, "Atlante"),
    (44.610332, -1.129941, "Atlante"),
    (43.604841, 1.306025, "Atlante · Colomiers · Boulangerie Ange"),
    (43.592823, 2.215955, "Atlante"),
    (43.444756, 6.702098, "Atlante"),
    (43.395720, 5.135384, "Atlante"),
    (43.349501, 3.257411, "Atlante"),
    (43.167609, 2.987556, "Atlante"),
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(min(1.0, a)))


def atlante_match_meters() -> float:
    """GPS tolerance in metres, default 300, always in [200, 500]."""
    raw = ATLANTE_MATCH_METERS_DEFAULT
    try:
        from django.conf import settings

        raw = getattr(
            settings, "MATESLA_ATLANTE_MATCH_METERS", ATLANTE_MATCH_METERS_DEFAULT
        )
    except Exception:
        raw = ATLANTE_MATCH_METERS_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = ATLANTE_MATCH_METERS_DEFAULT
    return max(ATLANTE_MATCH_METERS_MIN, min(ATLANTE_MATCH_METERS_MAX, value))


def nearest_atlante(
    lat: float | None,
    lon: float | None,
    *,
    max_m: float | None = None,
) -> dict[str, Any] | None:
    """Closest Atlante site within ``max_m`` (default configured tolerance)."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    limit = atlante_match_meters() if max_m is None else float(max_m)
    best: dict[str, Any] | None = None
    best_m = limit
    for site_lat, site_lon, name in ATLANTE_SITES:
        dist = _haversine_m(lat_f, lon_f, site_lat, site_lon)
        if dist <= best_m:
            best_m = dist
            best = {
                "name": name,
                "lat": site_lat,
                "lon": site_lon,
                "distance_m": dist,
            }
    return best
