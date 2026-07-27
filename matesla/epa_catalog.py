"""
EPA rated range (miles) catalog for battery degradation.

Design goals:
- Declarative table (not nested if/else hell) — easy to extend when Tesla
  publishes new ratings.
- Match by model, year, drivetrain, trim, wheel size, plant when known.
- When several rows match (e.g. SR vs LR RWD share the same VIN motor code),
  disambiguate using a live projected full-charge range sample when available.

Sources: fueleconomy.gov / ENERGY STAR / historical Tesla software ratings
(cars often keep the rated miles they shipped with, not the latest brochure).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from matesla.VinAnalysis import (
    GetModelFromVin,
    GetPlantRegionFromVin,
    GetYearFromVin,
    IsDualMotor,
    IsPerformanceMotor,
    GuessTrimFromVin,
    WheelInchesFromType,
)


@dataclass(frozen=True)
class EPAEntry:
    """One known EPA configuration. None optional fields = wildcard."""

    model: str  # "3", "Y", "S", "X"
    year_min: int
    year_max: int
    dual: Optional[bool]  # True AWD, False RWD, None any
    trim: Optional[str]  # "sr", "lr", "perf", None
    wheels_in: Optional[int]  # 18, 19, 20…
    plant: Optional[str]  # "US", "CN", "EU", None
    epa_miles: int
    note: str = ""


# Ordered from more specific / common truth for *software* rated range.
# Prefer the figure the car itself uses when new (often sticky across OTA).
EPA_TABLE: list[EPAEntry] = [
    # --- Model 3 pre-Highland dual motor (classic LR AWD, e.g. Corentin) ---
    EPAEntry("3", 2017, 2019, True, "lr", None, None, 310, "M3 LR AWD classic ≤2019"),
    EPAEntry("3", 2020, 2020, True, "lr", None, None, 322, "M3 LR AWD 2020 EPA"),
    EPAEntry("3", 2021, 2023, True, "lr", None, None, 353, "M3 LR AWD 2021+ 82kWh US rating"),
    EPAEntry("3", 2021, 2023, True, "perf", None, None, 315, "M3 Performance 2021+"),
    # Highland / 2024+ (US EPA ~342 LR AWD; 18" aero; 19" slightly less)
    EPAEntry("3", 2024, 2035, True, "lr", 18, None, 342, "M3 Highland LR AWD 18\""),
    EPAEntry("3", 2024, 2035, True, "lr", 19, None, 333, "M3 Highland LR AWD 19\" approx"),
    EPAEntry("3", 2024, 2035, True, "lr", None, None, 342, "M3 Highland LR AWD default"),
    EPAEntry("3", 2024, 2035, False, "lr", 18, None, 363, "M3 Highland LR RWD 18\""),
    EPAEntry("3", 2024, 2035, False, "lr", None, None, 363, "M3 Highland LR RWD"),
    EPAEntry("3", 2024, 2035, False, "sr", None, None, 272, "M3 Highland RWD / SR-class"),
    # Pre-Highland RWD
    EPAEntry("3", 2017, 2020, False, "lr", None, None, 325, "M3 LR RWD (rare, same pack as LR AWD)"),
    EPAEntry("3", 2017, 2020, False, "sr", None, None, 240, "M3 SR / SR+ ≤2020"),
    EPAEntry("3", 2021, 2023, False, "sr", None, None, 263, "M3 SR+ 2021+"),
    EPAEntry("3", 2021, 2023, False, "lr", None, None, 358, "M3 LR RWD 2021+ (market dependent)"),
    # --- Model Y (common) ---
    EPAEntry("Y", 2020, 2022, True, "lr", None, None, 326, "MY LR AWD early"),
    EPAEntry("Y", 2023, 2024, True, "lr", None, None, 330, "MY LR AWD mid"),
    EPAEntry("Y", 2020, 2025, False, "lr", None, None, 330, "MY RWD LR-class"),
    EPAEntry("Y", 2020, 2025, True, "perf", None, None, 303, "MY Performance"),
    # --- Model S (very rough — packs vary a lot) ---
    EPAEntry("S", 2016, 2019, True, None, None, None, 259, "MS 75D-class default"),
    EPAEntry("S", 2020, 2025, True, "lr", None, None, 405, "MS Long Range refresh-ish"),
    # --- Model X ---
    EPAEntry("X", 2016, 2020, True, None, None, None, 295, "MX default legacy"),
    EPAEntry("X", 2021, 2025, True, "lr", None, None, 348, "MX LR refresh-ish"),
]


def _wheel_match(entry_w: Optional[int], actual: Optional[int]) -> bool:
    if entry_w is None:
        return True
    if actual is None:
        return True  # don't reject; less specific rows also exist
    return entry_w == actual


def _plant_match(entry_p: Optional[str], actual: Optional[str]) -> bool:
    if entry_p is None:
        return True
    if actual is None:
        return True
    return entry_p == actual


def _trim_match(entry_t: Optional[str], actual: Optional[str]) -> bool:
    if entry_t is None:
        return True
    if actual is None:
        return True
    return entry_t == actual


def _specificity(entry: EPAEntry) -> int:
    """Higher = more constrained row (prefer when several match)."""
    score = 0
    if entry.dual is not None:
        score += 2
    if entry.trim is not None:
        score += 3
    if entry.wheels_in is not None:
        score += 2
    if entry.plant is not None:
        score += 1
    # narrower year window slightly preferred
    score += max(0, 10 - (entry.year_max - entry.year_min))
    return score


def matching_entries(
    *,
    model: str,
    year: int,
    dual: Optional[bool],
    trim: Optional[str],
    wheels_in: Optional[int],
    plant: Optional[str],
) -> list[EPAEntry]:
    out: list[EPAEntry] = []
    for e in EPA_TABLE:
        if e.model != model:
            continue
        if year < e.year_min or year > e.year_max:
            continue
        if e.dual is not None and dual is not None and e.dual != dual:
            continue
        if not _trim_match(e.trim, trim):
            continue
        if not _wheel_match(e.wheels_in, wheels_in):
            continue
        if not _plant_match(e.plant, plant):
            continue
        out.append(e)
    out.sort(key=_specificity, reverse=True)
    return out


def pick_epa_miles(
    candidates: Iterable[EPAEntry],
    projected_full_miles: Optional[float] = None,
) -> Optional[int]:
    """
    Choose one EPA from matching rows.

    If projected_full_miles is known (battery_range / soc * 100), pick the
    candidate whose implied degradation is most plausible:
      - not deeply negative (< -3%)
      - not absurdly high (> 40%)
      - among remaining, closest to ~5–15% mid-life cars, else smallest |deg|
    """
    cands = list(candidates)
    if not cands:
        return None
    if projected_full_miles is None or projected_full_miles < 50:
        return cands[0].epa_miles

    def score(epa: int) -> tuple:
        deg = 1.0 - (projected_full_miles / epa)
        # Hard reject strongly negative (EPA too low, e.g. SR on an LR car)
        if deg < -0.03:
            return (3, abs(deg), -_specificity_for_epa(epa, cands))
        if deg > 0.40:
            return (2, deg, -_specificity_for_epa(epa, cands))
        # Prefer mild positive degradation
        return (0, abs(deg - 0.08), -_specificity_for_epa(epa, cands))

    best = min(cands, key=lambda e: score(e.epa_miles))
    return best.epa_miles


def _specificity_for_epa(epa: int, cands: list[EPAEntry]) -> int:
    return max((_specificity(e) for e in cands if e.epa_miles == epa), default=0)


def lookup_epa_miles(
    vin: str,
    *,
    wheel_type: Optional[str] = None,
    projected_full_miles: Optional[float] = None,
    trim_hint: Optional[str] = None,
) -> tuple[Optional[int], dict]:
    """
    Main entry: return (epa_miles, debug_meta).
    """
    model = GetModelFromVin(vin)
    year = GetYearFromVin(vin)
    dual = IsDualMotor(vin)
    plant = GetPlantRegionFromVin(vin)
    wheels = WheelInchesFromType(wheel_type)
    trim = trim_hint or GuessTrimFromVin(vin, dual=dual, performance=IsPerformanceMotor(vin))

    meta = {
        "model": model,
        "year": year,
        "dual": dual,
        "plant": plant,
        "wheels_in": wheels,
        "trim": trim,
        "projected_full_miles": projected_full_miles,
    }
    if model is None or year is None:
        return None, meta

    matches = matching_entries(
        model=model,
        year=year,
        dual=dual,
        trim=trim,
        wheels_in=wheels,
        plant=plant,
    )
    # If trim unknown for RWD, try both sr and lr and let projected range decide
    if not matches and dual is False:
        matches = matching_entries(
            model=model,
            year=year,
            dual=False,
            trim=None,
            wheels_in=wheels,
            plant=plant,
        )
    if not matches:
        # last resort: ignore wheels/trim
        matches = matching_entries(
            model=model,
            year=year,
            dual=dual,
            trim=None,
            wheels_in=None,
            plant=None,
        )

    meta["matches"] = [
        (e.epa_miles, e.note, _specificity(e)) for e in matches[:8]
    ]
    epa = pick_epa_miles(matches, projected_full_miles)
    meta["epa"] = epa
    return epa, meta


def project_full_charge_miles(battery_range, battery_level) -> Optional[float]:
    try:
        br = float(battery_range)
        bl = float(battery_level)
    except (TypeError, ValueError):
        return None
    if br <= 0 or bl <= 1:
        return None
    return br / (bl / 100.0)
