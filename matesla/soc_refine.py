"""
Refine coarse (integer) SoC from battery_range.

Fleet REST vehicle_data exposes battery_level as a whole percent. The car's trip graph use finer resolution. battery_range still moves in tenths
of a mile, so:

    soc ≈ 100 * battery_range / pack_rated_miles

where pack_rated_miles is the car's current implied full-charge rated range
(median of recent range/soc samples — accounts for degradation better than raw EPA).

Important: on a *single* sample, range / (integer_soc/100) is tautological and
cannot create sub-percent precision. The pack estimate must come from a broader
history (or a clamped EPA fallback).
"""

from __future__ import annotations

from statistics import median

from django.core.cache import cache

# Refined SoC must stay near the API integer bucket (reject bad pack estimates).
MAX_REFINE_DELTA_PCT = 1.25
# Prefer fractional history when estimating pack size.
PACK_SAMPLE_LIMIT = 300
PACK_CACHE_SECONDS = 3600


def is_whole_percent(value) -> bool:
    """True when value looks like an integer percent (Fleet API SoC)."""
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return abs(numeric - round(numeric)) < 1e-6


def implied_full_range_miles(battery_range, battery_level) -> float | None:
    """Implied 100% rated miles from one sample: range / (soc/100)."""
    try:
        rated_range = float(battery_range)
        state_of_charge = float(battery_level)
    except (TypeError, ValueError):
        return None
    if rated_range <= 50 or state_of_charge <= 1:
        return None
    return rated_range / (state_of_charge / 100.0)


def refine_soc_percent(
    battery_level,
    battery_range,
    pack_rated_miles,
    *,
    max_delta: float = MAX_REFINE_DELTA_PCT,
) -> float | None:
    """
    Return refined SoC % when battery_level is a whole percent and pack/range
    allow it; otherwise return battery_level unchanged (or None if input None).
    """
    if battery_level is None:
        return None
    try:
        level = float(battery_level)
    except (TypeError, ValueError):
        return battery_level

    if battery_range is None or pack_rated_miles is None:
        return level
    if not is_whole_percent(level):
        return level  # already fractional (e.g. TeslaFi)

    try:
        rated_range = float(battery_range)
        pack_full_miles = float(pack_rated_miles)
    except (TypeError, ValueError):
        return level
    if rated_range <= 0 or pack_full_miles < 50:
        return level

    refined = 100.0 * rated_range / pack_full_miles
    if refined < 0 or refined > 105:
        return level
    # Stay inside the displayed integer bucket (±~1%)
    if abs(refined - level) > max_delta:
        return level
    return refined


def estimate_pack_rated_miles(vin: str | None, *, use_cache: bool = True) -> float | None:
    """
    Median implied full-charge rated range (miles) for this VIN.

    Prefers samples that already have fractional SoC (TeslaFi / previously refined).
    Falls back to all recent samples, then EPA cache.
    """
    if not vin:
        return None

    cache_key = f"matesla:pack_rated_mi:{vin}"
    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    from matesla.BatteryDegradation import GetEPARangeFromCache
    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

    recent_level_range_pairs = list(
        TeslaCarDataSnapshot.objects.filter(
            vin=vin,
            battery_level__gt=5,
            battery_range__gt=50,
        )
        .order_by("-Date")
        .values_list("battery_level", "battery_range")[:PACK_SAMPLE_LIMIT]
    )

    fractional_soc_implied: list[float] = []
    all_implied: list[float] = []
    for battery_level, battery_range in recent_level_range_pairs:
        full_miles = implied_full_range_miles(battery_range, battery_level)
        if full_miles is None or full_miles < 50 or full_miles > 600:
            continue
        all_implied.append(full_miles)
        if not is_whole_percent(battery_level):
            fractional_soc_implied.append(full_miles)

    pack_miles = None
    if len(fractional_soc_implied) >= 5:
        pack_miles = float(median(fractional_soc_implied))
    elif len(all_implied) >= 5:
        pack_miles = float(median(all_implied))
    else:
        epa_miles = GetEPARangeFromCache(vin)
        if epa_miles and epa_miles > 50:
            pack_miles = float(epa_miles)

    if pack_miles is not None and use_cache:
        cache.set(cache_key, pack_miles, PACK_CACHE_SECONDS)
    return pack_miles


def apply_soc_refinement(
    battery_level, usable_battery_level, battery_range, vin
):
    """
    Refine integer API SoC fields: returns (battery_level, usable_battery_level).

    usable is refined when missing, equal to battery_level, or also a whole percent.
    """
    pack_rated_miles = estimate_pack_rated_miles(vin)
    refined_battery_level = refine_soc_percent(
        battery_level, battery_range, pack_rated_miles
    )
    refined_usable = usable_battery_level
    if (
        usable_battery_level is None
        or usable_battery_level == battery_level
        or is_whole_percent(usable_battery_level)
    ):
        # Prefer refining usable from the same pack; if usable was None, mirror battery
        usable_base = (
            usable_battery_level
            if usable_battery_level is not None
            else battery_level
        )
        refined_from_pack = refine_soc_percent(
            usable_base, battery_range, pack_rated_miles
        )
        if refined_from_pack is not None:
            refined_usable = refined_from_pack
        elif refined_battery_level is not None and usable_battery_level is None:
            refined_usable = refined_battery_level
    return refined_battery_level, refined_usable


def invalidate_pack_cache(vin: str | None) -> None:
    """Drop cached pack size after imports or manual EPA fixes."""
    if vin:
        cache.delete(f"matesla:pack_rated_mi:{vin}")
