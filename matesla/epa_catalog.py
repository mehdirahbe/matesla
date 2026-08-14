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
    GuessTrimFromVin,
    IsDualMotor,
    IsPerformanceMotor,
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
    EPAEntry("3", 2024, 2035, True, "lr", 18, None, 342, 'M3 Highland LR AWD 18"'),
    EPAEntry("3", 2024, 2035, True, "lr", 19, None, 333, 'M3 Highland LR AWD 19" approx'),
    EPAEntry("3", 2024, 2035, True, "lr", None, None, 342, "M3 Highland LR AWD default"),
    EPAEntry("3", 2024, 2035, False, "lr", 18, None, 363, 'M3 Highland LR RWD 18"'),
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


def _wheel_match(entry_wheels_inches: Optional[int], actual_wheels_inches: Optional[int]) -> bool:
    """True if the catalog row accepts this wheel size (None = wildcard)."""
    if entry_wheels_inches is None:
        return True
    if actual_wheels_inches is None:
        return True  # don't reject; less specific rows also exist
    return entry_wheels_inches == actual_wheels_inches


def _plant_match(entry_plant: Optional[str], actual_plant: Optional[str]) -> bool:
    if entry_plant is None:
        return True
    if actual_plant is None:
        return True
    return entry_plant == actual_plant


def _trim_match(entry_trim: Optional[str], actual_trim: Optional[str]) -> bool:
    if entry_trim is None:
        return True
    if actual_trim is None:
        return True
    return entry_trim == actual_trim


def _specificity(entry: EPAEntry) -> int:
    """Higher score = more constrained row (prefer when several match)."""
    score = 0
    if entry.dual is not None:
        score += 2
    if entry.trim is not None:
        score += 3
    if entry.wheels_in is not None:
        score += 2
    if entry.plant is not None:
        score += 1
    # Narrower year window slightly preferred
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
    """Return catalog rows that match the vehicle identity, most specific first."""
    matches: list[EPAEntry] = []
    for entry in EPA_TABLE:
        if entry.model != model:
            continue
        if year < entry.year_min or year > entry.year_max:
            continue
        if entry.dual is not None and dual is not None and entry.dual != dual:
            continue
        if not _trim_match(entry.trim, trim):
            continue
        if not _wheel_match(entry.wheels_in, wheels_in):
            continue
        if not _plant_match(entry.plant, plant):
            continue
        matches.append(entry)
    matches.sort(key=_specificity, reverse=True)
    return matches


def pick_epa_miles(
    candidates: Iterable[EPAEntry],
    projected_full_miles: Optional[float] = None,
) -> Optional[int]:
    """
    Choose one EPA miles value from matching catalog rows.

    If projected_full_miles is known (battery_range / soc * 100), pick the
    candidate whose implied degradation is most plausible:
      - not deeply negative (< -3%)  → EPA too low for this pack
      - not absurdly high (> 40%)
      - among remaining, closest to ~8% mid-life, else smallest |deg|
    """
    candidate_list = list(candidates)
    if not candidate_list:
        return None
    if projected_full_miles is None or projected_full_miles < 50:
        return candidate_list[0].epa_miles

    def score(epa_miles: int) -> tuple:
        degradation_fraction = 1.0 - (projected_full_miles / epa_miles)
        # Hard reject strongly negative (EPA too low, e.g. SR on an LR car)
        if degradation_fraction < -0.03:
            return (
                3,
                abs(degradation_fraction),
                -_specificity_for_epa(epa_miles, candidate_list),
            )
        if degradation_fraction > 0.40:
            return (
                2,
                degradation_fraction,
                -_specificity_for_epa(epa_miles, candidate_list),
            )
        # Prefer mild positive degradation around ~8%
        return (
            0,
            abs(degradation_fraction - 0.08),
            -_specificity_for_epa(epa_miles, candidate_list),
        )

    best_entry = min(candidate_list, key=lambda entry: score(entry.epa_miles))
    return best_entry.epa_miles


