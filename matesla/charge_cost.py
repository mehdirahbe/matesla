"""
Price a derived charge session from user-entered places, tariffs and rates.

Rule order (most specific first):
  1. Tesla Supercharger invoice from charging history (or a stored amount)
  2. Home geofence for this vehicle on the session's local date (with role period)
  3. Work geofence likewise
  4. Supercharger site match → user Supercharger €/kWh
  5. Other chargers → user other-chargers €/kWh
  6. Unpriced (no invented euros)

Home dynamic: €/kWh = Elia spot €/MWh / 1000 + surcharge_cents / 100,
integrated per MTU (not one price × session kWh).

Civil clock for night windows and period dates: Europe/Brussels (same as DayMap).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from django.utils import timezone

from matesla.models.ChargeCost import (
    COST_PARTIAL,
    COST_PRICED,
    COST_UNPRICED,
    ChargeCostSettings,
    ChargePlace,
    ChargeSessionCost,
    DayAheadSpotPrice,
    PlaceTariffPeriod,
    ROLE_HOME,
    ROLE_WORK,
    RULE_HOME_DAY_NIGHT,
    RULE_HOME_DYNAMIC,
    RULE_HOME_FLAT,
    RULE_OTHER,
    RULE_PER_DAY,
    RULE_PLACE,
    RULE_SUPERCHARGER_INVOICE,
    RULE_SUPERCHARGER_RATE,
    RULE_UNPRICED,
    RULE_WORK,
    TARIFF_DAY_NIGHT,
    TARIFF_DYNAMIC,
    TARIFF_FLAT,
    TARIFF_PER_DAY,
    VehiclePlaceRole,
)

CHARGE_COST_TZ = ZoneInfo("Europe/Brussels")


@dataclass
class ChargeSession:
    hashed_vin: str
    start: datetime
    end: datetime
    kwh: float | None
    lat: float | None = None
    lon: float | None = None
    is_supercharger: bool = False
    tesla_invoice_eur: float | None = None
    tesla_session_id: str = ""
    tesla_site_name: str = ""
    # Optional (timestamp, kW) samples for MTU energy; scaled to kwh.
    power_samples: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class CostResult:
    cost_eur: float | None
    status: str
    rule: str
    place_id: int | None = None
    priced_kwh: float | None = None
    missing_kwh: float | None = None


def _as_utc(when: datetime) -> datetime:
    if timezone.is_naive(when):
        return when.replace(tzinfo=ZoneInfo("UTC"))
    return when.astimezone(ZoneInfo("UTC"))


def _local(when: datetime) -> datetime:
    return _as_utc(when).astimezone(CHARGE_COST_TZ)


def _local_date(when: datetime) -> date:
    return _local(when).date()


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


def _in_date_range(day: date, valid_from: date, valid_to: date | None) -> bool:
    if day < valid_from:
        return False
    if valid_to is not None and day > valid_to:
        return False
    return True


def _is_night(local_dt: datetime, night_start: time, night_end: time) -> bool:
    clock = local_dt.time()
    if night_start <= night_end:
        return night_start <= clock < night_end
    return clock >= night_start or clock < night_end


def owner_user_id(hashed_vin: str):
    """Best-effort owner of this hashed VIN (TeslaVehicle), or None."""
    from matesla.models.TeslaCarInfo import TeslaCarInfo
    from matesla.models.TeslaToken import TeslaVehicle
    from matesla.models.VinHash import HashTheVin

    info = TeslaCarInfo.objects.filter(hashedVin=hashed_vin).first()
    vin = (info.vin if info else "") or ""
    if vin:
        vehicle = (
            TeslaVehicle.objects.filter(vin=vin)
            .select_related("user")
            .first()
        )
        if vehicle:
            return vehicle.user_id
    for vehicle in TeslaVehicle.objects.exclude(vin="").select_related("user"):
        if HashTheVin(vehicle.vin) == hashed_vin:
            return vehicle.user_id
    return None


def settings_for_hashed_vin(hashed_vin: str) -> ChargeCostSettings | None:
    user_id = owner_user_id(hashed_vin)
    if user_id is not None:
        found = ChargeCostSettings.objects.filter(user_id=user_id).first()
        if found:
            return found
    return ChargeCostSettings.objects.order_by("pk").first()


def _matching_place(
    hashed_vin: str,
    lat: float | None,
    lon: float | None,
) -> ChargePlace | None:
    """Closest named geofence for this vehicle's owner (any tag)."""
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    user_id = owner_user_id(hashed_vin)
    places = (
        ChargePlace.objects.filter(user_id=user_id)
        if user_id is not None
        else ChargePlace.objects.all()
    )
    best = None
    best_d = None
    for place in places:
        dist = _haversine_m(lat_f, lon_f, place.latitude, place.longitude)
        if dist <= float(place.radius_m) and (best_d is None or dist < best_d):
            best_d = dist
            best = place
    return best


