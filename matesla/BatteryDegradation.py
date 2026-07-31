"""
Battery degradation (%) from rated range vs EPA-when-new.

EPA range is not in the Fleet API. We resolve it from the VIN (+ wheel_type,
optional live range sample) via matesla.epa_catalog, and cache the result on
TeslaCarInfo.EPARange so capture does not re-lookup every poll.
"""

from matesla.VinAnalysis import GetModelFromVin, GetYearFromVin, IsDualMotor
from matesla.epa_catalog import lookup_epa_miles, project_full_charge_miles
from matesla.models.TeslaCarInfo import TeslaCarInfo


def GetEPARangeFromCache(vin):
    """Return cached EPA miles for vin, or None if unknown."""
    car_info_rows = TeslaCarInfo.objects.filter(vin=vin)
    if len(car_info_rows) > 0 and car_info_rows[0].EPARange is not None:
        return car_info_rows[0].EPARange
    return None


def _wheel_type_for_vin(vin, car_info=None):
    """Prefer wheel_type from the given row, else look up TeslaCarInfo."""
    if car_info is not None and car_info.wheel_type:
        return car_info.wheel_type
    car_info_rows = TeslaCarInfo.objects.filter(vin=vin)
    if len(car_info_rows) > 0:
        return car_info_rows[0].wheel_type
    return None


def _projected_full_from_db(vin):
    """
    Best projected 100% rated miles from recent high-SoC snapshots.

    Used when the live sample is missing or too low-SoC to project full range.
    """
    try:
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        recent_samples = (
            TeslaCarDataSnapshot.objects.filter(
                vin=vin,
                battery_level__gte=50,
                battery_range__gt=50,
            )
            .order_by("-Date")
            .values_list("battery_range", "battery_level")[:40]
        )
        best_projection = None
        for battery_range_miles, battery_level_percent in recent_samples:
            projected = project_full_charge_miles(
                battery_range_miles, battery_level_percent
            )
            if projected is None:
                continue
            if best_projection is None or projected > best_projection:
                best_projection = projected
        return best_projection
    except Exception:
        return None


def ResolveEPARange(
    vin,
    *,
    force: bool = False,
    battery_range=None,
    battery_level=None,
    wheel_type=None,
):
    """
    Resolve and optionally persist EPA miles for a VIN.

    force=True recomputes even if TeslaCarInfo.EPARange is already set.
    Returns (epa_miles, model_code, is_dual_motor, model_year).
    """
    car_info_rows = TeslaCarInfo.objects.filter(vin=vin)
    car_info = car_info_rows[0] if len(car_info_rows) > 0 else None

    if car_info is not None and car_info.EPARange is not None and not force:
        return (
            car_info.EPARange,
            GetModelFromVin(vin),
            car_info.isDualMotor
            if car_info.isDualMotor is not None
            else IsDualMotor(vin),
            car_info.modelYear
            if car_info.modelYear is not None
            else GetYearFromVin(vin),
        )

    projected_full_miles = project_full_charge_miles(battery_range, battery_level)
    if projected_full_miles is None:
        projected_full_miles = _projected_full_from_db(vin)

    resolved_wheel_type = wheel_type or _wheel_type_for_vin(vin, car_info)
    epa_miles, _catalog_meta = lookup_epa_miles(
        vin,
        wheel_type=resolved_wheel_type,
        projected_full_miles=projected_full_miles,
    )

    model_code = GetModelFromVin(vin)
    is_dual_motor = IsDualMotor(vin)
    model_year = GetYearFromVin(vin)

    if car_info is not None and epa_miles is not None:
        car_info.EPARange = int(round(epa_miles))
        fields_to_update = ["EPARange"]
        if car_info.isDualMotor is None and is_dual_motor is not None:
            car_info.isDualMotor = is_dual_motor
            fields_to_update.append("isDualMotor")
        if car_info.modelYear is None and model_year is not None:
            car_info.modelYear = model_year
            fields_to_update.append("modelYear")
        car_info.save(update_fields=fields_to_update)

    return epa_miles, model_code, is_dual_motor, model_year


def GetEPARange(vin):
    """Public API used by degradation + tests: (epa, model, dual, year)."""
    return ResolveEPARange(vin, force=False)