def _specificity_for_epa(epa_miles: int, candidates: list[EPAEntry]) -> int:
    return max(
        (_specificity(entry) for entry in candidates if entry.epa_miles == epa_miles),
        default=0,
    )


def lookup_epa_miles(
    vin: str,
    *,
    wheel_type: Optional[str] = None,
    projected_full_miles: Optional[float] = None,
    trim_hint: Optional[str] = None,
) -> tuple[Optional[int], dict]:
    """
    Main entry: return (epa_miles, debug_meta) for a VIN + optional live range hint.
    """
    model = GetModelFromVin(vin)
    year = GetYearFromVin(vin)
    dual_motor = IsDualMotor(vin)
    plant_region = GetPlantRegionFromVin(vin)
    wheels_inches = WheelInchesFromType(wheel_type)
    trim = trim_hint or GuessTrimFromVin(
        vin, dual=dual_motor, performance=IsPerformanceMotor(vin)
    )

    debug_meta = {
        "model": model,
        "year": year,
        "dual": dual_motor,
        "plant": plant_region,
        "wheels_in": wheels_inches,
        "trim": trim,
        "projected_full_miles": projected_full_miles,
    }
    if model is None or year is None:
        return None, debug_meta

    matches = matching_entries(
        model=model,
        year=year,
        dual=dual_motor,
        trim=trim,
        wheels_in=wheels_inches,
        plant=plant_region,
    )
    # If trim unknown for RWD, try both sr and lr and let projected range decide
    if not matches and dual_motor is False:
        matches = matching_entries(
            model=model,
            year=year,
            dual=False,
            trim=None,
            wheels_in=wheels_inches,
            plant=plant_region,
        )
    if not matches:
        # Last resort: ignore wheels/trim
        matches = matching_entries(
            model=model,
            year=year,
            dual=dual_motor,
            trim=None,
            wheels_in=None,
            plant=None,
        )

    debug_meta["matches"] = [
        (entry.epa_miles, entry.note, _specificity(entry)) for entry in matches[:8]
    ]
    epa_miles = pick_epa_miles(matches, projected_full_miles)
    debug_meta["epa"] = epa_miles
    return epa_miles, debug_meta


def project_full_charge_miles(battery_range, battery_level) -> Optional[float]:
    """Extrapolate full-charge rated miles from a partial SoC sample."""
    try:
        rated_range = float(battery_range)
        state_of_charge = float(battery_level)
    except (TypeError, ValueError):
        return None
    if rated_range <= 0 or state_of_charge <= 1:
        return None
    return rated_range / (state_of_charge / 100.0)


# ---------------------------------------------------------------------------
# Usable pack capacity when new (kWh) — nameplate-class, NOT EPA × Wh/mi.
#
# Same physical pack can have different EPA rated miles (AWD vs RWD, wheels).
# Example: both 2019 Model 3 LR (AWD EPA 310 / RWD EPA 325) ship ~75 kWh usable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackEntry:
    """One known usable pack size when new. None optional fields = wildcard."""

    model: str
    year_min: int
    year_max: int
    dual: Optional[bool]
    trim: Optional[str]  # "sr", "lr", "perf", None
    pack_kwh: float
    note: str = ""


