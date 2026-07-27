import django
from django.db.models import Max, Min, Avg, F, FloatField, Case, When
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.template import loader
from django.views.decorators.http import require_GET
from django_tables2 import SingleTableView
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure

from anonymisedstats.views import (
    PrepareCSVFromQuery,
    GetXandYFromBatteryDegradResult,
    GenerateScatterGraph,
    GeneratePngFromGraph,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.VinHash import IsValidHash
from django.utils.translation import gettext as _
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
import json
import re

# Create your views here.

# Return a dictionary with titles for fields
from personalstats.tables import TeslaFirmwareHistoryTable

# Graph keys that are not real DB columns (computed in the view)
COMPUTED_GRAPH_FIELDS = frozenset({"range_at_100", "range_at_100_odometer"})

# Calendar "day" for history maps: user mental model is local civil date, not UTC midnight
DAY_MAP_TZ = ZoneInfo("Europe/Brussels")
# Max points sent to the browser for the polyline (downsample long TeslaFi days)
DAY_MAP_MAX_POINTS = 800
# Min parked duration (minutes) to list a stop
DAY_MAP_STOP_MIN_MINUTES = 8
# Speed at or below this (mi/h) counts as stopped
DAY_MAP_STOP_SPEED = 1.0


def GetTitleForFieldDico():
    dico = {
        "outside_temp": _("Outside temperature (°C)"),
        "driver_temp_setting": _("Driver temperature (°C)"),
        "inside_temp": _("Inside temperature (°C)"),
        "passenger_temp_setting": _("Passenger temperature (°C)"),
        "odometer": _("Odometer (miles)"),
        # Tesla drive_state.speed is mph (same unit basis as odometer miles)
        "speed": _("Speed (mi/h)"),
        "latitude": _("Latitude"),
        "longitude": _("Longitude"),
        # Tesla drive_state.power is kW (negative when regenerating)
        "power": _("Power (kW)"),
        "battery_level": _("Battery level (%)"),
        "battery_range": _("Battery range (miles)"),
        "charge_limit_soc": _("Battery charge limit (%)"),
        # Tesla charge_rate is miles of range added per hour (not km/h or kW)
        "charge_rate": _("Charge rate (mi/h)"),
        "charger_actual_current": _("Charger actual current (A)"),
        "charger_phases": _("Charger phases"),
        "charger_power": _("Charger power (kW)"),
        "charger_voltage": _("Charger voltage (V)"),
        "est_battery_range": _("Estimated battery range (miles)"),
        "usable_battery_level": _("Usable battery level (%)"),
        "battery_degradation": _("Battery degradation (%)"),
        # Extrapolated full-charge range: battery_range / soc * 100
        # (wording avoids bare "%" — breaks gettext python-format matching)
        "range_at_100": _("Range at full charge (miles)"),
        "range_at_100_odometer": _("Range at full charge vs odometer (miles)"),
    }
    return dico


# Return a nice title for field
def GetTitleForField(field):
    if field is None:
        return field
    dico = GetTitleForFieldDico()
    if field in dico:
        return dico[field]
    # not found, return as is
    return field


def _range_at_100_from_entry(entry):
    """Rated range extrapolated to 100% SoC (miles). Same basis as degradation."""
    br = entry.battery_range
    level = entry.usable_battery_level
    if level is None or level <= 0:
        level = entry.battery_level
    if br is None or level is None or level <= 0:
        return None
    return float(br) / float(level) * 100.0


def GetXandYRangeAt100(results, xfield):
    """Scatter points: X = model field, Y = range at 100% SoC."""
    xvalues = []
    yvalues = []
    for entry in results:
        y = _range_at_100_from_entry(entry)
        if y is None:
            continue
        x = getattr(entry, xfield, None)
        if x is None:
            continue
        xvalues.append(x)
        yvalues.append(y)
    return xvalues, yvalues


def annotate_range_at_100(qs):
    """Add computed range_at_100 column (miles) for aggregation."""
    return qs.annotate(
        range_at_100=Case(
            When(
                usable_battery_level__gt=0,
                then=F("battery_range") * 100.0 / F("usable_battery_level"),
            ),
            When(
                battery_level__gt=0,
                then=F("battery_range") * 100.0 / F("battery_level"),
            ),
            default=None,
            output_field=FloatField(),
        )
    ).filter(range_at_100__isnull=False, battery_range__isnull=False)

def GenerateDateGraph(datesList, maxvalues, minvalues, avgvalues, title):
    # matplotlib 3.9+ removed Axes.plot_date — use plot() with date objects
    fig = Figure(figsize=[12, 5])

    language = django.utils.translation.get_language()
    if language is not None and language == 'fr':
        formatter = DateFormatter('%d/%m/%y')
    else:
        formatter = DateFormatter('%m/%d/%y')

    ax = fig.subplots()
    if datesList is not None and minvalues is not None and len(datesList) > 0:
        ax.plot(datesList, minvalues, linestyle='-', marker='o', markersize=3, label=_('Minimum'))
        ax.plot(datesList, avgvalues, linestyle='-', marker='o', markersize=3, label=_('Average'))
        ax.plot(datesList, maxvalues, linestyle='-', marker='o', markersize=3, label=_('Maximum'))
        ax.legend()
        ax.xaxis.set_major_formatter(formatter)
        # One day of data still plots fine; widen x-axis so a single point is not clipped
        if len(datesList) == 1:
            d = datesList[0]
            ax.set_xlim(d - timedelta(days=1), d + timedelta(days=1))
        ax.ticklabel_format(axis='y', useOffset=False, style='plain')
        fig.autofmt_xdate()
    fig.suptitle(title)
    return GeneratePngFromGraph(fig)


def GetDatesAndValuesFromGroupByDateResult(results):
    dates = list()
    maxvalues = list()
    minvalues = list()
    avgvalues = list()
    for entry in results:
        dates.append(entry['DateOnlyDay'])
        maxvalues.append(entry['max_val'])
        minvalues.append(entry['min_val'])
        avgvalues.append(entry['avg_val'])
    return dates, maxvalues, minvalues, avgvalues


# Check params and ensure that they are not a potential SQL injection
# return response + False if problem, None + True if fine
def SecurityChecks(hashedVin, desiredfield):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin), False
    # Check that it is one field from the TeslaCarDataSnapshot, or a computed graph key
    validFields = TeslaCarDataSnapshot.__dict__
    if desiredfield is None or (
        desiredfield not in validFields and desiredfield not in COMPUTED_GRAPH_FIELDS
    ):
        # means invalid desiredfield field was passed
        return HttpResponseNotFound(
            "Graph for this field doesn't exists " + (desiredfield or "")
        ), False
    return None, True