def _matching_role(
    hashed_vin: str,
    role: str,
    when: datetime,
    lat: float | None,
    lon: float | None,
) -> tuple[ChargePlace, VehiclePlaceRole] | None:
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    day = _local_date(when)
    roles = (
        VehiclePlaceRole.objects.filter(hashed_vin=hashed_vin, role=role)
        .select_related("place")
    )
    best = None
    best_d = None
    for assignment in roles:
        if not _in_date_range(day, assignment.valid_from, assignment.valid_to):
            continue
        place = assignment.place
        dist = _haversine_m(lat_f, lon_f, place.latitude, place.longitude)
        if dist > float(place.radius_m):
            continue
        if best_d is None or dist < best_d:
            best_d = dist
            best = (place, assignment)
    return best


def _tariff_at(place: ChargePlace, when: datetime) -> PlaceTariffPeriod | None:
    day = _local_date(when)
    periods = list(place.tariff_periods.all())
    matching = [
        period
        for period in periods
        if _in_date_range(day, period.valid_from, period.valid_to)
    ]
    if not matching:
        return None
    matching.sort(key=lambda p: p.valid_from, reverse=True)
    return matching[0]


def _session_kwh(session: ChargeSession) -> float | None:
    if session.kwh is None:
        return None
    try:
        kwh = float(session.kwh)
    except (TypeError, ValueError):
        return None
    if kwh < 0:
        return None
    return kwh


def _kwh_for_known_rate(session: ChargeSession) -> float:
    """Tesla sometimes omits energy on a 1–2 min plug-in. A known rate still means 0 €."""
    kwh = _session_kwh(session)
    return kwh if kwh is not None else 0.0


def _energy_timeline(
    start: datetime,
    end: datetime,
    kwh: float,
    power_samples: Sequence[tuple[datetime, float]],
) -> list[tuple[datetime, datetime, float]]:
    """
    Non-overlapping (t0, t1, kwh) slices covering [start, end].

    Power samples are piecewise-constant then scaled so the sum equals kwh.
    Without samples, one uniform slice.
    """
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        end = start + timedelta(seconds=1)

    if not power_samples:
        return [(start, end, kwh)]

    points: list[tuple[datetime, float]] = []
    for stamp, power in power_samples:
        try:
            kw = float(power)
        except (TypeError, ValueError):
            continue
        if kw < 0:
            continue
        points.append((_as_utc(stamp), kw))
    points.sort(key=lambda item: item[0])
    if not points:
        return [(start, end, kwh)]

    raw: list[tuple[datetime, datetime, float]] = []
    cursor = start
    first_t, first_p = points[0]
    if first_t > start:
        hours = (min(first_t, end) - start).total_seconds() / 3600.0
        if hours > 0:
            raw.append((start, min(first_t, end), first_p * hours))
        cursor = min(first_t, end)
    for index, (stamp, power) in enumerate(points):
        if stamp >= end:
            break
        next_t = points[index + 1][0] if index + 1 < len(points) else end
        slice_end = min(max(stamp, cursor), end)
        # From max(stamp, cursor) to min(next_t, end)
        t0 = max(stamp, cursor)
        t1 = min(next_t, end)
        if t1 <= t0:
            continue
        hours = (t1 - t0).total_seconds() / 3600.0
        raw.append((t0, t1, power * hours))
        cursor = t1
    if cursor < end:
        last_p = points[-1][1]
        hours = (end - cursor).total_seconds() / 3600.0
        if hours > 0:
            raw.append((cursor, end, last_p * hours))

    raw_sum = sum(item[2] for item in raw)
    if raw_sum <= 0:
        return [(start, end, kwh)]
    scale = kwh / raw_sum
    return [(t0, t1, energy * scale) for t0, t1, energy in raw if energy * scale > 0]


