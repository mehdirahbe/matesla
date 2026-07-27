"""
Refine coarse (integer) SoC from battery_range.

Fleet REST vehicle_data exposes battery_level as a whole percent. TeslaFi and
the car's trip graph use finer resolution. battery_range still moves in tenths
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
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return abs(v - round(v)) < 1e-6


def implied_full_range_miles(battery_range, battery_level) -> float | None:
    try:
        br = float(battery_range)
        bl = float(battery_level)
    except (TypeError, ValueError):
        return None
    if br <= 50 or bl <= 1:
        return None
    return br / (bl / 100.0)


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
        br = float(battery_range)
        full = float(pack_rated_miles)
    except (TypeError, ValueError):
        return level
    if br <= 0 or full < 50:
        return level

    refined = 100.0 * br / full
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

    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
    from matesla.BatteryDegradation import GetEPARangeFromCache

    rows = list(
        TeslaCarDataSnapshot.objects.filter(
            vin=vin,
            battery_level__gt=5,
            battery_range__gt=50,
        )
        .order_by("-Date")
        .values_list("battery_level", "battery_range")[:PACK_SAMPLE_LIMIT]
    )

    fractional_implied: list[float] = []
    all_implied: list[float] = []
    for bl, br in rows:
        full = implied_full_range_miles(br, bl)
        if full is None or full < 50 or full > 600:
            continue
        all_implied.append(full)
        if not is_whole_percent(bl):
            fractional_implied.append(full)

    pack = None
    if len(fractional_implied) >= 5:
        pack = float(median(fractional_implied))
    elif len(all_implied) >= 5:
        pack = float(median(all_implied))
    else:
        epa = GetEPARangeFromCache(vin)
        if epa and epa > 50:
            pack = float(epa)

    if pack is not None and use_cache:
        cache.set(cache_key, pack, PACK_CACHE_SECONDS)
    return pack


def apply_soc_refinement(battery_level, usable_battery_level, battery_range, vin):
    """
    Refine integer API SoC fields in place-style: returns (bl, ubl).

    usable is refined when missing, equal to battery_level, or also a whole percent.
    """
    pack = estimate_pack_rated_miles(vin)
    new_bl = refine_soc_percent(battery_level, battery_range, pack)
    new_ubl = usable_battery_level
    if usable_battery_level is None or usable_battery_level == battery_level or is_whole_percent(
        usable_battery_level
    ):
        # Prefer refining usable from the same pack; if usable was None, mirror battery
        base_u = usable_battery_level if usable_battery_level is not None else battery_level
        refined_u = refine_soc_percent(base_u, battery_range, pack)
        if refined_u is not None:
            new_ubl = refined_u
        elif new_bl is not None and usable_battery_level is None:
            new_ubl = new_bl
    return new_bl, new_ubl


def invalidate_pack_cache(vin: str | None) -> None:
    if vin:
        cache.delete(f"matesla:pack_rated_mi:{vin}")