def _period_filter(qs, desiredperiod):
    """desiredperiod is expressed in weeks; 0 / None means all data."""
    if desiredperiod is not None and desiredperiod > 0:
        # most recent data
        mindate = datetime.now() - timedelta(weeks=desiredperiod)
        return qs.filter(DateOnlyDay__gte=mindate)
    return qs


# create a graph showing the evolution of field for a car identified by hashed
# vin.
# desiredperiod is expressed in weeks, 0 means all.
# allow to disable cache when improving graphs and you want a constant reload
# @never_cache
def StatsOnCarGraph(request, hashedVin, desiredfield, desiredperiod):
    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response
    title = GetTitleForField(desiredfield)
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    if not base.exists():
        return GenerateDateGraph(None, None, None, None, title)

    # range_at_100 is not a DB column: battery_range / SoC * 100 (full-charge miles)
    if desiredfield == "range_at_100":
        qs = annotate_range_at_100(_period_filter(base, desiredperiod))
        results = (
            qs.values("DateOnlyDay")
            .annotate(
                max_val=Max("range_at_100"),
                min_val=Min("range_at_100"),
                avg_val=Avg("range_at_100"),
            )
            .order_by("DateOnlyDay")
        )
    else:
        qs = _period_filter(base, desiredperiod)
        results = (
            qs.values("DateOnlyDay")
            .annotate(
                max_val=Max(desiredfield),
                min_val=Min(desiredfield),
                avg_val=Avg(desiredfield),
            )
            .order_by("DateOnlyDay")
        )

    dates, maxvalues, minvalues, avgvalues = GetDatesAndValuesFromGroupByDateResult(
        results
    )
    return GenerateDateGraph(dates, maxvalues, minvalues, avgvalues, title)