def _split_at(
    slices: list[tuple[datetime, datetime, float]],
    split_at: datetime,
) -> list[tuple[datetime, datetime, float]]:
    split_at = _as_utc(split_at)
    out: list[tuple[datetime, datetime, float]] = []
    for t0, t1, energy in slices:
        if split_at <= t0 or split_at >= t1:
            out.append((t0, t1, energy))
            continue
        span = (t1 - t0).total_seconds()
        if span <= 0:
            continue
        left = (split_at - t0).total_seconds() / span
        out.append((t0, split_at, energy * left))
        out.append((split_at, t1, energy * (1.0 - left)))
    return out


def _night_boundaries(
    start: datetime,
    end: datetime,
    night_start: time,
    night_end: time,
) -> list[datetime]:
    """Local night_start / night_end instants that fall strictly inside (start, end)."""
    start = _as_utc(start)
    end = _as_utc(end)
    local_start = _local(start)
    day0 = local_start.date() - timedelta(days=1)
    day1 = _local(end).date() + timedelta(days=1)
    bounds: list[datetime] = []
    day = day0
    while day <= day1:
        for clock in (night_start, night_end):
            local_hit = datetime.combine(day, clock, tzinfo=CHARGE_COST_TZ)
            utc_hit = local_hit.astimezone(ZoneInfo("UTC"))
            if start < utc_hit < end:
                bounds.append(utc_hit)
        day += timedelta(days=1)
    return sorted(set(bounds))


def _price_day_night(
    session: ChargeSession,
    tariff: PlaceTariffPeriod,
) -> CostResult:
    kwh = _kwh_for_known_rate(session)
    day_p = tariff.day_eur_per_kwh
    night_p = tariff.night_eur_per_kwh
    if day_p is None or night_p is None:
        return CostResult(None, COST_UNPRICED, RULE_HOME_DAY_NIGHT, place_id=tariff.place_id)
    slices = _energy_timeline(
        session.start, session.end, kwh, session.power_samples
    )
    for bound in _night_boundaries(
        session.start, session.end, tariff.night_start, tariff.night_end
    ):
        slices = _split_at(slices, bound)
    total = 0.0
    for t0, _t1, energy in slices:
        rate = night_p if _is_night(_local(t0), tariff.night_start, tariff.night_end) else day_p
        total += energy * float(rate)
    return CostResult(
        total,
        COST_PRICED,
        RULE_HOME_DAY_NIGHT,
        place_id=tariff.place_id,
        priced_kwh=kwh,
        missing_kwh=0.0,
    )


def spot_eur_mwh_at(when: datetime) -> tuple[float, int] | None:
    """Covering Elia MTU: (price €/MWh, resolution minutes), or None."""
    when = _as_utc(when)
    row = (
        DayAheadSpotPrice.objects.filter(mtu_start__lte=when)
        .order_by("-mtu_start")
        .first()
    )
    if row is None:
        return None
    end = row.mtu_start + timedelta(minutes=int(row.resolution_minutes))
    if when >= end:
        return None
    return float(row.price_eur_mwh), int(row.resolution_minutes)


