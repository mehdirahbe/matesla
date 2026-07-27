"""
Battery degradation (%) from rated range vs EPA-when-new.

EPA is not in the API. We resolve it from VIN (+ wheel_type, optional live
range sample) via matesla.epa_catalog, and cache on TeslaCarInfo.EPARange.
"""

from matesla.VinAnalysis import GetModelFromVin, IsDualMotor, GetYearFromVin
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.epa_catalog import (
    lookup_epa_miles,
    project_full_charge_miles,
)


def GetEPARangeFromCache(vin):
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0 and carInfos[0].EPARange is not None:
        return carInfos[0].EPARange
    return None


def _wheel_type_for_vin(vin, car_info=None):
    if car_info is not None and car_info.wheel_type:
        return car_info.wheel_type
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0:
        return carInfos[0].wheel_type
    return None


def _projected_full_from_db(vin):
    """Best projected 100% rated miles from recent high-SoC snapshots."""
    try:
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        qs = (
            TeslaCarDataSnapshot.objects.filter(
                vin=vin,
                battery_level__gte=50,
                battery_range__gt=50,
            )
            .order_by("-Date")
            .values_list("battery_range", "battery_level")[:40]
        )
        best = None
        for br, bl in qs:
            p = project_full_charge_miles(br, bl)
            if p is None:
                continue
            if best is None or p > best:
                best = p
        return best
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
    Resolve and optionally persist EPA miles for vin.

    force=True recomputes even if TeslaCarInfo.EPARange is already set.
    """
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    carInfo = carInfos[0] if len(carInfos) > 0 else None

    if carInfo is not None and carInfo.EPARange is not None and not force:
        return (
            carInfo.EPARange,
            GetModelFromVin(vin),
            carInfo.isDualMotor if carInfo.isDualMotor is not None else IsDualMotor(vin),
            carInfo.modelYear if carInfo.modelYear is not None else GetYearFromVin(vin),
        )

    projected = project_full_charge_miles(battery_range, battery_level)
    if projected is None:
        projected = _projected_full_from_db(vin)

    wt = wheel_type or _wheel_type_for_vin(vin, carInfo)
    epa, _meta = lookup_epa_miles(vin, wheel_type=wt, projected_full_miles=projected)

    model = GetModelFromVin(vin)
    isDual = IsDualMotor(vin)
    year = GetYearFromVin(vin)

    if carInfo is not None and epa is not None:
        carInfo.EPARange = int(round(epa))
        fields = ["EPARange"]
        if carInfo.isDualMotor is None and isDual is not None:
            carInfo.isDualMotor = isDual
            fields.append("isDualMotor")
        if carInfo.modelYear is None and year is not None:
            carInfo.modelYear = year
            fields.append("modelYear")
        carInfo.save(update_fields=fields)

    return epa, model, isDual, year


def GetEPARange(vin):
    """Public API used by degradation + tests: (epa, model, dual, year)."""
    return ResolveEPARange(vin, force=False)


# Update EPARange (miles), e.g. after heuristic correction
def UpdateBatteryEPARange(vin, EPARange):
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0:
        carInfo = carInfos[0]
        carInfo.EPARange = int(round(EPARange)) if EPARange is not None else None
        carInfo.save(update_fields=["EPARange"])


def ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange):
    if EPARange is None or battery_level is None or batteryrange is None:
        return None
    try:
        bl = float(battery_level)
        br = float(batteryrange)
        epa = float(EPARange)
    except (TypeError, ValueError):
        return None
    if bl <= 0 or epa <= 0:
        return None
    batterydegradation = (1.0 - ((br / bl) * 100.0) / epa) * 100.0
    return batterydegradation


def ComputeNumCycles(EPARange, odometerMiles):
    if EPARange is None or odometerMiles is None:
        return None
    if EPARange == 0:
        return None
    cycles = (1.0 * odometerMiles) / EPARange
    # rough guess: +20% for regen / vampire refilled without odometer
    cycles = cycles * 1.2
    return cycles


def ComputeBatteryDegradation(batteryrange, battery_level, vin, odometerMiles):
    EPARange, model, isDual, year = ResolveEPARange(
        vin,
        force=False,
        battery_range=batteryrange,
        battery_level=battery_level,
    )
    if EPARange is None:
        return None, None, None
    batterydegradation = ComputeBatteryDegradationFromEPARange(
        batteryrange, battery_level, EPARange
    )
    if batterydegradation is None:
        return None, None, None

    # Safety net: RWD still mis-tagged as SR (EPA too low) → large negative deg
    if model == "3" and isDual is False and batterydegradation < -8:
        for candidate in (325, 358, 363):
            deg = ComputeBatteryDegradationFromEPARange(
                batteryrange, battery_level, candidate
            )
            if deg is not None and deg >= -2:
                EPARange = candidate
                batterydegradation = deg
                UpdateBatteryEPARange(vin, EPARange)
                break

    # Model S pack ambiguity (75 / 90 / 100)
    if model == "S" and isDual is True and batterydegradation < 0:
        for candidate in (270.0, 294.0, 335.0, 405.0):
            deg = ComputeBatteryDegradationFromEPARange(
                batteryrange, battery_level, candidate
            )
            if deg is not None and deg >= 0:
                EPARange = candidate
                batterydegradation = deg
                UpdateBatteryEPARange(vin, EPARange)
                return (
                    batterydegradation,
                    ComputeNumCycles(EPARange, odometerMiles),
                    EPARange,
                )

    if batterydegradation < 0:
        batterydegradation = 0.0
    return batterydegradation, ComputeNumCycles(EPARange, odometerMiles), EPARange
