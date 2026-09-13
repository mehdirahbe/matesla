"""
Place / region search over TeslaCarDataSnapshot GPS.

Given a geocoder bbox and a civil date range (Europe/Brussels, same as DayMap),
return the distinct days the car was inside that box, plus a thinned trace for
the map. Does not use ChargePlace, address ILIKE, or active_route_destination.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.sqlite_guard import heavy_snapshot_read

PLACE_SEARCH_TZ = ZoneInfo("Europe/Brussels")
MAX_RANGE_DAYS = 400
MAP_MAX_POINTS = 2000
MAP_GAP = timedelta(minutes=30)
MAP_MIN_MOVE_M = 180.0
MILES_TO_KM = 1.609344


@dataclass(frozen=True)
class DayHit:
    day: date
    point_count: int
    km: float | None


def civil_bounds(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    """Inclusive civil days → half-open UTC-aware [start, end) in Brussels."""
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    start = datetime(
        start_day.year, start_day.month, start_day.day, 0, 0, 0, tzinfo=PLACE_SEARCH_TZ
    )
    end = datetime(
        end_day.year, end_day.month, end_day.day, 0, 0, 0, tzinfo=PLACE_SEARCH_TZ
    ) + timedelta(days=1)
    return start, end


def summer_bounds(year: int) -> tuple[date, date]:
    return date(year, 6, 21), date(year, 9, 22)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def range_too_long(start_day: date, end_day: date) -> bool:
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    return (end_day - start_day).days + 1 > MAX_RANGE_DAYS


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    angle = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371000.0 * asin(sqrt(min(1.0, angle)))


def _civil_day(when: datetime) -> date:
    if when.tzinfo is None:
        when = when.replace(tzinfo=PLACE_SEARCH_TZ)
    return when.astimezone(PLACE_SEARCH_TZ).date()


def _km_from_odometer(odo_min, odo_max) -> float | None:
    if odo_min is None or odo_max is None:
        return None
    try:
        delta_miles = float(odo_max) - float(odo_min)
    except (TypeError, ValueError):
        return None
    if delta_miles < 0:
        return None
    km = delta_miles * MILES_TO_KM
    if km < 0.05:
        return 0.0
    return km


def fetch_snapshots_in_bbox(
    hashed_vin: str,
    start_dt: datetime,
    end_dt: datetime,
    south: float,
    north: float,
    west: float,
    east: float,
) -> list[tuple]:
    """
    hashedVin + civil Date window + lat/lon box.

    Uses the existing (hashedVin, Date) index, then lat/lon filters — not a
    full-table scan and not the address cache.
    """
    queryset = (
        TeslaCarDataSnapshot.objects.filter(
            hashedVin=hashed_vin,
            Date__gte=start_dt,
            Date__lt=end_dt,
            latitude__gte=south,
            latitude__lte=north,
            longitude__gte=west,
            longitude__lte=east,
        )
        .order_by("Date")
        .values_list("Date", "latitude", "longitude", "odometer")
    )
    with heavy_snapshot_read():
        return list(queryset)


def days_from_snapshots(rows: list[tuple]) -> list[DayHit]:
    by_day: dict[date, list] = {}
    for when, lat, lon, odo in rows:
        if when is None or lat is None or lon is None:
            continue
        day = _civil_day(when)
        by_day.setdefault(day, []).append((when, lat, lon, odo))
    hits: list[DayHit] = []
    for day in sorted(by_day):
        samples = by_day[day]
        odos = [row[3] for row in samples if row[3] is not None]
        km = _km_from_odometer(min(odos), max(odos)) if odos else None
        if km is None and len(samples) >= 2:
            metres = 0.0
            prev = samples[0]
            for sample in samples[1:]:
                metres += _haversine_m(prev[1], prev[2], sample[1], sample[2])
                prev = sample
            km = metres / 1000.0 if metres >= 50 else 0.0
        hits.append(DayHit(day=day, point_count=len(samples), km=km))
    return hits


def _downsample(points: list, max_points: int) -> list:
    count = len(points)
    if count <= max_points or max_points < 2:
        return points
    step = (count - 1) / (max_points - 1)
    return [points[int(round(index * step))] for index in range(max_points)]


def snapshot_count_in_bbox(
    hashed_vin: str,
    start_dt: datetime,
    end_dt: datetime,
    south: float,
    north: float,
    west: float,
    east: float,
) -> int:
    with heavy_snapshot_read():
        return TeslaCarDataSnapshot.objects.filter(
            hashedVin=hashed_vin,
            Date__gte=start_dt,
            Date__lt=end_dt,
            latitude__gte=south,
            latitude__lte=north,
            longitude__gte=west,
            longitude__lte=east,
        ).count()


def order_hits_by_presence(hashed_vin, start_dt, end_dt, hits: list) -> list:
    """Prefer the geocoder hit that actually contains this car's GPS."""
    if not hits:
        return hits
    scored = []
    for hit in hits:
        count = snapshot_count_in_bbox(
            hashed_vin,
            start_dt,
            end_dt,
            hit.south,
            hit.north,
            hit.west,
            hit.east,
        )
        scored.append((count, hit))
    if not any(count > 0 for count, _hit in scored):
        return list(hits)
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _count, hit in scored]


def traces_from_snapshots(rows: list[tuple], max_points: int = MAP_MAX_POINTS) -> list[list]:
    """Polyline segments (and 1-point parked clusters) inside the bbox."""
    segments: list[list] = []
    current: list = []
    last_time = None
    last_lat = last_lon = None
    for when, lat, lon, _odo in rows:
        if when is None or lat is None or lon is None:
            continue
        try:
            latitude = float(lat)
            longitude = float(lon)
        except (TypeError, ValueError):
            continue
        if last_time is not None and (when - last_time) > MAP_GAP:
            if current:
                segments.append(current)
            current = []
            last_lat = last_lon = None
        if last_lat is not None:
            if _haversine_m(last_lat, last_lon, latitude, longitude) < MAP_MIN_MOVE_M:
                last_time = when
                continue
        current.append([round(latitude, 5), round(longitude, 5)])
        last_lat, last_lon, last_time = latitude, longitude, when
    if current:
        segments.append(current)
    total = sum(len(segment) for segment in segments)
    if total <= max_points:
        return segments
    # Even thin across all points, keep segment breaks
    budget = max_points
    thinned = []
    for segment in segments:
        share = max(1, int(round(budget * len(segment) / max(total, 1))))
        thinned.append(_downsample(segment, min(len(segment), share)))
    return thinned