def _mtu_boundaries(start: datetime, end: datetime) -> list[datetime]:
    start = _as_utc(start)
    end = _as_utc(end)
    rows = list(
        DayAheadSpotPrice.objects.filter(
            mtu_start__gt=start - timedelta(hours=2),
            mtu_start__lt=end + timedelta(hours=2),
        ).order_by("mtu_start")
    )
    bounds: list[datetime] = []
    for row in rows:
        stamp = _as_utc(row.mtu_start)
        if start < stamp < end:
            bounds.append(stamp)
        mtu_end = stamp + timedelta(minutes=int(row.resolution_minutes))
        if start < mtu_end < end:
            bounds.append(mtu_end)
    return sorted(set(bounds))


def _price_dynamic(
    session: ChargeSession,
    tariff: PlaceTariffPeriod,
) -> CostResult:
    kwh = _kwh_for_known_rate(session)
    cents = tariff.dynamic_surcharge_cents
    if cents is None:
        return CostResult(None, COST_UNPRICED, RULE_HOME_DYNAMIC, place_id=tariff.place_id)
    if kwh <= 0:
        return CostResult(
            0.0,
            COST_PRICED,
            RULE_HOME_DYNAMIC,
            place_id=tariff.place_id,
            priced_kwh=0.0,
            missing_kwh=0.0,
        )
    surcharge = float(cents) / 100.0
    slices = _energy_timeline(
        session.start, session.end, kwh, session.power_samples
    )
    for bound in _mtu_boundaries(session.start, session.end):
        slices = _split_at(slices, bound)
    total = 0.0
    priced = 0.0
    missing = 0.0
    for t0, _t1, energy in slices:
        spot = spot_eur_mwh_at(t0)
        if spot is None:
            missing += energy
            continue
        eur_mwh, _res = spot
        total += energy * (eur_mwh / 1000.0 + surcharge)
        priced += energy
    if priced <= 0:
        return CostResult(
            None,
            COST_UNPRICED,
            RULE_HOME_DYNAMIC,
            place_id=tariff.place_id,
            priced_kwh=0.0,
            missing_kwh=missing or kwh,
        )
    status = COST_PARTIAL if missing > 1e-9 else COST_PRICED
    return CostResult(
        total,
        status,
        RULE_HOME_DYNAMIC,
        place_id=tariff.place_id,
        priced_kwh=priced,
        missing_kwh=missing,
    )


def _price_flat(
    session: ChargeSession,
    rate: float,
    rule: str,
    place_id: int | None,
) -> CostResult:
    kwh = _kwh_for_known_rate(session)
    return CostResult(
        kwh * float(rate),
        COST_PRICED,
        rule,
        place_id=place_id,
        priced_kwh=kwh,
        missing_kwh=0.0,
    )


def _unpriced_at(place: ChargePlace, role: str) -> CostResult:
    """Inside a tagged geofence with no tariff on that date: still that place, not 'other'."""
    if role == ROLE_WORK:
        rule = RULE_WORK
    elif role == ROLE_HOME:
        periods = list(place.tariff_periods.all())
        latest = max(periods, key=lambda p: p.valid_from) if periods else None
        if latest is None:
            rule = RULE_HOME_FLAT
        elif latest.mode == TARIFF_DYNAMIC:
            rule = RULE_HOME_DYNAMIC
        elif latest.mode == TARIFF_DAY_NIGHT:
            rule = RULE_HOME_DAY_NIGHT
        elif latest.mode == TARIFF_PER_DAY:
            rule = RULE_PER_DAY
        else:
            rule = RULE_HOME_FLAT
    else:
        rule = RULE_PLACE
    return CostResult(None, COST_UNPRICED, rule, place_id=place.id)