# Weeks values offered in the personal-stats period dropdown (1 Month = 4).
STATS_PERIOD_WEEKS = frozenset({1, 2, 4, 13, 26, 52, 104, 260, 520})
STATS_PERIOD_SESSION_KEY = "personalstats_period_weeks"
STATS_PERIOD_DEFAULT = 4


def parse_stats_period(raw, default=STATS_PERIOD_DEFAULT):
    """Return a valid period in weeks, or default."""
    try:
        weeks = int(raw)
    except (TypeError, ValueError):
        return default
    return weeks if weeks in STATS_PERIOD_WEEKS else default


def resolve_stats_period(request, *, persist=True):
    """
    Preferred stats graph window (weeks).
    Query ?period= wins, then session, then 1 month (4 weeks).
    """
    if request.GET.get("period") is not None:
        weeks = parse_stats_period(request.GET.get("period"))
    else:
        weeks = parse_stats_period(
            request.session.get(STATS_PERIOD_SESSION_KEY),
            default=STATS_PERIOD_DEFAULT,
        )
    if persist:
        request.session[STATS_PERIOD_SESSION_KEY] = weeks
    return weeks


def _vehicle_chrome_context(request, hashedVin):
    """Shared multi-vehicle selector context for personalstats pages."""
    context = {
        "hashedVin": hashedVin,
        "stats_period": resolve_stats_period(request),
    }
    user = request.user
    if user.is_authenticated:
        from matesla.TeslaConnect import list_user_vehicles, resolve_active_vehicle

        vehicles = list_user_vehicles(user)
        active = resolve_active_vehicle(user, request)
        context["user_vehicles"] = [
            {
                "api_id": v.api_id,
                "vin": v.vin,
                "display_name": v.display_name,
                "label": v.label,
                "state": v.state,
                "is_primary": v.is_primary,
            }
            for v in vehicles
        ]
        context["active_vehicle_api_id"] = active.api_id if active else None
        context["active_vehicle_label"] = active.label if active else None
    return context


