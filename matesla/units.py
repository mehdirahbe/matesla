"""
Distance / speed / energy-intensity display units.

Tesla Fleet API and our DB store distances in miles (odometer, battery_range,
active_route_miles_to_arrival, EPA catalog). Conversion happens only at the
presentation layer. Default display unit is kilometres.
"""

from __future__ import annotations

from typing import Any

# Exact international mile
MILES_TO_KM = 1.609344
KM_TO_MILES = 1.0 / MILES_TO_KM

UNIT_KM = "km"
UNIT_MI = "mi"
VALID_UNITS = frozenset({UNIT_KM, UNIT_MI})
DEFAULT_DISTANCE_UNIT = UNIT_KM

COOKIE_NAME = "matesla_distance_unit"
COOKIE_MAX_AGE = 365 * 24 * 60 * 60  # 1 year
# Query param appended on preference change so the browser cannot reuse a
# cached HTML page rendered with the previous unit (bfcache / disk cache).
CACHE_BUST_QUERY = "_du"

# kWh/100 km → Wh/mi:  (kWh/100km)*10 Wh/km * km_per_mi
KWH_PER_100KM_TO_WH_PER_MI = 10.0 * MILES_TO_KM  # ≈ 16.09344
WH_PER_MI_TO_KWH_PER_100KM = 1.0 / KWH_PER_100KM_TO_WH_PER_MI


def normalize_unit(value: Any) -> str:
    raw = (str(value) if value is not None else "").strip().lower()
    if raw in ("mi", "mile", "miles", "imperial"):
        return UNIT_MI
    if raw in ("km", "kilometer", "kilometre", "kilometers", "kilometres", "metric"):
        return UNIT_KM
    return DEFAULT_DISTANCE_UNIT


def get_distance_unit(request=None) -> str:
    """Resolve preference from request cookie; default km."""
    if request is None:
        return DEFAULT_DISTANCE_UNIT
    cookie = getattr(request, "COOKIES", None) or {}
    return normalize_unit(cookie.get(COOKIE_NAME))


def attach_distance_unit(request) -> str:
    """Set request.distance_unit and return it."""
    unit = get_distance_unit(request)
    request.distance_unit = unit
    return unit


def set_distance_unit_cookie(response, unit: str, *, secure: bool = False) -> None:
    unit = normalize_unit(unit)
    response.set_cookie(
        COOKIE_NAME,
        unit,
        max_age=COOKIE_MAX_AGE,
        path="/",
        samesite="Lax",
        secure=secure,
        httponly=False,  # allow future client-side reads if needed
    )


