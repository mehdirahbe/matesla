"""
Implied 100% rated range (miles) from stored snapshots.

Stored SoC is never rewritten from battery_range. This module only estimates
the current pack's full rated miles (median of range/soc) for other features
such as DC-charge “full rated miles”. TeslaFi fractional SoC is preferred
when enough samples exist.
"""

from __future__ import annotations

from statistics import median

from django.core.cache import cache

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


def estimate_pack_rated_miles(vin: str | None, *, use_cache: bool = True) -> float | None:
    """
    Median implied full-charge rated range (miles) for this VIN.

    Prefers samples that already have fractional SoC (TeslaFi).
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


def invalidate_pack_cache(vin: str | None) -> None:
    """Drop cached pack size after imports or manual EPA fixes."""
    if vin:
        cache.delete(f"matesla:pack_rated_mi:{vin}")