# allow to disable cache when improving HTML and you want a constant reload
# @never_cache
def Stats(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    template = loader.get_template('personalstats/carstats.html')
    context = _vehicle_chrome_context(request, hashedVin)
    context.update(GetTitleForFieldDico())
    return HttpResponse(template.render(context, request))


def _parse_day_string(raw):
    """
    Accept ISO YYYY-MM-DD or European D/M/YYYY (also D-M-YYYY).
    Returns date or None.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # 2024-12-31
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 31/12/2024 or 31-12-2024
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", raw)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _downsample_indices(n, max_points):
    if n <= max_points or max_points < 2:
        return list(range(n))
    # always keep first and last
    idxs = {0, n - 1}
    step = (n - 1) / (max_points - 1)
    for i in range(1, max_points - 1):
        idxs.add(int(round(i * step)))
    return sorted(idxs)


def _point_kind(p):
    """Classify a sample: charge | drive | park."""
    cs = (p.get("charging_state") or "").strip().lower()
    cp = p.get("charger_power")
    if cs == "charging" or (cp is not None and cp > 0.5):
        return "charge"
    sh = (p.get("shift_state") or "").strip().upper()
    sp = p.get("speed")
    if sh in ("D", "R", "N") or (sp is not None and sp > DAY_MAP_STOP_SPEED):
        return "drive"
    return "park"


def _estimate_pack_kwh(epa_range_miles):
    """
    Rough usable pack size from EPA range (miles).
    ~220 Wh/mi fleet average; clamp to a sensible EV pack window.
    """
    if epa_range_miles and epa_range_miles > 50:
        kwh = float(epa_range_miles) * 0.22
        return max(40.0, min(120.0, kwh))
    return 75.0


def _soc(p):
    """Prefer usable_battery_level, else battery_level."""
    u = p.get("usable_battery_level")
    if u is not None:
        return float(u)
    b = p.get("battery_level")
    return float(b) if b is not None else None


def _segment_day(rows, pack_kwh):
    """
    Build chronological drive + charge segments with metrics.
    rows: full-day samples (GPS optional).
    """
    if not rows:
        return [], []

    # Group consecutive points of the same kind
    groups = []
    cur_kind = _point_kind(rows[0])
    cur = [rows[0]]
    for p in rows[1:]:
        k = _point_kind(p)
        if k == cur_kind:
            cur.append(p)
        else:
            groups.append((cur_kind, cur))
            cur_kind = k
            cur = [p]
    groups.append((cur_kind, cur))

    drives = []
    charges = []
    for kind, pts in groups:
        if len(pts) < 1:
            continue
        a, b = pts[0], pts[-1]
        seconds = max(0.0, (b["t"] - a["t"]).total_seconds())
        minutes = seconds / 60.0
        hours = seconds / 3600.0

        if kind == "drive":
            # Need a real movement span
            if minutes < 1.0 and len(pts) < 3:
                continue
            odo_a, odo_b = a.get("odometer"), b.get("odometer")
            miles = None
            if odo_a is not None and odo_b is not None and odo_b >= odo_a:
                miles = odo_b - odo_a
            # Tiny odo blips are noise
            if miles is not None and miles < 0.05 and minutes < 3:
                continue
            km = miles * 1.609344 if miles is not None else None
            soc_a, soc_b = _soc(a), _soc(b)
            soc_used = None
            if soc_a is not None and soc_b is not None:
                soc_used = soc_a - soc_b
            kwh_used = None
            if soc_used is not None and soc_used > 0:
                kwh_used = soc_used / 100.0 * pack_kwh
            kwh_per_100km = None
            if kwh_used is not None and km is not None and km > 0.2:
                kwh_per_100km = kwh_used / km * 100.0
            avg_mph = (miles / hours) if (miles is not None and hours > 0.01) else None
            avg_kmh = (km / hours) if (km is not None and hours > 0.01) else None
            drives.append(
                {
                    "kind": "drive",
                    "start": a["t"],
                    "end": b["t"],
                    "start_local": a["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
                    "end_local": b["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
                    "minutes": int(round(minutes)),
                    "miles": miles,
                    "km": km,
                    "avg_mph": avg_mph,
                    "avg_kmh": avg_kmh,
                    "soc_start": soc_a,
                    "soc_end": soc_b,
                    "soc_used": soc_used,
                    "kwh_used": kwh_used,
                    "kwh_per_100km": kwh_per_100km,
                    "lat": a.get("lat"),
                    "lon": a.get("lon"),
                    "end_lat": b.get("lat"),
                    "end_lon": b.get("lon"),
                }
            )
        elif kind == "charge":
            if minutes < 1.0 and len(pts) < 2:
                continue
            soc_a, soc_b = _soc(a), _soc(b)
            soc_added = None
            if soc_a is not None and soc_b is not None:
                soc_added = soc_b - soc_a
            e_a, e_b = a.get("charge_energy_added"), b.get("charge_energy_added")
            kwh_added = None
            if e_a is not None and e_b is not None and e_b >= e_a:
                kwh_added = e_b - e_a
            elif soc_added is not None and soc_added > 0:
                kwh_added = soc_added / 100.0 * pack_kwh
            powers = [p["charger_power"] for p in pts if p.get("charger_power") is not None]
            max_power = max(powers) if powers else None
            # mid GPS for address
            gps_pts = [p for p in pts if p.get("lat") is not None and p.get("lon") is not None]
            mid = gps_pts[len(gps_pts) // 2] if gps_pts else a
            charges.append(
                {
                    "kind": "charge",
                    "start": a["t"],
                    "end": b["t"],
                    "start_local": a["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
                    "end_local": b["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
                    "minutes": int(round(minutes)),
                    "soc_start": soc_a,
                    "soc_end": soc_b,
                    "soc_added": soc_added,
                    "kwh_added": kwh_added,
                    "max_power_kw": max_power,
                    "lat": mid.get("lat"),
                    "lon": mid.get("lon"),
                }
            )

    return drives, charges


def DayMap(request, hashedVin, day=None):
    """
    Map of GPS track for one civil day (Europe/Brussels) + drive/charge stats.

    UX goal vs TeslaFi: type 31/12/2024 (or use HTML date picker) + prev/next day.
    """
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)

    # Resolve day: path / GET date= / default today (Brussels)
    raw = day or request.GET.get("date") or request.POST.get("date") or ""
    chosen = _parse_day_string(raw)
    parse_error = None
    if raw and chosen is None:
        parse_error = _("Invalid date. Use DD/MM/YYYY or YYYY-MM-DD.")
    if chosen is None:
        chosen = datetime.now(DAY_MAP_TZ).date()

    # Canonical bookmarkable URL when date came only as ?date=
    if day is None and request.GET.get("date") and chosen is not None and parse_error is None:
        from django.shortcuts import redirect

        return redirect("PersoDayMapDay", hashedVin=hashedVin, day=chosen.isoformat())

    day_start = datetime(chosen.year, chosen.month, chosen.day, 0, 0, 0, tzinfo=DAY_MAP_TZ)
    day_end = day_start + timedelta(days=1)
    prev_day = chosen - timedelta(days=1)
    next_day = chosen + timedelta(days=1)

    # Full telemetry for the day (GPS may be missing on some charge samples)
    qs = (
        TeslaCarDataSnapshot.objects.filter(
            hashedVin=hashedVin,
            Date__gte=day_start,
            Date__lt=day_end,
        )
        .order_by("Date")
        .only(
            "Date",
            "vin",
            "latitude",
            "longitude",
            "speed",
            "odometer",
            "shift_state",
            "battery_level",
            "usable_battery_level",
            "charging_state",
            "charger_power",
            "charge_energy_added",
            "battery_range",
        )
    )

    raw_rows = []
    vin = None
    for s in qs.iterator(chunk_size=2000):
        if vin is None and s.vin:
            vin = s.vin
        raw_rows.append(
            {
                "t": s.Date,
                "lat": float(s.latitude) if s.latitude is not None else None,
                "lon": float(s.longitude) if s.longitude is not None else None,
                "speed": float(s.speed) if s.speed is not None else None,
                "odometer": float(s.odometer) if s.odometer is not None else None,
                "shift_state": s.shift_state,
                "battery_level": float(s.battery_level)
                if s.battery_level is not None
                else None,
                "usable_battery_level": float(s.usable_battery_level)
                if s.usable_battery_level is not None
                else None,
                "charging_state": s.charging_state,
                "charger_power": float(s.charger_power)
                if s.charger_power is not None
                else None,
                "charge_energy_added": float(s.charge_energy_added)
                if s.charge_energy_added is not None
                else None,
            }
        )

    # Map polyline: GPS only
    gps_rows = [p for p in raw_rows if p["lat"] is not None and p["lon"] is not None]
    total_points = len(gps_rows)
    idxs = _downsample_indices(total_points, DAY_MAP_MAX_POINTS)
    path = [
        {
            "lat": gps_rows[i]["lat"],
            "lon": gps_rows[i]["lon"],
            "t": gps_rows[i]["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M:%S"),
            "speed": gps_rows[i]["speed"],
        }
        for i in idxs
    ]

    # Pack size for kWh estimates
    epa = None
    if vin:
        from matesla.models.TeslaCarInfo import TeslaCarInfo

        info = TeslaCarInfo.objects.filter(vin=vin).first()
        if info and info.EPARange:
            epa = info.EPARange
    pack_kwh = _estimate_pack_kwh(epa)

    drives, charges = _segment_day(raw_rows, pack_kwh)

    # Page render: cache only (instant). Missing labels are filled by JS via
    # ResolveAddress → GetAddressFromLatLong (rate-limited Nominatim).
    from matesla.models.AddressFromLatLong import LookupCachedAddress

    def addr_cached(lat, lon):
        if lat is None or lon is None:
            return None
        try:
            return LookupCachedAddress(round(float(lat), 4), round(float(lon), 4))
        except Exception:
            return None

    start_addr = end_addr = None
    start_lat = start_lon = end_lat = end_lon = None
    if gps_rows:
        start_lat, start_lon = gps_rows[0]["lat"], gps_rows[0]["lon"]
        end_lat, end_lon = gps_rows[-1]["lat"], gps_rows[-1]["lon"]
        start_addr = addr_cached(start_lat, start_lon)
        end_addr = addr_cached(end_lat, end_lon)
    for d in drives:
        d["start_address"] = addr_cached(d.get("lat"), d.get("lon"))
        d["end_address"] = addr_cached(d.get("end_lat"), d.get("end_lon"))
    for c in charges:
        c["address"] = addr_cached(c.get("lat"), c.get("lon"))

    # Day totals from drives (same metrics as the drives table)
    miles_driven = sum(d["miles"] or 0 for d in drives) or None
    miles_driven_km = sum(d["km"] or 0 for d in drives) or None
    if miles_driven == 0:
        miles_driven = None
    if miles_driven_km == 0:
        miles_driven_km = None
    drive_hours = sum(
        max(0.0, (d["end"] - d["start"]).total_seconds()) / 3600.0 for d in drives
    )
    day_avg_mph = (
        miles_driven / drive_hours
        if miles_driven is not None and drive_hours > 0.01
        else None
    )
    day_avg_kmh = (
        miles_driven_km / drive_hours
        if miles_driven_km is not None and drive_hours > 0.01
        else None
    )
    day_soc_start = drives[0].get("soc_start") if drives else None
    day_soc_end = drives[-1].get("soc_end") if drives else None
    soc_used_vals = [d["soc_used"] for d in drives if d.get("soc_used") is not None]
    day_soc_used = sum(soc_used_vals) if soc_used_vals else None
    kwh_used_vals = [d["kwh_used"] for d in drives if d.get("kwh_used") is not None]
    day_kwh_used = sum(kwh_used_vals) if kwh_used_vals else None
    day_kwh_per_100km = None
    if (
        day_kwh_used is not None
        and miles_driven_km is not None
        and miles_driven_km > 0.2
    ):
        day_kwh_per_100km = day_kwh_used / miles_driven_km * 100.0

    # Unified timeline for the template (drives + charges, chronological)
    timeline = sorted(
        [{"type": "drive", **d} for d in drives]
        + [{"type": "charge", **c} for c in charges],
        key=lambda x: x["start"],
    )

    context = _vehicle_chrome_context(request, hashedVin)
    context.update(
        {
            "day": chosen,
            "day_iso": chosen.isoformat(),
            "day_display": chosen.strftime("%d/%m/%Y"),
            "prev_day_iso": prev_day.isoformat(),
            "next_day_iso": next_day.isoformat(),
            "tz_name": "Europe/Brussels",
            "total_points": total_points,
            "path_points": len(path),
            "path_json": json.dumps(path),
            "drives": drives,
            "charges": charges,
            "timeline": timeline,
            "start_addr": start_addr,
            "end_addr": end_addr,
            "start_lat": start_lat,
            "start_lon": start_lon,
            "end_lat": end_lat,
            "end_lon": end_lon,
            "start_time": (
                gps_rows[0]["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M:%S")
                if gps_rows
                else None
            ),
            "end_time": (
                gps_rows[-1]["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M:%S")
                if gps_rows
                else None
            ),
            "miles_driven": miles_driven,
            "miles_driven_km": miles_driven_km,
            "day_avg_mph": day_avg_mph,
            "day_avg_kmh": day_avg_kmh,
            "day_soc_start": day_soc_start,
            "day_soc_end": day_soc_end,
            "day_soc_used": day_soc_used,
            "day_kwh_per_100km": day_kwh_per_100km,
            "pack_kwh_estimate": pack_kwh,
            "parse_error": parse_error,
            "has_track": total_points > 0 or len(raw_rows) > 0,
            "has_gps": total_points > 0,
        }
    )
    return render(request, "personalstats/daymap.html", context)


@require_GET
def ResolveAddress(request):
    """
    Async reverse-geocode for DayMap (and similar).

    Query: ?lat=50.7868&lon=4.3517
    Returns JSON: {ok, lat, lon, address, cached, error?}
    Cache hits are instant; misses call Nominatim under the daily/1-req-s quota.
    """
    try:
        lat = float(request.GET.get("lat", ""))
        lon = float(request.GET.get("lon", ""))
    except (TypeError, ValueError):
        return JsonResponse(
            {"ok": False, "error": "invalid_coords"}, status=400
        )
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return JsonResponse({"ok": False, "error": "out_of_range"}, status=400)

    la, lo = round(lat, 4), round(lon, 4)
    from matesla.models.AddressFromLatLong import (
        LookupCachedAddress,
        GetAddressFromLatLong,
    )

    cached = LookupCachedAddress(la, lo)
    if cached is not None:
        return JsonResponse(
            {"ok": True, "lat": la, "lon": lo, "address": cached, "cached": True}
        )

    address = GetAddressFromLatLong(la, lo)
    if not address or address == "Unknown":
        return JsonResponse(
            {
                "ok": False,
                "lat": la,
                "lon": lo,
                "address": None,
                "cached": False,
                "error": "unresolved_or_quota",
            }
        )
    return JsonResponse(
        {"ok": True, "lat": la, "lon": lo, "address": address, "cached": False}
    )


# returns data stored in db for the user is CSV-->the only info from the car
# we need is the vin to filter results
def view_AllMyDataAsCSV(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    if TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count() == 0:
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    query = "select * from matesla_teslacardatasnapshot where \"hashedVin\"='" + hashedVin + "';"
    return PrepareCSVFromQuery(query)


def BatteryDegradationGraph(request, hashedVin, desiredfield, desiredperiod=0):
    """
    Scatter graphs for battery health tab.

    - odometer: X=odometer, Y=battery_degradation (%)
    - range_at_100_odometer: X=odometer, Y=extrapolated range at 100% SoC (miles)

    desiredperiod is weeks (0 = all), same meaning as StatsOnCarGraph / #DesiredPeriod.
    """
    # Computed scatter (Y = range at 100%), not a real model field on X axis alone
    if desiredfield == "range_at_100_odometer":
        if not IsValidHash(hashedVin):
            # means invalid hashedVin field was passed
            return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
        title = GetTitleForField(desiredfield)
        qs = _period_filter(
            TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin), desiredperiod
        )
        if not qs.exists():
            return GenerateScatterGraph(None, None, title)
        # random sample for long TeslaFi histories (still bounded; uses randomNr index)
        results = qs.order_by("randomNr")[:2000]
        xvalues, yvalues = GetXandYRangeAt100(results, "odometer")
        return GenerateScatterGraph(xvalues, yvalues, title)

    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response

    title = GetTitleForField(desiredfield)
    qs = _period_filter(
        TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin), desiredperiod
    )
    if not qs.exists():
        return GenerateScatterGraph(None, None, title)

    # see in anonymous stats for random samples
    results = qs.order_by("randomNr")[:2000]
    xvalues, yvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    return GenerateScatterGraph(xvalues, yvalues, title)

# returns page with firmware history for the car
class FirmwareHistoryView(SingleTableView):
    model = TeslaFirmwareHistory
    table_class = TeslaFirmwareHistoryTable
    template_name = 'personalstats/FirmwareHistory.html'


# Display page with car firmware history
def FirmwareHistory(request, hashedVin):
    # see https://django-tables2.readthedocs.io/en/latest/pages/table-data.html
    table = TeslaFirmwareHistoryTable(TeslaFirmwareHistory.objects.filter(hashedVin=hashedVin))
    return render(request, 'personalstats/FirmwareHistory.html',
                  {'table': table, 'hashedVin': hashedVin})


# returns CSV with firmware history for the car
def FirmwareHistoryCSV(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    if TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count() == 0:
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    query = "select \"Version\",\"Date\" from matesla_TeslaFirmwareHistory where \"hashedVin\"='" + hashedVin + "' order by 2 desc;"
    return PrepareCSVFromQuery(query)