def _price_home_or_work(
    session: ChargeSession,
    place: ChargePlace,
    role: str,
) -> CostResult | None:
    tariff = _tariff_at(place, session.start)
    if tariff is None:
        return None
    if tariff.mode == TARIFF_PER_DAY:
        if tariff.eur_per_day is None:
            return None
        return CostResult(
            float(tariff.eur_per_day),
            COST_PRICED,
            RULE_PER_DAY,
            place.id,
            priced_kwh=_kwh_for_known_rate(session),
            missing_kwh=0.0,
        )
    if role == ROLE_WORK:
        # Work uses the place rate: flat if set, else day rate, else night.
        rate = tariff.flat_eur_per_kwh
        if rate is None:
            rate = tariff.day_eur_per_kwh
        if rate is None:
            rate = tariff.night_eur_per_kwh
        if rate is None:
            return None
        return _price_flat(session, rate, RULE_WORK, place.id)
    if tariff.mode == TARIFF_FLAT:
        if tariff.flat_eur_per_kwh is None:
            return None
        rule = RULE_HOME_FLAT if role == ROLE_HOME else RULE_PLACE
        if role == ROLE_WORK:
            rule = RULE_WORK
        return _price_flat(session, tariff.flat_eur_per_kwh, rule, place.id)
    if tariff.mode == TARIFF_DAY_NIGHT:
        return _price_day_night(session, tariff)
    if tariff.mode == TARIFF_DYNAMIC:
        return _price_dynamic(session, tariff)
    return None


def price_session(session: ChargeSession) -> CostResult:
    """Apply the rule chain. Does not write the database."""
    if session.tesla_invoice_eur is not None:
        try:
            amount = float(session.tesla_invoice_eur)
        except (TypeError, ValueError):
            amount = None
        if amount is not None:
            return CostResult(
                amount,
                COST_PRICED,
                RULE_SUPERCHARGER_INVOICE,
                priced_kwh=_session_kwh(session),
                missing_kwh=0.0,
            )

    home = _matching_role(
        session.hashed_vin, ROLE_HOME, session.start, session.lat, session.lon
    )
    if home:
        priced = _price_home_or_work(session, home[0], ROLE_HOME)
        if priced is not None:
            return priced
        return _unpriced_at(home[0], ROLE_HOME)

    work = _matching_role(
        session.hashed_vin, ROLE_WORK, session.start, session.lat, session.lon
    )
    if work:
        priced = _price_home_or_work(session, work[0], ROLE_WORK)
        if priced is not None:
            return priced
        return _unpriced_at(work[0], ROLE_WORK)

    named = _matching_place(session.hashed_vin, session.lat, session.lon)
    if named:
        priced = _price_home_or_work(session, named, "other")
        if priced is not None:
            if priced.rule in (RULE_HOME_DAY_NIGHT, RULE_HOME_DYNAMIC, RULE_HOME_FLAT):
                priced.rule = RULE_PLACE
            return priced
        return _unpriced_at(named, "other")

    settings = settings_for_hashed_vin(session.hashed_vin)
    if session.is_supercharger and settings and settings.supercharger_eur_per_kwh is not None:
        return _price_flat(
            session,
            settings.supercharger_eur_per_kwh,
            RULE_SUPERCHARGER_RATE,
            None,
        )
    if settings and settings.other_eur_per_kwh is not None:
        return _price_flat(
            session,
            settings.other_eur_per_kwh,
            RULE_OTHER,
            None,
        )
    return CostResult(None, COST_UNPRICED, RULE_UNPRICED)


def persist_session_cost(session: ChargeSession, result: CostResult) -> ChargeSessionCost:
    """Insert or update the row keyed by (hashed_vin, start). Keeps tesla invoice if set."""
    defaults = {
        "end": _as_utc(session.end),
        "kwh": session.kwh,
        "latitude": session.lat,
        "longitude": session.lon,
        "place_id": result.place_id,
        "rule": result.rule,
        "status": result.status,
        "cost_eur": result.cost_eur,
    }
    if session.tesla_invoice_eur is not None:
        defaults["tesla_invoice_eur"] = session.tesla_invoice_eur
    if session.tesla_session_id:
        defaults["tesla_session_id"] = session.tesla_session_id
    obj, created = ChargeSessionCost.objects.update_or_create(
        hashed_vin=session.hashed_vin,
        start=_as_utc(session.start),
        defaults=defaults,
    )
    if not created and session.tesla_invoice_eur is None and obj.tesla_invoice_eur is not None:
        # Recalc must not drop a stored Tesla invoice.
        obj.rule = RULE_SUPERCHARGER_INVOICE
        obj.status = COST_PRICED
        obj.cost_eur = obj.tesla_invoice_eur
        obj.save(update_fields=["rule", "status", "cost_eur"])
        result.cost_eur = obj.cost_eur
        result.status = obj.status
        result.rule = obj.rule
    return obj