PACK_TABLE: list[PackEntry] = [
    # Model 3 classic (Panasonic ~75 kWh LR / smaller SR)
    PackEntry("3", 2017, 2020, None, "lr", 75.0, "M3 LR pre-Highland ~75 kWh usable"),
    PackEntry("3", 2017, 2020, None, "perf", 75.0, "M3 Performance classic same pack as LR"),
    PackEntry("3", 2017, 2020, None, "sr", 50.0, "M3 SR / SR+ ~50 kWh class"),
    # Model 3 2021–2023 (82 kWh LR class in many markets)
    PackEntry("3", 2021, 2023, None, "lr", 82.0, "M3 LR 2021–2023 ~82 kWh"),
    PackEntry("3", 2021, 2023, None, "perf", 82.0, "M3 Performance 2021–2023"),
    PackEntry("3", 2021, 2023, None, "sr", 60.0, "M3 SR+ 2021+ approx"),
    # Highland / 2024+
    PackEntry("3", 2024, 2035, None, "lr", 75.0, "M3 Highland LR ~75 kWh usable"),
    PackEntry("3", 2024, 2035, None, "perf", 75.0, "M3 Highland Performance"),
    PackEntry("3", 2024, 2035, None, "sr", 60.0, "M3 Highland RWD / SR-class approx"),
    # Model Y (common LR packs ~75 kWh class)
    PackEntry("Y", 2020, 2025, None, "lr", 75.0, "MY LR ~75 kWh class"),
    PackEntry("Y", 2020, 2025, None, "perf", 75.0, "MY Performance"),
    PackEntry("Y", 2020, 2025, None, "sr", 60.0, "MY RWD / SR-class approx"),
    # S/X very rough defaults
    PackEntry("S", 2016, 2019, None, None, 75.0, "MS 75-class default"),
    PackEntry("S", 2020, 2025, None, None, 100.0, "MS refresh large pack-ish"),
    PackEntry("X", 2016, 2020, None, None, 100.0, "MX legacy rough"),
    PackEntry("X", 2021, 2025, None, None, 100.0, "MX refresh rough"),
]


def _pack_specificity(entry: PackEntry) -> int:
    score = 0
    if entry.dual is not None:
        score += 2
    if entry.trim is not None:
        score += 3
    score += max(0, 10 - (entry.year_max - entry.year_min))
    return score


def _infer_trim_for_pack(
    trim: Optional[str],
    *,
    epa_range_miles: Optional[float] = None,
) -> Optional[str]:
    """When VIN trim is unknown, use EPA miles as a coarse SR vs LR hint."""
    if trim is not None:
        return trim
    if epa_range_miles is None:
        return None
    try:
        epa = float(epa_range_miles)
    except (TypeError, ValueError):
        return None
    if epa <= 0:
        return None
    # SR-class EPA is well below LR; 270 keeps 2019 SR (~240) vs LR RWD (~325) apart
    if epa < 270:
        return "sr"
    return "lr"


def lookup_pack_kwh(
    vin: str,
    *,
    epa_range_miles: Optional[float] = None,
    trim_hint: Optional[str] = None,
) -> Optional[float]:
    """
    Usable pack kWh when new for this VIN (catalog), or None if unknown.

    Prefer this over EPA×Wh/mi for capacity / SoC→kWh estimates: rated range
    changes with drivetrain and wheels while the pack stays the same.
    """
    model = GetModelFromVin(vin)
    year = GetYearFromVin(vin)
    if model is None or year is None:
        return None
    dual_motor = IsDualMotor(vin)
    trim = _infer_trim_for_pack(
        trim_hint
        or GuessTrimFromVin(vin, dual=dual_motor, performance=IsPerformanceMotor(vin)),
        epa_range_miles=epa_range_miles,
    )

    matches: list[PackEntry] = []
    for entry in PACK_TABLE:
        if entry.model != model:
            continue
        if year < entry.year_min or year > entry.year_max:
            continue
        if entry.dual is not None and dual_motor is not None and entry.dual != dual_motor:
            continue
        if not _trim_match(entry.trim, trim):
            continue
        matches.append(entry)

    if not matches and trim is not None:
        # Retry ignoring trim (e.g. unknown trim on a known model year)
        for entry in PACK_TABLE:
            if entry.model != model:
                continue
            if year < entry.year_min or year > entry.year_max:
                continue
            if entry.dual is not None and dual_motor is not None and entry.dual != dual_motor:
                continue
            if entry.trim is None:
                matches.append(entry)

    if not matches:
        return None
    matches.sort(key=_pack_specificity, reverse=True)
    return float(matches[0].pack_kwh)