def UpdateBatteryEPARange(vin, epa_range_miles):
    """Persist a corrected EPA range (miles) on TeslaCarInfo."""
    car_info_rows = TeslaCarInfo.objects.filter(vin=vin)
    if len(car_info_rows) > 0:
        car_info = car_info_rows[0]
        car_info.EPARange = (
            int(round(epa_range_miles)) if epa_range_miles is not None else None
        )
        car_info.save(update_fields=["EPARange"])


def ComputeBatteryDegradationFromEPARange(
    battery_range_miles, battery_level_percent, epa_range_miles
):
    """
    Degradation % = how much full-charge rated range fell vs EPA when new.

    full_charge_now ≈ battery_range / (battery_level/100)
    degradation = (1 - full_charge_now / EPA) * 100
    """
    if (
        epa_range_miles is None
        or battery_level_percent is None
        or battery_range_miles is None
    ):
        return None
    try:
        level = float(battery_level_percent)
        rated_range = float(battery_range_miles)
        epa = float(epa_range_miles)
    except (TypeError, ValueError):
        return None
    if level <= 0 or epa <= 0:
        return None
    battery_degradation_percent = (1.0 - ((rated_range / level) * 100.0) / epa) * 100.0
    return battery_degradation_percent


def ComputeNumCycles(epa_range_miles, odometer_miles):
    """
    Rough lifetime cycle estimate: odometer / EPA, with +20% for regen/vampire
    energy that refilled the pack without adding odometer miles.
    """
    if epa_range_miles is None or odometer_miles is None:
        return None
    if epa_range_miles == 0:
        return None
    cycles = (1.0 * odometer_miles) / epa_range_miles
    cycles = cycles * 1.2
    return cycles


def ComputeBatteryDegradation(
    battery_range_miles, battery_level_percent, vin, odometer_miles
):
    """
    Full degradation pipeline for one sample: resolve EPA, compute %, cycles.

    Includes safety nets when VIN decode mis-tags pack size (RWD Model 3 SR
    vs LR, Model S 75/90/100 ambiguity) which otherwise yields nonsense
    negative degradation.
    """
    epa_range_miles, model_code, is_dual_motor, _model_year = ResolveEPARange(
        vin,
        force=False,
        battery_range=battery_range_miles,
        battery_level=battery_level_percent,
    )
    if epa_range_miles is None:
        return None, None, None
    battery_degradation_percent = ComputeBatteryDegradationFromEPARange(
        battery_range_miles, battery_level_percent, epa_range_miles
    )
    if battery_degradation_percent is None:
        return None, None, None

    # Safety net: RWD still mis-tagged as SR (EPA too low) → large negative deg
    if model_code == "3" and is_dual_motor is False and battery_degradation_percent < -8:
        for candidate_epa in (325, 358, 363):
            candidate_degradation = ComputeBatteryDegradationFromEPARange(
                battery_range_miles, battery_level_percent, candidate_epa
            )
            if candidate_degradation is not None and candidate_degradation >= -2:
                epa_range_miles = candidate_epa
                battery_degradation_percent = candidate_degradation
                UpdateBatteryEPARange(vin, epa_range_miles)
                break

    # Model S pack ambiguity (75 / 90 / 100)
    if model_code == "S" and is_dual_motor is True and battery_degradation_percent < 0:
        for candidate_epa in (270.0, 294.0, 335.0, 405.0):
            candidate_degradation = ComputeBatteryDegradationFromEPARange(
                battery_range_miles, battery_level_percent, candidate_epa
            )
            if candidate_degradation is not None and candidate_degradation >= 0:
                epa_range_miles = candidate_epa
                battery_degradation_percent = candidate_degradation
                UpdateBatteryEPARange(vin, epa_range_miles)
                return (
                    battery_degradation_percent,
                    ComputeNumCycles(epa_range_miles, odometer_miles),
                    epa_range_miles,
                )

    if battery_degradation_percent < 0:
        battery_degradation_percent = 0.0
    return (
        battery_degradation_percent,
        ComputeNumCycles(epa_range_miles, odometer_miles),
        epa_range_miles,
    )