def _brussels_day_bounds(when: datetime) -> tuple[datetime, datetime]:
    day = _local_date(when)
    start = datetime.combine(day, time.min, tzinfo=CHARGE_COST_TZ)
    return start, start + timedelta(days=1)


def _sibling_daily_fee_exists(session: ChargeSession, result: CostResult) -> bool:
    """True if another persisted session already holds this place's €/day."""
    if result.rule != RULE_PER_DAY or result.place_id is None:
        return False
    day_start, day_end = _brussels_day_bounds(session.start)
    return (
        ChargeSessionCost.objects.filter(
            hashed_vin=session.hashed_vin,
            place_id=result.place_id,
            rule=RULE_PER_DAY,
            start__gte=_as_utc(day_start),
            start__lt=_as_utc(day_end),
            cost_eur__gt=0,
        )
        .exclude(start=_as_utc(session.start))
        .exists()
    )


def apply_daily_place_fees(rows: list[tuple[ChargeSession, CostResult]]) -> None:
    """One pitch fee per place per Brussels civil day; later plug-ins that day are 0 €."""
    charged: set[tuple[int, date]] = set()
    for session, result in sorted(rows, key=lambda item: _as_utc(item[0].start)):
        if result.rule != RULE_PER_DAY or result.place_id is None:
            continue
        if result.cost_eur is None:
            continue
        key = (result.place_id, _local_date(session.start))
        if key in charged or _sibling_daily_fee_exists(session, result):
            result.cost_eur = 0.0
            result.status = COST_PRICED
        else:
            charged.add(key)


def price_and_persist(session: ChargeSession) -> CostResult:
    result = price_session(session)
    if result.rule == RULE_PER_DAY and _sibling_daily_fee_exists(session, result):
        result.cost_eur = 0.0
        result.status = COST_PRICED
    persist_session_cost(session, result)
    return result


def lookup_persisted_cost(
    hashed_vin: str,
    fragment_start: datetime,
    fragment_end: datetime,
) -> ChargeSessionCost | None:
    """Full session whose interval overlaps this civil-day fragment."""
    start = _as_utc(fragment_start)
    end = _as_utc(fragment_end)
    return (
        ChargeSessionCost.objects.filter(hashed_vin=hashed_vin)
        .filter(start__lt=end, end__gt=start)
        .order_by("start")
        .first()
    )


# Same gap as personalstats charge-session grouping.
_EXPAND_GAP = timedelta(minutes=30)
_EXPAND_PAD = timedelta(hours=16)