def with_query_param(url: str, key: str, value: str) -> str:
    """Set or replace a query parameter on an absolute or relative URL."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parts = urlparse(url or "/")
    path = parts.path if parts.path else "/"
    query_items = [
        (name, val)
        for name, val in parse_qsl(parts.query, keep_blank_values=True)
        if name != key
    ]
    query_items.append((key, value))
    return urlunparse(
        (parts.scheme, parts.netloc, path, parts.params, urlencode(query_items), parts.fragment)
    )


def redirect_url_for_unit(next_url: str, unit: str) -> str:
    """Safe return URL after unit change, cache-busted so HTML is re-rendered."""
    unit = normalize_unit(unit)
    return with_query_param(next_url or "/", CACHE_BUST_QUERY, unit)


def is_km(unit: str | None = None) -> bool:
    return normalize_unit(unit) == UNIT_KM


def miles_to_display(miles: float | None, unit: str | None = None) -> float | None:
    if miles is None:
        return None
    try:
        value = float(miles)
    except (TypeError, ValueError):
        return None
    if is_km(unit):
        return value * MILES_TO_KM
    return value


def km_to_display(km: float | None, unit: str | None = None) -> float | None:
    if km is None:
        return None
    try:
        value = float(km)
    except (TypeError, ValueError):
        return None
    if is_km(unit):
        return value
    return value * KM_TO_MILES


def mph_to_display(mph: float | None, unit: str | None = None) -> float | None:
    """Tesla road speed is mph; display as km/h or mph."""
    return miles_to_display(mph, unit)


def kmh_to_display(kmh: float | None, unit: str | None = None) -> float | None:
    return km_to_display(kmh, unit)


def kwh_per_100km_to_display(
    kwh_per_100km: float | None, unit: str | None = None
) -> float | None:
    """
    Energy intensity: kWh/100 km (metric) or Wh/mi (imperial / Tesla US).
    """
    if kwh_per_100km is None:
        return None
    try:
        value = float(kwh_per_100km)
    except (TypeError, ValueError):
        return None
    if is_km(unit):
        return value
    return value * KWH_PER_100KM_TO_WH_PER_MI


def wh_per_km_to_display(wh_per_km: float | None, unit: str | None = None) -> float | None:
    if wh_per_km is None:
        return None
    try:
        value = float(wh_per_km)
    except (TypeError, ValueError):
        return None
    if is_km(unit):
        return value
    return value * MILES_TO_KM  # Wh/mi


def unit_labels(unit: str | None = None) -> dict[str, str]:
    """Short labels for templates / JS."""
    if is_km(unit):
        return {
            "distance": "km",
            "speed": "km/h",
            "energy": "kWh/100 km",
            "wh_dist": "Wh/km",
            "range_rate": "km/h",
        }
    return {
        "distance": "mi",
        "speed": "mph",
        "energy": "Wh/mi",
        "wh_dist": "Wh/mi",
        "range_rate": "mph",
    }


def format_number(value: float | None, decimals: int = 1) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if decimals <= 0:
        return f"{number:.0f}"
    return f"{number:.{decimals}f}"


def format_distance(
    miles: float | None,
    unit: str | None = None,
    *,
    decimals: int = 1,
    with_unit: bool = True,
) -> str:
    display = miles_to_display(miles, unit)
    if display is None:
        return "—"
    text = format_number(display, decimals) or "—"
    if not with_unit:
        return text
    return f"{text} {unit_labels(unit)['distance']}"


def format_speed_from_mph(
    mph: float | None,
    unit: str | None = None,
    *,
    decimals: int = 0,
    with_unit: bool = True,
) -> str:
    display = mph_to_display(mph, unit)
    if display is None:
        return "—"
    text = format_number(display, decimals) or "—"
    if not with_unit:
        return text
    return f"{text} {unit_labels(unit)['speed']}"


def format_speed_from_kmh(
    kmh: float | None,
    unit: str | None = None,
    *,
    decimals: int = 0,
    with_unit: bool = True,
) -> str:
    display = kmh_to_display(kmh, unit)
    if display is None:
        return "—"
    text = format_number(display, decimals) or "—"
    if not with_unit:
        return text
    return f"{text} {unit_labels(unit)['speed']}"


def format_energy_intensity(
    kwh_per_100km: float | None,
    unit: str | None = None,
    *,
    decimals: int = 1,
    with_unit: bool = True,
) -> str:
    display = kwh_per_100km_to_display(kwh_per_100km, unit)
    if display is None:
        return "—"
    # Wh/mi is typically shown as integer; kWh/100 km with 1 decimal
    if not is_km(unit) and decimals == 1:
        decimals = 0
    text = format_number(display, decimals) or "—"
    if not with_unit:
        return text
    return f"{text} {unit_labels(unit)['energy']}"


def format_epa_range(
    epa_miles: float | int | None,
    unit: str | None = None,
    *,
    decimals: int = 0,
) -> str:
    """
    EPA range is officially in miles (US standard).

    - mi mode: ``341 mi``
    - km mode: ``549 km (341 mi)`` — official miles kept in parentheses
    """
    if epa_miles is None:
        return "—"
    try:
        miles = float(epa_miles)
    except (TypeError, ValueError):
        return "—"
    miles_text = format_number(miles, decimals) or "—"
    if is_km(unit):
        km_text = format_number(miles * MILES_TO_KM, decimals) or "—"
        return f"{km_text} km ({miles_text} mi)"
    return f"{miles_text} mi"


def context_for_unit(unit: str | None = None) -> dict[str, Any]:
    unit = normalize_unit(unit)
    labels = unit_labels(unit)
    return {
        "distance_unit": unit,
        "is_unit_km": unit == UNIT_KM,
        "is_unit_mi": unit == UNIT_MI,
        "u_dist": labels["distance"],
        "u_speed": labels["speed"],
        "u_energy": labels["energy"],
        "u_wh_dist": labels["wh_dist"],
        "u_range_rate": labels["range_rate"],
        "miles_to_km": MILES_TO_KM,
    }
