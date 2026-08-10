"""Template filters for distance / speed / energy display units."""

from __future__ import annotations

from django import template

from matesla.units import (
    format_distance,
    format_energy_intensity,
    format_epa_range,
    format_number,
    format_speed_from_kmh,
    format_speed_from_mph,
    km_to_display,
    kwh_per_100km_to_display,
    miles_to_display,
    mph_to_display,
    normalize_unit,
    unit_labels,
    wh_per_km_to_display,
)

register = template.Library()


def _unit_from_context(context, unit=None) -> str:
    if unit is not None and str(unit).strip():
        return normalize_unit(unit)
    if context is not None:
        return normalize_unit(context.get("distance_unit"))
    return normalize_unit(None)


@register.simple_tag(takes_context=True)
def dist_mi(context, miles, decimals=1, with_unit=True, unit=None):
    """Format a miles value in the active (or forced) distance unit."""
    u = _unit_from_context(context, unit)
    return format_distance(miles, u, decimals=int(decimals), with_unit=bool(with_unit))


@register.simple_tag(takes_context=True)
def dist_km(context, km, decimals=1, with_unit=True, unit=None):
    """Format a kilometre value in the active distance unit."""
    u = _unit_from_context(context, unit)
    display = km_to_display(km, u)
    if display is None:
        return "—"
    text = format_number(display, int(decimals)) or "—"
    if not with_unit:
        return text
    return f"{text} {unit_labels(u)['distance']}"


@register.simple_tag(takes_context=True)
def speed_mph(context, mph, decimals=0, with_unit=True, unit=None):
    u = _unit_from_context(context, unit)
    return format_speed_from_mph(
        mph, u, decimals=int(decimals), with_unit=bool(with_unit)
    )


@register.simple_tag(takes_context=True)
def speed_kmh(context, kmh, decimals=0, with_unit=True, unit=None):
    u = _unit_from_context(context, unit)
    return format_speed_from_kmh(
        kmh, u, decimals=int(decimals), with_unit=bool(with_unit)
    )


@register.simple_tag(takes_context=True)
def energy_kwh100(context, kwh_per_100km, decimals=1, with_unit=True, unit=None):
    u = _unit_from_context(context, unit)
    return format_energy_intensity(
        kwh_per_100km, u, decimals=int(decimals), with_unit=bool(with_unit)
    )


@register.simple_tag(takes_context=True)
def epa_range(context, epa_miles, decimals=0, unit=None):
    u = _unit_from_context(context, unit)
    return format_epa_range(epa_miles, u, decimals=int(decimals))


@register.filter
def as_distance_from_miles(miles, unit):
    """Filter: ``{{ miles|as_distance_from_miles:distance_unit }}`` (number only)."""
    display = miles_to_display(miles, unit)
    return display if display is not None else ""


@register.filter
def as_distance_from_km(km, unit):
    display = km_to_display(km, unit)
    return display if display is not None else ""


@register.filter
def as_speed_from_mph(mph, unit):
    display = mph_to_display(mph, unit)
    return display if display is not None else ""


@register.filter
def as_speed_from_kmh(kmh, unit):
    from matesla.units import kmh_to_display

    display = kmh_to_display(kmh, unit)
    return display if display is not None else ""


@register.filter
def as_energy_from_kwh100(kwh_per_100km, unit):
    display = kwh_per_100km_to_display(kwh_per_100km, unit)
    return display if display is not None else ""


@register.filter
def as_wh_from_whkm(wh_per_km, unit):
    display = wh_per_km_to_display(wh_per_km, unit)
    return display if display is not None else ""