def expand_session_from_snapshots(
    hashed_vin: str,
    fragment_start: datetime,
    fragment_end: datetime,
    fragment_kwh: float | None,
    lat: float | None,
    lon: float | None,
) -> ChargeSession:
    """
    Widen a civil-day charge fragment to the full plug-in (30 min gap).

    Used so DayMap can show the same session total on both sides of midnight.
    """
    from django.db.models import Q

    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

    start = _as_utc(fragment_start)
    end = _as_utc(fragment_end)
    window = TeslaCarDataSnapshot.objects.filter(
        hashedVin=hashed_vin,
        Date__gte=start - _EXPAND_PAD,
        Date__lte=end + _EXPAND_PAD,
    ).filter(
        Q(charging_state__in=["Charging", "Starting"]) | Q(charger_power__gt=0.5)
    ).order_by("Date").values(
        "Date",
        "charge_energy_added",
        "latitude",
        "longitude",
        "charger_power",
        "charger_voltage",
        "charger_actual_current",
        "charger_phases",
    )

    groups: list[list[dict]] = []
    current: list[dict] = []
    for row in window:
        stamp = row.get("Date")
        if stamp is None:
            continue
        stamp = _as_utc(stamp)
        row = dict(row)
        row["t"] = stamp
        if current and stamp - current[-1]["t"] > _EXPAND_GAP:
            groups.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        groups.append(current)

    chosen = None
    for group in groups:
        g0, g1 = group[0]["t"], group[-1]["t"]
        if g0 <= end and g1 >= start:
            chosen = group
            break
    if chosen is None:
        return ChargeSession(
            hashed_vin=hashed_vin,
            start=start,
            end=end,
            kwh=fragment_kwh,
            lat=lat,
            lon=lon,
        )

    energies = []
    powers = []
    mid_lat, mid_lon = lat, lon
    gps = [
        (pt.get("latitude"), pt.get("longitude"))
        for pt in chosen
        if pt.get("latitude") is not None and pt.get("longitude") is not None
    ]
    if gps:
        mid_lat, mid_lon = gps[len(gps) // 2]
    for pt in chosen:
        energy = pt.get("charge_energy_added")
        if energy is not None:
            try:
                energy_f = float(energy)
            except (TypeError, ValueError):
                energy_f = None
            if energy_f is not None and energy_f > 0.05:
                energies.append(energy_f)
        power = pt.get("charger_power")
        try:
            power_f = float(power) if power is not None else None
        except (TypeError, ValueError):
            power_f = None
        if power_f is not None and power_f > 0.05:
            powers.append((pt["t"], power_f))
    kwh = max(energies) if energies else fragment_kwh
    return ChargeSession(
        hashed_vin=hashed_vin,
        start=chosen[0]["t"],
        end=chosen[-1]["t"],
        kwh=kwh,
        lat=mid_lat,
        lon=mid_lon,
        power_samples=powers,
        is_supercharger=_group_looks_dc(chosen),
    )


def _group_looks_dc(group: list[dict]) -> bool:
    peak = 0.0
    for pt in group:
        try:
            power = float(pt.get("charger_power") or 0)
        except (TypeError, ValueError):
            continue
        if power > peak:
            peak = power
    return peak >= 40.0


def _session_from_group(hashed_vin: str, group: list[dict]) -> ChargeSession:
    gps = [
        (pt.get("latitude"), pt.get("longitude"))
        for pt in group
        if pt.get("latitude") is not None and pt.get("longitude") is not None
    ]
    mid_lat = mid_lon = None
    if gps:
        mid_lat, mid_lon = gps[len(gps) // 2]
    energies = []
    powers = []
    for pt in group:
        energy = pt.get("charge_energy_added")
        if energy is not None:
            try:
                energy_f = float(energy)
            except (TypeError, ValueError):
                energy_f = None
            if energy_f is not None and energy_f > 0.05:
                energies.append(energy_f)
        power = pt.get("charger_power")
        try:
            power_f = float(power) if power is not None else None
        except (TypeError, ValueError):
            power_f = None
        if power_f is not None and power_f > 0.05:
            powers.append((pt["t"], power_f))
    kwh = max(energies) if energies else None
    return ChargeSession(
        hashed_vin=hashed_vin,
        start=group[0]["t"],
        end=group[-1]["t"],
        kwh=kwh,
        lat=mid_lat,
        lon=mid_lon,
        power_samples=powers,
        is_supercharger=_group_looks_dc(group),
    )


def iter_sessions_starting_in(
    hashed_vin: str,
    window_start: datetime,
    window_end: datetime,
) -> list[ChargeSession]:
    """Full plug-in sessions whose start falls in [window_start, window_end)."""
    from django.db.models import Q

    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

    start = _as_utc(window_start)
    end = _as_utc(window_end)
    rows = TeslaCarDataSnapshot.objects.filter(
        hashedVin=hashed_vin,
        Date__gte=start - _EXPAND_PAD,
        Date__lt=end + _EXPAND_PAD,
    ).filter(
        Q(charging_state__in=["Charging", "Starting"]) | Q(charger_power__gt=0.5)
    ).order_by("Date").values(
        "Date",
        "charge_energy_added",
        "latitude",
        "longitude",
        "charger_power",
    )
    groups: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        stamp = row.get("Date")
        if stamp is None:
            continue
        stamp = _as_utc(stamp)
        point = dict(row)
        point["t"] = stamp
        if current and stamp - current[-1]["t"] > _EXPAND_GAP:
            groups.append(current)
            current = [point]
        else:
            current.append(point)
    if current:
        groups.append(current)
    sessions = []
    for group in groups:
        if len(group) < 2 and (group[-1]["t"] - group[0]["t"]).total_seconds() < 60:
            continue
        session = _session_from_group(hashed_vin, group)
        if start <= session.start < end:
            sessions.append(session)
    return sessions


def price_sessions_starting_in(
    hashed_vin: str,
    window_start: datetime,
    window_end: datetime,
    *,
    supercharger_match=None,
) -> list[tuple[ChargeSession, CostResult]]:
    """Price every session starting in the window; persist costs."""
    from matesla.tesla_charging_history import (
        apply_tesla_invoices,
        ensure_tesla_invoices_for_window,
    )

    try:
        ensure_tesla_invoices_for_window(
            hashed_vin,
            window_start,
            window_end,
            backfill=True,
            max_extra_chunks=1,
        )
    except Exception:
        pass
    sessions = list(iter_sessions_starting_in(hashed_vin, window_start, window_end))
    for session in sessions:
        if supercharger_match and session.lat is not None and session.lon is not None:
            try:
                hit = supercharger_match(session.lat, session.lon)
            except Exception:
                hit = None
            if hit:
                session.is_supercharger = True
                if isinstance(hit, dict):
                    name = (hit.get("name") or "").strip()
                    if name and not session.tesla_site_name:
                        session.tesla_site_name = name
    apply_tesla_invoices(sessions)
    out = []
    for session in sessions:
        stored = ChargeSessionCost.objects.filter(
            hashed_vin=hashed_vin, start=session.start
        ).first()
        if stored and stored.tesla_invoice_eur is not None and session.tesla_invoice_eur is None:
            session.tesla_invoice_eur = stored.tesla_invoice_eur
            if stored.tesla_session_id and not session.tesla_session_id:
                session.tesla_session_id = stored.tesla_session_id
        out.append((session, price_session(session)))
    apply_daily_place_fees(out)
    for session, result in out:
        persist_session_cost(session, result)
    return out


def annotate_daymap_charges(
    hashed_vin: str,
    charges: list[dict],
    *,
    supercharger_for: dict[tuple[float, float], bool] | None = None,
) -> None:
    """
    Mutate DayMap charge dicts with cost_eur / cost_status / cost_is_session_total.

    Never raises: a pricing failure must not blank the day map.
    """
    for charge in charges:
        try:
            start = charge.get("start")
            end = charge.get("end")
            if start is None or end is None:
                continue
            lat, lon = charge.get("lat"), charge.get("lon")
            is_sc = bool(charge.get("is_supercharger"))
            if supercharger_for and lat is not None and lon is not None:
                is_sc = is_sc or bool(
                    supercharger_for.get((round(float(lat), 5), round(float(lon), 5)))
                )
            session = expand_session_from_snapshots(
                hashed_vin,
                start,
                end,
                charge.get("kwh_added"),
                lat,
                lon,
            )
            session.is_supercharger = is_sc
            stored = ChargeSessionCost.objects.filter(
                hashed_vin=hashed_vin,
                start=session.start,
            ).first()
            if stored and stored.tesla_invoice_eur is not None:
                session.tesla_invoice_eur = stored.tesla_invoice_eur
            result = price_and_persist(session)
            charge["cost_eur"] = result.cost_eur
            charge["cost_status"] = result.status
            charge["cost_rule"] = result.rule
            charge["cost_is_session_total"] = True
            charge["place_name"] = ""
            if result.place_id:
                named = ChargePlace.objects.filter(pk=result.place_id).only("name").first()
                if named:
                    charge["place_name"] = named.name
        except Exception:
            charge.setdefault("cost_eur", None)
            charge.setdefault("cost_status", COST_UNPRICED)
            charge.setdefault("cost_is_session_total", True)
            charge.setdefault("place_name", "")
