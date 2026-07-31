import django
from django.db.models import Max, Min, Avg, F, FloatField, Case, When, Q
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.template import loader
from django.views.decorators.http import require_GET
from django_tables2 import SingleTableView
from matplotlib.dates import DateFormatter

from anonymisedstats.views import (
    PrepareCSVFromQuery,
    GetXandYFromBatteryDegradResult,
    GenerateScatterGraph,
    GeneratePngFromGraph,
    degradation_scatter_queryset,
    aggregate_scatter_daily_median,
    DEGRADATION_SCATTER_FALLBACK_SOC,
)
from matesla.graphstyle import (
    ACCENT,
    ACCENT_SOFT,
    DANGER,
    ENERGY,
    MUTED,
    SERIES_COLORS,
    TEXT,
    WARM,
    finish_figure,
    graph_size_from_request,
    make_figure,
    style_axes,
    style_legend,
    style_suptitle,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.VinHash import IsValidHash
from django.utils.translation import gettext as _
from datetime import date, timedelta, datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo
import json
import re

# Create your views here.

# Return a dictionary with titles for fields
from personalstats.tables import TeslaFirmwareHistoryTable

# Graph keys that are not real DB columns (computed in the view)
COMPUTED_GRAPH_FIELDS = frozenset(
    {
        "range_at_100",
        "range_at_100_odometer",
        "efficiency_by_speed",
        "efficiency_by_temp",
    }
)

# Calendar "day" for history maps: user mental model is local civil date, not UTC midnight
DAY_MAP_TZ = ZoneInfo("Europe/Brussels")
# Max points sent to the browser for the polyline (downsample long TeslaFi days)
DAY_MAP_MAX_POINTS = 800
# Min parked duration (minutes) to list a stop
DAY_MAP_STOP_MIN_MINUTES = 8
# Speed at or below this (mi/h) counts as stopped
DAY_MAP_STOP_SPEED = 1.0
# Efficiency charts (inspired by TeslaFi; drives ≥ 10 km)
EFFICIENCY_MIN_KM = 10.0
EFFICIENCY_MIN_PCT = 25.0
EFFICIENCY_MAX_PCT = 140.0
EFFICIENCY_SPEED_BIN_KMH = 5
EFFICIENCY_TEMP_BIN_C = 5
# Gap longer than this between drive samples starts a new trip
EFFICIENCY_TRIP_GAP = timedelta(minutes=15)
# Cap drive samples for binning (enough for stable histograms; not raw time series)
EFFICIENCY_MAX_DRIVE_ROWS = 20000
# Lifetime map (stats page): path + summary KPIs for the selected period
LIFETIME_MAP_MAX_POINTS = 5000
# Split polyline when consecutive drive samples are farther apart than this
LIFETIME_MAP_GAP = timedelta(minutes=30)
# Ignore GPS jitter while "driving" but barely moving (~180 m)
LIFETIME_MAP_MIN_MOVE_M = 180.0
# Count as a trip for KPIs if at least this distance (km)
LIFETIME_MAP_MIN_TRIP_KM = 1.0
# Soft cache so flipping chart period does not re-scan every time
LIFETIME_MAP_CACHE_SECONDS = 600
# Charge sessions shorter than this are ignored (plug glitches)
CHARGE_SESSION_MIN_MINUTES = 5
CHARGE_SESSION_GAP = timedelta(minutes=30)
# charge_limit_soc histogram buckets (high → low), labels for axis
CHARGE_LIMIT_BUCKET_LABELS = (
    "100%",
    "95–99%",
    "90–94%",
    "80–89%",
    "70–79%",
    "< 70%",
)
# Peak charger_power (kW): ≤11 kW = AC; ≥12 kW = DC (modern Teslas)
# (label, lo_inclusive, hi_exclusive) — last band hi=None means no upper bound
# Order high → low for display
CHARGER_POWER_BUCKETS = (
    ("≥ 250 kW", 250, None),
    ("200–249 kW", 200, 250),
    ("150–199 kW", 150, 200),
    ("100–149 kW", 100, 150),
    ("50–99 kW", 50, 100),
    ("12–49 kW", 12, 50),
    ("≤ 11 kW", None, 12),  # AC
)
# Peak charge_rate (mi/h of rated range added) — Tesla units
CHARGE_RATE_BUCKETS = (
    ("≥ 700 mi/h", 700, None),
    ("500–699 mi/h", 500, 700),
    ("300–499 mi/h", 300, 500),
    ("150–299 mi/h", 150, 300),
    ("50–149 mi/h", 50, 150),
    ("≤ 49 mi/h", None, 50),  # typical AC
)
# Peak SoC reached during a charge session (end-of-charge habit)
CHARGE_END_SOC_BUCKET_LABELS = (
    "100%",
    "95–99%",
    "90–94%",
    "80–89%",
    "70–79%",
    "50–69%",
    "< 50%",
)
# Daily minimum SoC (how low the pack goes)
DAILY_MIN_SOC_BUCKET_LABELS = (
    "< 5%",
    "5–9%",
    "10–19%",
    "20–29%",
    "30–39%",
    "40–49%",
    "≥ 50%",
)
# Drive speed histogram (km/h) — Tesla API speed is mph, converted at bin time
# (label, lo_inclusive, hi_exclusive); last hi=None = open upper
DRIVE_SPEED_BUCKETS_KMH = (
    ("0–20", 0, 20),
    ("20–40", 20, 40),
    ("40–60", 40, 60),
    ("60–90", 60, 90),
    ("90–110", 90, 110),
    ("110–130", 110, 130),
    ("≥ 130", 130, None),
)
# Drive power histogram (kW): negative = regen, positive = traction
DRIVE_POWER_BUCKETS_KW = (
    ("≤ −80", None, -80),
    ("−80…−40", -80, -40),
    ("−40…−15", -40, -15),
    ("−15…−2", -15, -2),
    ("−2…+2", -2, 2),
    ("+2…+15", 2, 15),
    ("+15…+40", 15, 40),
    ("+40…+80", 40, 80),
    ("+80…+150", 80, 150),
    ("≥ +150", 150, None),
)
# Bar colors: regen (green) → coast (muted) → traction (blue → amber → red)
DRIVE_POWER_BAR_COLORS = (
    ENERGY,
    ENERGY,
    ENERGY,
    "#3ec9e0",
    MUTED,
    ACCENT_SOFT,
    ACCENT,
    WARM,
    DANGER,
    "#ff3b4e",
)
# Hard safety: stop scanning drive sensors after this many valid rows (O(1) memory
# streaming — no list). Far above normal multi-year logs; avoids pathological DB.
DRIVE_SENSOR_MAX_SCAN = 200000
# When integrating power×Δt to kWh, ignore gaps larger than this
# (TeslaFi / adaptive capture can be 10–60 s between drive samples)
DRIVE_POWER_ENERGY_MAX_GAP = timedelta(seconds=90)


def GetTitleForFieldDico():
    dico = {
        "outside_temp": _("Outside temperature (°C)"),
        "driver_temp_setting": _("Driver temperature (°C)"),
        "inside_temp": _("Inside temperature (°C)"),
        "passenger_temp_setting": _("Passenger temperature (°C)"),
        "odometer": _("Odometer (miles)"),
        # Drive samples histogram (API stores mph; axis in km/h)
        "speed": _("Speed distribution"),
        "latitude": _("Latitude"),
        "longitude": _("Longitude"),
        # Drive power histogram (kW; negative = regenerative braking)
        "power": _("Power distribution"),
        # Charge-session peak SoC (not raw level over time)
        "battery_level": _("SoC at end of charge"),
        # Daily minimum SoC habits (not raw rated range over time)
        "battery_range": _("Daily minimum SoC"),
        # Histogram of charge sessions by limit (not a raw time series)
        "charge_limit_soc": _("Charge limit distribution"),
        # Session histograms (peak rate / power), not raw time series
        "charge_rate": _("Charge rate distribution"),
        "charger_actual_current": _("Charger actual current (A)"),
        "charger_phases": _("Charger phases"),
        "charger_power": _("Charger power distribution"),
        "charger_voltage": _("Charger voltage (V)"),
        "est_battery_range": _("Estimated battery range (miles)"),
        "usable_battery_level": _("Usable battery level (%)"),
        "battery_degradation": _("Battery degradation (%)"),
        # Extrapolated full-charge range: battery_range / soc * 100
        # (wording avoids bare "%" — breaks gettext python-format matching)
        "range_at_100": _("Range at full charge (miles)"),
        "range_at_100_odometer": _("Range at full charge vs odometer (miles)"),
        # Trip efficiency vs rated range drop (100% = matched EPA-rated prediction)
        "efficiency_by_speed": _("Efficiency vs average speed"),
        "efficiency_by_temp": _("Efficiency vs outside temperature"),
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


def _entry_get(entry, key, default=None):
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def GetXandYRangeAt100(results, xfield, *, daily_median=True, min_soc=None):
    """Scatter points: X = model field, Y = range at 100% SoC (high SoC, quieter)."""
    # Floor matches fallback so soft queryset (75 %) is not re-filtered to 90 % here.
    if min_soc is None:
        min_soc = DEGRADATION_SCATTER_FALLBACK_SOC
    xvalues = []
    yvalues = []
    dates = []
    for entry in results:
        if _entry_get(entry, "charging_state") == "Charging":
            continue
        level = _entry_get(entry, "usable_battery_level")
        if level is None:
            level = _entry_get(entry, "battery_level")
        if level is None or float(level) < min_soc:
            continue
        br = _entry_get(entry, "battery_range")
        if br is None or level is None or float(level) <= 0:
            continue
        y = float(br) / float(level) * 100.0
        x = _entry_get(entry, xfield)
        if x is None:
            continue
        xvalues.append(x)
        yvalues.append(y)
        dates.append(_entry_get(entry, "Date"))
    if daily_median:
        return aggregate_scatter_daily_median(xvalues, yvalues, dates)
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

def GenerateDateGraph(datesList, maxvalues, minvalues, avgvalues, title, size="full"):
    # matplotlib 3.9+ removed Axes.plot_date — use plot() with date objects
    fig, cfg = make_figure(size)

    language = django.utils.translation.get_language()
    if language is not None and language == 'fr':
        formatter = DateFormatter('%d/%m/%y')
    else:
        formatter = DateFormatter('%m/%d/%y')

    ax = fig.subplots()
    if datesList is not None and minvalues is not None and len(datesList) > 0:
        lw = cfg["linewidth"]
        # Lines only (no markers) — clearer on dense multi-week series
        ax.plot(
            datesList,
            minvalues,
            color=SERIES_COLORS[0],
            linestyle="-",
            linewidth=lw,
            label=_("Minimum"),
            zorder=2,
        )
        ax.plot(
            datesList,
            avgvalues,
            color=SERIES_COLORS[1],
            linestyle="-",
            linewidth=lw + 0.25,
            label=_("Average"),
            zorder=3,
        )
        ax.plot(
            datesList,
            maxvalues,
            color=SERIES_COLORS[2],
            linestyle="-",
            linewidth=lw,
            label=_("Maximum"),
            zorder=2,
        )
        style_legend(ax, cfg)
        ax.xaxis.set_major_formatter(formatter)
        # One day of data still plots fine; widen x-axis so a single point is not clipped
        if len(datesList) == 1:
            d = datesList[0]
            ax.set_xlim(d - timedelta(days=1), d + timedelta(days=1))
        fig.autofmt_xdate()
    finish_figure(fig, ax, title, cfg)
    return GeneratePngFromGraph(fig, size=size)


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


def _rated_range_miles(br, ideal=None):
    """Prefer rated battery_range; fall back to ideal."""
    if br is not None:
        try:
            return float(br)
        except (TypeError, ValueError):
            pass
    if ideal is not None:
        try:
            return float(ideal)
        except (TypeError, ValueError):
            pass
    return None


def _trip_efficiency_pct(r0, r1, miles):
    """
    Efficiency vs Tesla rated range prediction.
    100% = distance driven matches rated-range drop (EPA-style estimate).
    Below 100% = used more rated range than miles driven (worse than prediction).
    """
    if miles is None or miles < 0.5 or r0 is None or r1 is None:
        return None
    used = r0 - r1
    if used < 0.25:
        return None
    pct = 100.0 * float(miles) / used
    if pct < EFFICIENCY_MIN_PCT or pct > EFFICIENCY_MAX_PCT:
        return None
    return pct


def _drive_filter_q():
    """SQL filter: only rows that look like motion (skip pure park/charge)."""
    return Q(shift_state__in=["D", "R", "N"]) | Q(speed__gt=DAY_MAP_STOP_SPEED)


def _extract_efficiency_trips_from_drive_rows(rows):
    """
    Drive-only samples, chronological. Split into trips on time gaps
    (no need to load park points — efficiency only needs odo + rated range).
    """
    if not rows or len(rows) < 2:
        return []

    # Split on long gaps between consecutive drive samples
    segments = []
    cur = [rows[0]]
    for p in rows[1:]:
        prev_t, t = cur[-1]["t"], p["t"]
        if prev_t is None or t is None or (t - prev_t) > EFFICIENCY_TRIP_GAP:
            if len(cur) >= 2:
                segments.append(cur)
            cur = [p]
        else:
            cur.append(p)
    if len(cur) >= 2:
        segments.append(cur)

    trips = []
    for pts in segments:
        a, b = pts[0], pts[-1]
        seconds = max(0.0, (b["t"] - a["t"]).total_seconds())
        if seconds < 60:
            continue
        odo_a, odo_b = a.get("odometer"), b.get("odometer")
        if odo_a is None or odo_b is None or odo_b < odo_a:
            continue
        miles = float(odo_b) - float(odo_a)
        km = miles * 1.609344
        if km < EFFICIENCY_MIN_KM:
            continue
        r0 = _rated_range_miles(a.get("battery_range"), a.get("ideal_battery_range"))
        r1 = _rated_range_miles(b.get("battery_range"), b.get("ideal_battery_range"))
        eff = _trip_efficiency_pct(r0, r1, miles)
        if eff is None:
            continue
        hours = seconds / 3600.0
        avg_kmh = km / hours if hours > 0.01 else None
        if avg_kmh is None or avg_kmh < 5 or avg_kmh > 160:
            continue
        temps = []
        for p in pts:
            ot = p.get("outside_temp")
            if ot is not None:
                try:
                    temps.append(float(ot))
                except (TypeError, ValueError):
                    pass
        avg_temp = sum(temps) / len(temps) if temps else None
        trips.append(
            {
                "km": km,
                "avg_kmh": avg_kmh,
                "avg_temp_c": avg_temp,
                "efficiency_pct": eff,
            }
        )
    return trips


def _bin_trips(trips, *, key, bin_width, label_fmt):
    """
    Aggregate trips into bins.
    Returns (labels, mean_efficiency, total_km) sorted by bin start.
    """
    buckets = {}  # bin_start -> [effs], km_sum
    for t in trips:
        val = t.get(key)
        if val is None:
            continue
        start = int(val // bin_width) * bin_width
        if key == "avg_kmh" and start < 10:
            continue  # match TeslaFi-style focus on real road speeds
        b = buckets.setdefault(start, {"effs": [], "km": 0.0})
        b["effs"].append(t["efficiency_pct"])
        b["km"] += t["km"]

    if not buckets:
        return [], [], []

    labels = []
    effs = []
    kms = []
    for start in sorted(buckets.keys()):
        b = buckets[start]
        if not b["effs"]:
            continue
        labels.append(label_fmt(start, start + bin_width))
        effs.append(sum(b["effs"]) / len(b["effs"]))
        kms.append(b["km"])
    return labels, effs, kms


def _downsample_rows_inplace(rows, max_rows):
    """Keep chronological order; thin evenly to at most max_rows."""
    n = len(rows)
    if n <= max_rows:
        return rows
    step = max(1, (n + max_rows - 1) // max_rows)
    return rows[::step][:max_rows]


def _load_efficiency_trips(qs):
    """
    Shared trip list for 1D/2D efficiency charts.
    SQL drive filter + single scan thin to EFFICIENCY_MAX_DRIVE_ROWS.
    """
    drive_qs = (
        qs.filter(_drive_filter_q())
        .order_by("Date")
        .values(
            "Date",
            "speed",
            "odometer",
            "battery_range",
            "ideal_battery_range",
            "outside_temp",
        )
    )

    rows = []
    cap = EFFICIENCY_MAX_DRIVE_ROWS
    for s in drive_qs.iterator(chunk_size=4000):
        t = s.get("Date")
        if t is None:
            continue
        rows.append(
            {
                "t": t,
                "speed": s.get("speed"),
                "odometer": s.get("odometer"),
                "battery_range": s.get("battery_range"),
                "ideal_battery_range": s.get("ideal_battery_range"),
                "outside_temp": s.get("outside_temp"),
            }
        )
        if len(rows) >= cap * 2:
            rows = rows[::2]

    rows = _downsample_rows_inplace(rows, cap)
    return _extract_efficiency_trips_from_drive_rows(rows)


def _efficiency_bins_for_queryset(qs, *, by_speed: bool):
    """1D histograms: efficiency vs speed or temperature."""
    trips = _load_efficiency_trips(qs)
    if by_speed:
        labels, eff, kms = _bin_trips(
            trips,
            key="avg_kmh",
            bin_width=EFFICIENCY_SPEED_BIN_KMH,
            label_fmt=lambda a, b: f"{int(a)}–{int(b)}",
        )
        xlabel = _("Average speed (km/h)")
    else:
        labels, eff, kms = _bin_trips(
            trips,
            key="avg_temp_c",
            bin_width=EFFICIENCY_TEMP_BIN_C,
            label_fmt=lambda a, b: f"{int(a)}–{int(b)}",
        )
        xlabel = _("Outside temperature (°C)")
    return labels, eff, kms, xlabel


def _charge_limit_bucket_index(limit_pct: float) -> int:
    """Map a charge limit % into CHARGE_LIMIT_BUCKET_LABELS index."""
    x = float(limit_pct)
    if x >= 99.5:
        return 0  # 100%
    if x >= 95:
        return 1  # 95–99
    if x >= 90:
        return 2  # 90–94
    if x >= 80:
        return 3  # 80–89
    if x >= 70:
        return 4  # 70–79
    return 5  # < 70


def _range_bucket_index(value: float, buckets) -> int:
    """Map value into bucket list of (label, lo, hi) high→low; lo/hi may be None."""
    x = float(value)
    for i, (_lab, lo, hi) in enumerate(buckets):
        if lo is not None and x < lo:
            continue
        if hi is not None and x >= hi:
            continue
        return i
    return len(buckets) - 1


def _iter_drive_sensor(qs, *extra_fields, extra_q=None):
    """
    Stream drive-motion samples chronologically (O(1) memory via iterator).

    Yields dict rows with Date + extra_fields. Stops after DRIVE_SENSOR_MAX_SCAN
    valid rows as a hard safety bound.
    """
    fields = {"Date", *extra_fields}
    drive_qs = qs.filter(_drive_filter_q())
    if extra_q is not None:
        drive_qs = drive_qs.filter(extra_q)
    drive_qs = drive_qs.order_by("Date").values(*fields)
    n = 0
    for s in drive_qs.iterator(chunk_size=4000):
        if s.get("Date") is None:
            continue
        yield s
        n += 1
        if n >= DRIVE_SENSOR_MAX_SCAN:
            break


def _drive_speed_histogram(qs):
    """
    Histogram of driving speed in km/h (API stores mph).
    Full stream (no thin) — only bin counters, O(1) memory.
    """
    counts = [0] * len(DRIVE_SPEED_BUCKETS_KMH)
    for r in _iter_drive_sensor(
        qs,
        "speed",
        extra_q=Q(speed__isnull=False) & Q(speed__gt=DAY_MAP_STOP_SPEED),
    ):
        sp = r.get("speed")
        if sp is None:
            continue
        try:
            mph = float(sp)
        except (TypeError, ValueError):
            continue
        if mph < 0 or mph > 200:
            continue
        if mph <= DAY_MAP_STOP_SPEED:
            continue
        kmh = mph * 1.609344
        counts[_range_bucket_index(kmh, DRIVE_SPEED_BUCKETS_KMH)] += 1
    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    labels = [b[0] for b in DRIVE_SPEED_BUCKETS_KMH]
    return labels, counts, pcts


def _drive_power_histogram(qs):
    """
    Histogram of drive_state.power (kW). Negative = regenerative braking.

    Single chronological stream (no subsampling): bin counts + power×Δt energy
    so regen/traction kWh stay consistent with the % recovered.

    Returns (labels, counts, pcts, meta) with traction_kwh, regen_kwh, regen_pct.
    """
    counts = [0] * len(DRIVE_POWER_BUCKETS_KW)
    traction_j = 0.0  # kW·s (≈ kJ)
    regen_j = 0.0
    # Sample-magnitude fallback when Δt is too sparse for energy integration
    traction_mag = 0.0
    regen_mag = 0.0
    prev_t = None
    prev_p = None
    n = 0
    for r in _iter_drive_sensor(qs, "power", extra_q=Q(power__isnull=False)):
        t = r.get("Date")
        pw = r.get("power")
        if t is None or pw is None:
            continue
        try:
            p = float(pw)
        except (TypeError, ValueError):
            continue
        if p < -400 or p > 400:
            continue
        counts[_range_bucket_index(p, DRIVE_POWER_BUCKETS_KW)] += 1
        n += 1
        if p > 0.5:
            traction_mag += p
        elif p < -0.5:
            regen_mag += -p
        if prev_t is not None and prev_p is not None:
            dt = t - prev_t
            if timedelta(0) < dt <= DRIVE_POWER_ENERGY_MAX_GAP:
                p_avg = 0.5 * (prev_p + p)
                seconds = dt.total_seconds()
                if p_avg > 0.5:
                    traction_j += p_avg * seconds
                elif p_avg < -0.5:
                    regen_j += (-p_avg) * seconds
        prev_t, prev_p = t, p

    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    labels = [b[0] for b in DRIVE_POWER_BUCKETS_KW]
    traction_kwh = traction_j / 3600.0
    regen_kwh = regen_j / 3600.0
    # Prefer time-integrated energy when we accumulated a meaningful amount;
    # otherwise fall back to Σ|P| ratio (good when sampling is irregular).
    if traction_kwh >= 1.0:
        regen_pct = 100.0 * regen_kwh / traction_kwh
        mode = "energy"
    elif traction_mag > 1.0:
        regen_pct = 100.0 * regen_mag / traction_mag
        mode = "sample"
        traction_kwh = None
        regen_kwh = None
    else:
        regen_pct = None
        mode = None
    meta = {
        "n_samples": n,
        "traction_kwh": traction_kwh,
        "regen_kwh": regen_kwh,
        "regen_pct": regen_pct,
        "mode": mode,
    }
    return labels, counts, pcts, meta


def _session_charge_limit(pts) -> float | None:
    """
    Representative charge limit for a charge session.
    Prefer charge_limit_soc (median of samples); else peak SoC reached.
    """
    limits = []
    peaks = []
    for p in pts:
        lim = p.get("charge_limit_soc")
        if lim is not None:
            try:
                limits.append(float(lim))
            except (TypeError, ValueError):
                pass
        soc = p.get("usable_battery_level")
        if soc is None:
            soc = p.get("battery_level")
        if soc is not None:
            try:
                peaks.append(float(soc))
            except (TypeError, ValueError):
                pass
    if limits:
        limits.sort()
        return limits[len(limits) // 2]
    if peaks:
        return max(peaks)
    return None


def _session_float_max(pts, key) -> float | None:
    best = None
    for p in pts:
        v = p.get(key)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv < 0:
            continue
        if best is None or fv > best:
            best = fv
    return best


def _session_delta_field(pts, key) -> float | None:
    """Positive delta of a cumulative field over the session (max - min of samples)."""
    vals = []
    for p in pts:
        v = p.get(key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < 2:
        return None
    delta = max(vals) - min(vals)
    return delta if delta > 0.01 else None


def _iter_charge_sessions(qs, extra_fields=()):
    """
    Yield lists of sample dicts for each charge session (≥ min duration).
    SQL: charging samples only; split on CHARGE_SESSION_GAP.
    """
    fields = {
        "Date",
        "charge_limit_soc",
        "battery_level",
        "usable_battery_level",
        "charger_power",
        "charge_rate",
        "charge_energy_added",
        "charge_miles_added_rated",
        "battery_range",
    }
    fields.update(extra_fields)
    charge_q = Q(charging_state__iexact="Charging") | Q(
        charging_state__iexact="Starting"
    ) | Q(charger_power__gt=0.5)
    rows = qs.filter(charge_q).order_by("Date").values(*fields)

    cur = []
    for r in rows.iterator(chunk_size=4000):
        t = r.get("Date")
        if t is None:
            continue
        p = dict(r)
        p["t"] = t
        if cur and (t - cur[-1]["t"]) > CHARGE_SESSION_GAP:
            if _charge_session_ok(cur):
                yield cur
            cur = [p]
        else:
            cur.append(p)
    if cur and _charge_session_ok(cur):
        yield cur


def _charge_session_ok(pts) -> bool:
    if len(pts) < 2:
        return False
    minutes = max(0.0, (pts[-1]["t"] - pts[0]["t"]).total_seconds()) / 60.0
    return minutes >= CHARGE_SESSION_MIN_MINUTES


def _charge_limit_session_histogram(qs):
    """Count charge sessions by charge_limit_soc bucket. Returns (labels, counts, pcts)."""
    counts = [0] * len(CHARGE_LIMIT_BUCKET_LABELS)
    for pts in _iter_charge_sessions(qs):
        lim = _session_charge_limit(pts)
        if lim is None:
            continue
        counts[_charge_limit_bucket_index(lim)] += 1
    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    return list(CHARGE_LIMIT_BUCKET_LABELS), counts, pcts


def _charge_peak_histogram(qs, *, metric: str):
    """
    Sessions binned by peak charger_power (kW) or peak charge_rate (mi/h).
    Returns (labels, session_counts, amount_per_bucket, pcts, amount_unit).
    amount_unit is "kwh" for power (charge_energy_added) or "mi" for rate.
    """
    if metric == "charger_power":
        buckets = CHARGER_POWER_BUCKETS
        peak_key = "charger_power"
        amount_unit = "kwh"
    else:
        buckets = CHARGE_RATE_BUCKETS
        peak_key = "charge_rate"
        amount_unit = "mi"

    n_b = len(buckets)
    counts = [0] * n_b
    amount_tot = [0.0] * n_b

    for pts in _iter_charge_sessions(qs):
        peak = _session_float_max(pts, peak_key)
        # Fleet/TeslaFi often omit charger_power on AC; infer AC from low charge_rate
        if (peak is None or peak <= 0) and peak_key == "charger_power":
            rate = _session_float_max(pts, "charge_rate")
            if rate is not None and 0 < rate <= 49:
                peak = 7.0  # typical home AC for bucketing only
        if peak is None or peak <= 0:
            continue
        idx = _range_bucket_index(peak, buckets)
        counts[idx] += 1
        if amount_unit == "kwh":
            amt = _session_delta_field(pts, "charge_energy_added")
            # Fallback: rough kWh from rated miles if energy counter missing
            if amt is None:
                miles = _session_delta_field(pts, "charge_miles_added_rated")
                if miles is None:
                    miles = _session_delta_field(pts, "battery_range")
                if miles is not None and miles > 0:
                    # ~0.22 kWh/mi fleet average (same ballpark as daymap pack estimate)
                    amt = miles * 0.22
        else:
            amt = _session_delta_field(pts, "charge_miles_added_rated")
            if amt is None:
                amt = _session_delta_field(pts, "battery_range")
        if amt is not None and amt > 0:
            amount_tot[idx] += amt

    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    labels = [b[0] for b in buckets]
    return labels, counts, amount_tot, pcts, amount_unit


def _end_soc_bucket_index(soc: float) -> int:
    """Peak SoC during charge → CHARGE_END_SOC_BUCKET_LABELS."""
    x = float(soc)
    if x >= 99.5:
        return 0
    if x >= 95:
        return 1
    if x >= 90:
        return 2
    if x >= 80:
        return 3
    if x >= 70:
        return 4
    if x >= 50:
        return 5
    return 6


def _daily_min_soc_bucket_index(soc: float) -> int:
    """Daily min SoC → DAILY_MIN_SOC_BUCKET_LABELS."""
    x = float(soc)
    if x < 5:
        return 0
    if x < 10:
        return 1
    if x < 20:
        return 2
    if x < 30:
        return 3
    if x < 40:
        return 4
    if x < 50:
        return 5
    return 6


def _sample_soc(p) -> float | None:
    u = p.get("usable_battery_level")
    if u is not None:
        try:
            return float(u)
        except (TypeError, ValueError):
            pass
    b = p.get("battery_level")
    if b is not None:
        try:
            return float(b)
        except (TypeError, ValueError):
            pass
    return None


def _charge_end_soc_histogram(qs):
    """Sessions classified by max SoC reached while charging."""
    counts = [0] * len(CHARGE_END_SOC_BUCKET_LABELS)
    for pts in _iter_charge_sessions(qs):
        peak = None
        for p in pts:
            soc = _sample_soc(p)
            if soc is None:
                continue
            if peak is None or soc > peak:
                peak = soc
        if peak is None:
            continue
        counts[_end_soc_bucket_index(peak)] += 1
    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    return list(CHARGE_END_SOC_BUCKET_LABELS), counts, pcts


def _daily_min_soc_histogram(qs):
    """Calendar days classified by minimum SoC that day."""
    day_mins = (
        qs.exclude(battery_level__isnull=True, usable_battery_level__isnull=True)
        .values("DateOnlyDay")
        .annotate(
            min_u=Min("usable_battery_level"),
            min_b=Min("battery_level"),
        )
        .order_by("DateOnlyDay")
    )
    counts = [0] * len(DAILY_MIN_SOC_BUCKET_LABELS)
    for row in day_mins.iterator(chunk_size=2000):
        if row.get("DateOnlyDay") is None:
            continue
        mu, mb = row.get("min_u"), row.get("min_b")
        # Prefer usable min when available, else battery_level min
        day_min = None
        if mu is not None:
            try:
                day_min = float(mu)
            except (TypeError, ValueError):
                day_min = None
        if day_min is None and mb is not None:
            try:
                day_min = float(mb)
            except (TypeError, ValueError):
                day_min = None
        if day_min is None:
            continue
        counts[_daily_min_soc_bucket_index(day_min)] += 1
    total = sum(counts)
    pcts = [(100.0 * c / total) if total else 0.0 for c in counts]
    return list(DAILY_MIN_SOC_BUCKET_LABELS), counts, pcts


def GenerateChargeLimitHistogram(labels, counts, pcts, title, size="full"):
    """
    Bar chart: how often charge sessions used each limit band.
    Annotate each bar with count and share of sessions.
    """
    return GenerateChargeSessionHistogram(
        labels,
        counts,
        pcts,
        title,
        size=size,
        xlabel=_("Charge limit"),
        amount_per_bucket=None,
        amount_unit=None,
    )


def GenerateChargeSessionHistogram(
    labels,
    counts,
    pcts,
    title,
    size="full",
    *,
    xlabel,
    amount_per_bucket=None,
    amount_unit=None,
    foot_extra=None,
    count_ylabel=None,
    footer=None,
    bar_colors=None,
    edge_color=None,
):
    """
    Bar chart: session/day/sample counts by bucket; annotate n, %, optional energy/range.
    amount_unit: "kwh" | "mi" | None
    footer: if set, replaces the auto-built footer text.
    bar_colors: optional list of face colors (one per bar).
    Footer sits outside the axes so it never covers short bars.
    """
    fig, cfg = make_figure(size, bar=True)
    ax = fig.subplots()
    if labels and counts and sum(counts) > 0:
        x = list(range(len(labels)))
        if bar_colors and len(bar_colors) >= len(labels):
            colors = list(bar_colors[: len(labels)])
        else:
            colors = ACCENT_SOFT
        bars = ax.bar(
            x,
            counts,
            color=colors,
            edgecolor=edge_color or ACCENT,
            linewidth=0.5,
            alpha=0.92,
            zorder=2,
        )
        ymax = max(counts) if counts else 1
        # Room above tallest bar for 2-line labels (not 3 lines over short bars)
        ax.set_ylim(0, ymax * 1.18)
        fs = max(6.0, cfg["tick_size"] - 0.5)
        for i, (bar, c, p) in enumerate(zip(bars, counts, pcts)):
            if c <= 0:
                continue
            # Compact: "73 (5%)" or "12.4k (20%)" for large drive-sample counts
            if c >= 10000:
                c_txt = f"{c / 1000:.0f}k"
            elif c >= 1000:
                c_txt = f"{c / 1000:.1f}k"
            else:
                c_txt = str(c)
            label = f"{c_txt} ({p:.0f}%)"
            amt = None
            if amount_per_bucket is not None and i < len(amount_per_bucket):
                amt = amount_per_bucket[i]
            if amt is not None and amt >= 0.5:
                if amount_unit == "kwh":
                    label = f"{c_txt} ({p:.0f}%)\n{amt:.0f} kWh"
                else:
                    label = f"{c_txt} ({p:.0f}%)\n{amt:.0f} mi"
            ax.annotate(
                label,
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=fs,
                fontweight="medium",
                zorder=3,
                clip_on=False,
            )
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=22, ha="right")
        ax.set_ylabel(count_ylabel or _("Charge sessions"), color=MUTED)
        ax.set_xlabel(xlabel, color=MUTED)
        total = sum(counts)
        # Short footer under the figure (not inside the plot area)
        if footer is not None:
            foot = footer
        elif amount_unit == "kwh":
            foot = _(
                "n=%(n)s sessions (≥%(min)s min) · bars = peak kW · kWh added"
            ) % {
                "n": total,
                "min": CHARGE_SESSION_MIN_MINUTES,
            }
        elif amount_unit == "mi":
            foot = _(
                "n=%(n)s sessions (≥%(min)s min) · bars = peak · mi = range added"
            ) % {
                "n": total,
                "min": CHARGE_SESSION_MIN_MINUTES,
            }
        elif foot_extra:
            foot = _("n=%(n)s") % {"n": total} + " · " + foot_extra
        else:
            foot = _("n=%(n)s sessions (≥%(min)s min)") % {
                "n": total,
                "min": CHARGE_SESSION_MIN_MINUTES,
            }
        style_axes(ax, cfg)
        style_suptitle(fig, title, cfg)
        try:
            fig.tight_layout(rect=(0.02, 0.08, 0.98, 0.90))
        except Exception:
            pass
        fig.text(
            0.5,
            0.01,
            foot,
            ha="center",
            va="bottom",
            color=MUTED,
            fontsize=cfg["tick_size"] - 0.5,
        )
    else:
        finish_figure(fig, ax, title, cfg)
    return GeneratePngFromGraph(fig, size=size)


def GenerateEfficiencyBinGraph(labels, efficiency, km_totals, title, xlabel, size="full"):
    """
    Dual-axis chart: bars = km recorded in bin, line = mean efficiency %.
    Dark MaTesla style (not a TeslaFi clone).
    """
    fig, cfg = make_figure(size)
    ax = fig.subplots()
    if labels and efficiency and len(labels) > 0:
        x = list(range(len(labels)))
        ax2 = ax.twinx()
        # Bars behind the line
        bar_w = 0.72
        ax2.bar(
            x,
            km_totals,
            width=bar_w,
            color=ACCENT_SOFT,
            alpha=0.35,
            edgecolor=ACCENT,
            linewidth=0.4,
            zorder=1,
            label=_("Distance recorded (km)"),
        )
        ax.plot(
            x,
            efficiency,
            color=ENERGY,
            linestyle="-",
            linewidth=cfg["linewidth"] + 0.35,
            marker="o",
            markersize=cfg["markersize"] + 1.2,
            markerfacecolor=ENERGY,
            markeredgecolor="#0b1220",
            markeredgewidth=0.6,
            zorder=3,
            label=_("Efficiency"),
        )
        # Annotate a few efficiency values when not too crowded
        if len(labels) <= 18:
            for i, e in enumerate(efficiency):
                ax.annotate(
                    f"{e:.0f}%",
                    (i, e),
                    textcoords="offset points",
                    xytext=(0, 7),
                    ha="center",
                    fontsize=cfg["tick_size"] - 0.5,
                    color=TEXT,
                    zorder=4,
                )
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=35, ha="right")
        ax.set_ylabel(_("Efficiency (%)"), color=MUTED)
        ax2.set_ylabel(_("Distance (km)"), color=MUTED)
        ax.set_xlabel(xlabel, color=MUTED)
        ax.set_ylim(bottom=max(0, min(efficiency) - 12), top=min(145, max(efficiency) + 12))
        ax2.set_ylim(bottom=0, top=max(km_totals) * 1.25 if km_totals else 1)
        style_axes(ax, cfg)
        ax2.set_facecolor("none")
        ax2.tick_params(colors=MUTED, labelsize=cfg["tick_size"], length=3.5, width=0.7)
        ax2.yaxis.label.set_color(MUTED)
        for spine in ax2.spines.values():
            spine.set_color("#3a5070")
            spine.set_linewidth(cfg["spine_width"])
        # Combined legend
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        leg = ax.legend(
            h1 + h2,
            l1 + l2,
            facecolor="#162338",
            edgecolor="#3a5070",
            labelcolor=TEXT,
            fontsize=cfg["legend_size"],
            framealpha=0.92,
            loc="best",
        )
        if leg is not None:
            leg.get_frame().set_linewidth(0.8)
        # Subtitle hint
        ax.text(
            0.01,
            0.02,
            _("Trips ≥ 10 km · 100% = matched rated range use"),
            transform=ax.transAxes,
            fontsize=cfg["tick_size"] - 0.5,
            color=MUTED,
            va="bottom",
            zorder=5,
        )
        style_suptitle(fig, title, cfg)
        try:
            fig.tight_layout(rect=(0.02, 0.02, 0.98, 0.92))
        except Exception:
            pass
    else:
        finish_figure(fig, ax, title, cfg)
    return GeneratePngFromGraph(fig, size=size)


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
    size = graph_size_from_request(request)
    title = GetTitleForField(desiredfield)
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    if not base.exists():
        if desiredfield in ("efficiency_by_speed", "efficiency_by_temp"):
            return GenerateEfficiencyBinGraph(
                None, None, None, title, "", size=size
            )
        if desiredfield in ("speed", "power"):
            return GenerateChargeSessionHistogram(
                [],
                [],
                [],
                title,
                size=size,
                xlabel="",
                count_ylabel=_("Samples"),
            )
        return GenerateDateGraph(None, None, None, None, title, size=size)

    # Trip efficiency histograms (not a raw time series field)
    if desiredfield in ("efficiency_by_speed", "efficiency_by_temp"):
        qs = _period_filter(base, desiredperiod)
        labels, eff, kms, xlabel = _efficiency_bins_for_queryset(
            qs, by_speed=(desiredfield == "efficiency_by_speed")
        )
        return GenerateEfficiencyBinGraph(
            labels, eff, kms, title, xlabel, size=size
        )

    # Drive speed distribution (replaces noisy min/avg/max time series)
    if desiredfield == "speed":
        qs = _period_filter(base, desiredperiod)
        labels, counts, pcts = _drive_speed_histogram(qs)
        return GenerateChargeSessionHistogram(
            labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=_("Speed (km/h)"),
            amount_per_bucket=None,
            amount_unit=None,
            count_ylabel=_("Samples"),
            foot_extra=_("drive samples only · Tesla speed converted mph→km/h"),
        )

    # Drive power distribution + regen vs traction energy estimate
    if desiredfield == "power":
        qs = _period_filter(base, desiredperiod)
        labels, counts, pcts, meta = _drive_power_histogram(qs)
        total = sum(counts)
        if (
            meta.get("regen_pct") is not None
            and meta.get("mode") == "energy"
            and meta.get("traction_kwh") is not None
        ):
            # 1 decimal so small regen does not round to "0 kWh" next to a non-zero %
            foot = _(
                "n=%(n)s · regen %(regen).1f kWh / traction %(trac).1f kWh "
                "= %(pct).1f%% recovered"
            ) % {
                "n": total,
                "regen": meta["regen_kwh"],
                "trac": meta["traction_kwh"],
                "pct": meta["regen_pct"],
            }
        elif meta.get("regen_pct") is not None:
            # Sparse samples: ratio of Σ|P_regen| / Σ P_traction
            foot = _(
                "n=%(n)s · regen vs traction = %(pct).1f%% "
                "(sample power ratio · green = regen)"
            ) % {
                "n": total,
                "pct": meta["regen_pct"],
            }
        elif total:
            foot = _(
                "n=%(n)s drive samples · green = regen · blue/red = traction"
            ) % {"n": total}
        else:
            foot = _("No drive power samples in this period")
        return GenerateChargeSessionHistogram(
            labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=_("Power (kW) · negative = regen"),
            amount_per_bucket=None,
            amount_unit=None,
            count_ylabel=_("Samples"),
            footer=foot,
            bar_colors=DRIVE_POWER_BAR_COLORS,
            edge_color="#1a2a40",
        )

    # Charge limit: session histogram (how often set to 100% / 80% / …)
    if desiredfield == "charge_limit_soc":
        qs = _period_filter(base, desiredperiod)
        labels, counts, pcts = _charge_limit_session_histogram(qs)
        return GenerateChargeLimitHistogram(labels, counts, pcts, title, size=size)

    # Peak power / charge-rate session histograms (DC vs AC, Supercharge peaks)
    if desiredfield in ("charger_power", "charge_rate"):
        qs = _period_filter(base, desiredperiod)
        labels, counts, amounts, pcts, amount_unit = _charge_peak_histogram(
            qs, metric=desiredfield
        )
        xlabel = (
            _("Peak charger power")
            if desiredfield == "charger_power"
            else _("Peak charge rate")
        )
        return GenerateChargeSessionHistogram(
            labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=xlabel,
            amount_per_bucket=amounts,
            amount_unit=amount_unit,
        )

    # End-of-charge SoC (replaces noisy battery_level time series)
    if desiredfield == "battery_level":
        qs = _period_filter(base, desiredperiod)
        labels, counts, pcts = _charge_end_soc_histogram(qs)
        return GenerateChargeSessionHistogram(
            labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=_("Peak SoC during charge"),
            amount_per_bucket=None,
            amount_unit=None,
            foot_extra=_("sessions classified by max SoC while charging"),
        )

    # Daily minimum SoC (replaces noisy battery_range time series)
    if desiredfield == "battery_range":
        qs = _period_filter(base, desiredperiod)
        labels, counts, pcts = _daily_min_soc_histogram(qs)
        return GenerateChargeSessionHistogram(
            labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=_("Daily minimum SoC"),
            amount_per_bucket=None,
            amount_unit=None,
            foot_extra=_("one value per calendar day"),
            count_ylabel=_("Days"),
        )

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
    return GenerateDateGraph(dates, maxvalues, minvalues, avgvalues, title, size=size)

# Weeks values offered in the personal-stats period dropdown (1 Month = 4, 10 Years = 520).
STATS_PERIOD_WEEKS = frozenset({1, 2, 4, 13, 26, 52, 104, 260, 520})
STATS_PERIOD_SESSION_KEY = "personalstats_period_weeks"
STATS_PERIOD_DEFAULT = 520  # 10 years — full history is fast enough even with long series


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
    Query ?period= wins, then session, then 10 years (520 weeks).
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
    from mysite.writable_access import resolve_acting_user

    user = resolve_acting_user(request)
    if user is not None:
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


def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two WGS84 points."""
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2.0) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2.0) ** 2
    return 2.0 * r * asin(min(1.0, sqrt(a)))


def _thin_segments_to_cap(segments, max_points):
    """
    Proportionally downsample polyline segments so total points ≤ max_points.

    First evenly samples *which* segments to keep (so late trips are not
    dropped when the budget runs out), then thins points inside each.
    Always keeps ≥2 points on retained multi-point segments.
    """
    multi = [s for s in segments if len(s) >= 2]
    if not multi:
        return [s for s in segments if s]
    total = sum(len(s) for s in multi)
    if total <= max_points or max_points < 2:
        return multi

    # Each kept segment needs ≥2 points → hard cap on segment count
    max_segs = max(1, max_points // 2)
    if len(multi) > max_segs:
        keep_idx = _downsample_indices(len(multi), max_segs)
        multi = [multi[i] for i in keep_idx]

    weights = [len(s) for s in multi]
    wsum = float(sum(weights) or 1)
    # Proportional target, then fix so sum(allow) == max_points and each ≥2
    raw = [max(2, int(round(max_points * (w / wsum)))) for w in weights]
    # Clamp to segment length
    allow = [min(len(s), a) for s, a in zip(multi, raw)]
    # Redistribute if over/under budget
    diff = max_points - sum(allow)
    i = 0
    n = len(allow)
    # Prefer giving extra points to longer segments
    order = sorted(range(n), key=lambda k: weights[k], reverse=(diff > 0))
    guard = 0
    while diff != 0 and n and guard < max_points * 4:
        k = order[i % n]
        if diff > 0 and allow[k] < len(multi[k]):
            allow[k] += 1
            diff -= 1
        elif diff < 0 and allow[k] > 2:
            allow[k] -= 1
            diff += 1
        i += 1
        guard += 1

    out = []
    for s, a in zip(multi, allow):
        idxs = _downsample_indices(len(s), max(2, min(a, len(s))))
        out.append([s[j] for j in idxs])
    # Hard safety: never exceed max_points even if redistribution stalled
    total_out = sum(len(s) for s in out)
    if total_out > max_points and len(out) > 1:
        keep_n = max(1, max_points // 2)
        keep_idx = _downsample_indices(len(out), min(len(out), keep_n))
        out = [out[i] for i in keep_idx]
        # final even thin of all remaining points
        flat_budget = max_points
        per = max(2, flat_budget // max(1, len(out)))
        out = [
            [s[j] for j in _downsample_indices(len(s), min(len(s), per))]
            for s in out
        ]
    return out


def _build_lifetime_map_payload(hashedVin, desiredperiod):
    """
    Drive GPS polylines + period summary KPIs (TeslaFi-style lifetime strip).

    Single scan of drive samples with GPS; map is distance-thinned and capped.
    Metrics use odometer / rated-range / temp on the same trip segmentation.
    """
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    qs = _period_filter(base, desiredperiod)
    drive_qs = (
        qs.filter(_drive_filter_q())
        .filter(latitude__isnull=False, longitude__isnull=False)
        .order_by("Date")
        .values(
            "Date",
            "latitude",
            "longitude",
            "odometer",
            "battery_range",
            "ideal_battery_range",
            "outside_temp",
            "speed",
            "vin",
        )
    )

    segments = []  # list of [[lat, lon], ...]
    cur_path = []
    last_kept_lat = last_kept_lon = None
    last_t = None
    raw_gps = 0

    # Trip KPI accumulators (gap-split, independent of map thinning)
    drives = 0
    km_total = 0.0
    rated_mi_used = 0.0
    hours_total = 0.0
    temp_sum = 0.0
    temp_n = 0
    trip_t0 = trip_odo0 = trip_r0 = None
    trip_t1 = trip_odo1 = trip_r1 = None
    vin = None

    def finalize_trip():
        nonlocal drives, km_total, rated_mi_used, hours_total
        nonlocal trip_t0, trip_odo0, trip_r0, trip_t1, trip_odo1, trip_r1
        if trip_t0 is None or trip_t1 is None:
            trip_t0 = trip_odo0 = trip_r0 = None
            trip_t1 = trip_odo1 = trip_r1 = None
            return
        if trip_odo0 is not None and trip_odo1 is not None and trip_odo1 >= trip_odo0:
            miles = float(trip_odo1) - float(trip_odo0)
            km = miles * 1.609344
        else:
            km = 0.0
        seconds = max(0.0, (trip_t1 - trip_t0).total_seconds())
        if km >= LIFETIME_MAP_MIN_TRIP_KM and seconds >= 60:
            drives += 1
            km_total += km
            hours_total += seconds / 3600.0
            if trip_r0 is not None and trip_r1 is not None and trip_r0 > trip_r1:
                used = float(trip_r0) - float(trip_r1)
                if used > 0.25:
                    rated_mi_used += used
        trip_t0 = trip_odo0 = trip_r0 = None
        trip_t1 = trip_odo1 = trip_r1 = None

    def flush_path():
        nonlocal cur_path, last_kept_lat, last_kept_lon
        if len(cur_path) >= 2:
            segments.append(cur_path)
        elif len(cur_path) == 1:
            # Keep lonely points as degenerate segment (drawn as a small circle later)
            segments.append(cur_path)
        cur_path = []
        last_kept_lat = last_kept_lon = None

    progressive_cap = LIFETIME_MAP_MAX_POINTS * 2

    for s in drive_qs.iterator(chunk_size=4000):
        t = s.get("Date")
        lat, lon = s.get("latitude"), s.get("longitude")
        if t is None or lat is None or lon is None:
            continue
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
            continue
        if abs(lat_f) < 1e-5 and abs(lon_f) < 1e-5:
            continue  # null island / unset GPS

        raw_gps += 1
        if vin is None and s.get("vin"):
            vin = s["vin"]

        # --- KPI trip state ---
        if last_t is not None and (t - last_t) > LIFETIME_MAP_GAP:
            finalize_trip()
            flush_path()
        odo = s.get("odometer")
        try:
            odo_f = float(odo) if odo is not None else None
        except (TypeError, ValueError):
            odo_f = None
        r = _rated_range_miles(s.get("battery_range"), s.get("ideal_battery_range"))
        ot = s.get("outside_temp")
        if ot is not None:
            try:
                temp_sum += float(ot)
                temp_n += 1
            except (TypeError, ValueError):
                pass

        if trip_t0 is None:
            trip_t0, trip_odo0, trip_r0 = t, odo_f, r
        trip_t1, trip_odo1, trip_r1 = t, odo_f if odo_f is not None else trip_odo1, r if r is not None else trip_r1

        # --- Map path (distance thin) ---
        keep = False
        if last_kept_lat is None:
            keep = True
        else:
            dist = _haversine_m(last_kept_lat, last_kept_lon, lat_f, lon_f)
            if dist >= LIFETIME_MAP_MIN_MOVE_M:
                keep = True
        if keep:
            cur_path.append([round(lat_f, 5), round(lon_f, 5)])
            last_kept_lat, last_kept_lon = lat_f, lon_f
            # Progressive thin if we are exploding memory mid-scan
            n_pts = sum(len(seg) for seg in segments) + len(cur_path)
            if n_pts > progressive_cap:
                if cur_path:
                    segments.append(cur_path)
                    cur_path = []
                    last_kept_lat = last_kept_lon = None
                segments = _thin_segments_to_cap(segments, LIFETIME_MAP_MAX_POINTS)
                progressive_cap = LIFETIME_MAP_MAX_POINTS * 2

        last_t = t

    finalize_trip()
    flush_path()
    segments = _thin_segments_to_cap(segments, LIFETIME_MAP_MAX_POINTS)
    # Drop 1-point stubs unless they are the only evidence of travel
    multi = [s for s in segments if len(s) >= 2]
    if multi:
        segments = multi

    path_points = sum(len(s) for s in segments)
    rated_km_used = rated_mi_used * 1.609344 if rated_mi_used > 0 else 0.0
    efficiency_pct = None
    if rated_km_used > 1.0 and km_total > 1.0:
        efficiency_pct = 100.0 * km_total / rated_km_used
        if efficiency_pct < 10 or efficiency_pct > 200:
            efficiency_pct = None
    # ~220 Wh/mi fleet average → Wh/km and kWh used from rated miles
    kwh_used = rated_mi_used * 0.22 if rated_mi_used > 0 else None
    wh_per_km = None
    if kwh_used is not None and km_total > 1.0:
        wh_per_km = (kwh_used * 1000.0) / km_total
    avg_kmh = (km_total / hours_total) if hours_total > 0.05 else None
    avg_temp = (temp_sum / temp_n) if temp_n else None

    # Human-readable driving time
    total_minutes = int(round(hours_total * 60.0))
    drive_days = total_minutes // (24 * 60)
    rem = total_minutes % (24 * 60)
    drive_hours = rem // 60
    drive_mins = rem % 60

    return {
        "ok": True,
        "period_weeks": int(desiredperiod) if desiredperiod else 0,
        "segments": segments,
        "path_points": path_points,
        "raw_gps_samples": raw_gps,
        "drives": drives,
        "km_driven": round(km_total, 1) if km_total > 0 else 0.0,
        "rated_km_used": round(rated_km_used, 1) if rated_km_used > 0 else 0.0,
        "wh_per_km": round(wh_per_km) if wh_per_km is not None else None,
        "efficiency_pct": round(efficiency_pct, 2) if efficiency_pct is not None else None,
        "kwh_used": round(kwh_used, 1) if kwh_used is not None else None,
        "avg_kmh": round(avg_kmh, 1) if avg_kmh is not None else None,
        "avg_temp_c": round(avg_temp, 1) if avg_temp is not None else None,
        "drive_time": {
            "days": drive_days,
            "hours": drive_hours,
            "minutes": drive_mins,
            "total_hours": round(hours_total, 2),
        },
        "has_track": path_points > 0,
    }


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


def _is_integer_percent(value):
    """True when value is a whole percent (typical Fleet API SoC)."""
    if value is None:
        return False
    return abs(float(value) - round(float(value))) < 1e-6


def _soc_delta_from_range(a, b, soc_ref, rising):
    """
    Estimate SoC change from rated battery_range delta.

    Fleet API usually reports battery_level as an integer. On short trips the
    displayed SoC does not move, while battery_range still does (tenths of a
    mile). TeslaFi historical imports already have fractional SoC — callers
    should prefer that when available.
    """
    ra, rb = a.get("battery_range"), b.get("battery_range")
    if ra is None or rb is None or soc_ref is None or soc_ref <= 1:
        return None
    if rising:
        if rb <= ra:
            return None
        delta_range = rb - ra
    else:
        if ra <= rb:
            return None
        delta_range = ra - rb
    full = ra / (soc_ref / 100.0)
    if full < 50:
        return None
    return delta_range / full * 100.0


def _drive_soc_metrics(a, b):
    """
    SoC start/end/used for a drive segment.

    Prefer API SoC when it is fractional (TeslaFi). When both ends are whole
    percents and the API delta is ~0, refine used (and displayed end) from
    battery_range so short trips still get a kWh/100 km estimate.
    """
    soc_a, soc_b = _soc(a), _soc(b)
    used_api = None
    if soc_a is not None and soc_b is not None:
        used_api = soc_a - soc_b

    used_range = _soc_delta_from_range(a, b, soc_a if soc_a is not None else soc_b, rising=False)
    api_coarse = _is_integer_percent(soc_a) and _is_integer_percent(soc_b)

    soc_used = used_api
    if used_range is not None:
        if used_api is None or used_api <= 0.05 or (api_coarse and used_range > used_api):
            soc_used = used_range

    # Keep API start; if we refined used past a flat integer end, show refined end
    display_end = soc_b
    if (
        soc_used is not None
        and soc_a is not None
        and soc_used > 0.05
        and (soc_b is None or api_coarse)
    ):
        display_end = soc_a - soc_used

    return soc_a, display_end, soc_used


def _charge_soc_metrics(a, b):
    """SoC start/end/added for a charge segment (same coarse-SoC refinement)."""
    soc_a, soc_b = _soc(a), _soc(b)
    added_api = None
    if soc_a is not None and soc_b is not None:
        added_api = soc_b - soc_a

    added_range = _soc_delta_from_range(a, b, soc_a if soc_a is not None else soc_b, rising=True)
    api_coarse = _is_integer_percent(soc_a) and _is_integer_percent(soc_b)

    soc_added = added_api
    if added_range is not None:
        if added_api is None or added_api <= 0.05 or (api_coarse and added_range > added_api):
            soc_added = added_range

    display_end = soc_b
    if (
        soc_added is not None
        and soc_a is not None
        and soc_added > 0.05
        and (soc_b is None or api_coarse)
    ):
        display_end = soc_a + soc_added

    return soc_a, display_end, soc_added


def _segment_day(rows, pack_kwh):
    """
    Build chronological drive + charge segments with metrics.
    rows: full-day samples (GPS optional).

    Drive timing vs addresses (important with sparse capture):
      - Times: first in-gear sample → first park/charge after the drive
        (do NOT start the clock on the previous parking dwell, or a 10 min
        wait at the park becomes “drive time”).
      - Addresses / odo / SoC: prefer adjacent park/charge samples so the
        map shows the real parking GPS, not the last mid-road D sample.
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
    for gi, (kind, pts) in enumerate(groups):
        if len(pts) < 1:
            continue
        a, b = pts[0], pts[-1]
        seconds = max(0.0, (b["t"] - a["t"]).total_seconds())
        minutes = seconds / 60.0
        hours = seconds / 3600.0

        if kind == "drive":
            # Timing anchors: actual drive samples only for start clock
            t_start_pt = pts[0]
            t_end_pt = pts[-1]
            # Address / metrics anchors (may use parking)
            geo_start = t_start_pt
            geo_end = t_end_pt
            if gi > 0 and groups[gi - 1][0] in ("park", "charge"):
                prev_pts = groups[gi - 1][1]
                if prev_pts:
                    geo_start = prev_pts[-1]
            if gi + 1 < len(groups) and groups[gi + 1][0] in ("park", "charge"):
                next_pts = groups[gi + 1][1]
                if next_pts:
                    # Arrival park: end clock + GPS (trip finished when parked)
                    t_end_pt = next_pts[0]
                    geo_end = next_pts[0]

            seconds = max(0.0, (t_end_pt["t"] - t_start_pt["t"]).total_seconds())
            minutes = seconds / 60.0
            hours = seconds / 3600.0

            # Need a real movement span
            if minutes < 1.0 and len(pts) < 3:
                continue
            # Odo / SoC from parking when available (full trip), else drive ends
            odo_a, odo_b = geo_start.get("odometer"), geo_end.get("odometer")
            miles = None
            if odo_a is not None and odo_b is not None and odo_b >= odo_a:
                miles = odo_b - odo_a
            # Tiny odo blips are noise
            if miles is not None and miles < 0.05 and minutes < 3:
                continue
            km = miles * 1.609344 if miles is not None else None
            soc_a, soc_b, soc_used = _drive_soc_metrics(geo_start, geo_end)
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
                    "start": t_start_pt["t"],
                    "end": t_end_pt["t"],
                    "start_local": t_start_pt["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
                    "end_local": t_end_pt["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M"),
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
                    "lat": geo_start.get("lat"),
                    "lon": geo_start.get("lon"),
                    "end_lat": geo_end.get("lat"),
                    "end_lon": geo_end.get("lon"),
                }
            )
        elif kind == "charge":
            if minutes < 1.0 and len(pts) < 2:
                continue
            soc_a, soc_b, soc_added = _charge_soc_metrics(a, b)
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
                "battery_range": float(s.battery_range)
                if s.battery_range is not None
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
def LifetimeMapData(request, hashedVin):
    """
    JSON for the personal-stats lifetime map card.

    Query: ?period=<weeks> (same values as #DesiredPeriod; 0 = all history).
    Returns polylines + summary KPIs (drives, km, efficiency, …).
    """
    if not IsValidHash(hashedVin):
        return JsonResponse(
            {"ok": False, "error": "invalid_hash"}, status=404
        )
    weeks = parse_stats_period(
        request.GET.get("period"), default=STATS_PERIOD_DEFAULT
    )
    # Allow explicit 0 (= all) via query even if not in the select list
    raw = request.GET.get("period")
    if raw is not None:
        try:
            raw_i = int(raw)
            if raw_i == 0:
                weeks = 0
        except (TypeError, ValueError):
            pass

    cache_key = f"lifetime_map_v1:{hashedVin}:{weeks}"
    cache_backend = None
    try:
        from django.core.cache import cache as cache_backend

        cached = cache_backend.get(cache_key)
        if cached is not None:
            return JsonResponse(cached)
    except Exception:
        cache_backend = None

    payload = _build_lifetime_map_payload(hashedVin, weeks)
    if cache_backend is not None:
        try:
            cache_backend.set(cache_key, payload, LIFETIME_MAP_CACHE_SECONDS)
        except Exception:
            pass
    return JsonResponse(payload)


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
    Optional query ?size=thumb|full (default full).
    """
    size = graph_size_from_request(request)
    # Computed scatter (Y = range at 100%), not a real model field on X axis alone
    if desiredfield == "range_at_100_odometer":
        if not IsValidHash(hashedVin):
            # means invalid hashedVin field was passed
            return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
        title = GetTitleForField(desiredfield)
        base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
        qs = _period_filter(degradation_scatter_queryset(base), desiredperiod)
        if not qs.exists():
            return GenerateScatterGraph(None, None, title, size=size)
        # Full period (no row cap — [:N] by Date only kept early history).
        # Daily median collapses same-day BMS jitter (~1k days max for 10y).
        results = qs.order_by("Date").values(
            "odometer",
            "battery_range",
            "Date",
            "usable_battery_level",
            "battery_level",
            "charging_state",
        )
        xvalues, yvalues = GetXandYRangeAt100(results, "odometer")
        return GenerateScatterGraph(xvalues, yvalues, title, size=size)

    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response

    title = GetTitleForField(desiredfield)
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    qs = _period_filter(degradation_scatter_queryset(base), desiredperiod)
    if not qs.exists():
        return GenerateScatterGraph(None, None, title, size=size)

    # Must cover the whole period: capping by Date truncated high-mileage history
    # (e.g. 8000 dense TeslaFi rows ≈ only the first few 10k miles).
    results = qs.order_by("Date").values(
        desiredfield,
        "battery_degradation",
        "Date",
        "usable_battery_level",
        "battery_level",
        "charging_state",
    )
    xvalues, yvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    return GenerateScatterGraph(xvalues, yvalues, title, size=size)

# returns page with firmware history for the car
class FirmwareHistoryView(SingleTableView):
    model = TeslaFirmwareHistory
    table_class = TeslaFirmwareHistoryTable
    template_name = 'personalstats/FirmwareHistory.html'


def _firmware_timeline(entries_chrono):
    """
    Build UI timeline points from firmware rows ordered oldest → newest,
    then reverse for display: newest on the left (no scroll), past to the right.

    Each item: first-seen date, version label, days until the next version
    (or until today for the current build).
    """
    from datetime import date as date_cls

    today = date_cls.today()
    items = []
    n = len(entries_chrono)
    for i, row in enumerate(entries_chrono):
        start = row.Date
        if i + 1 < n:
            end = entries_chrono[i + 1].Date
        else:
            end = today
        days_on = None
        if start and end:
            try:
                days_on = max(0, (end - start).days)
            except Exception:
                days_on = None
        ver = (row.Version or "").strip()
        # "2025.20.3 8252e1d331" → primary "2025.20.3", build hash aside
        parts = ver.split(None, 1)
        items.append(
            {
                "date": start,
                "version": ver,
                "version_short": parts[0] if parts else ver,
                "version_build": parts[1] if len(parts) > 1 else "",
                "days_on": days_on,
                "is_current": not row.IsArchive and i == n - 1,
                "is_archive": bool(row.IsArchive),
            }
        )
    # Newest first (left); scroll right for older builds
    items.reverse()
    return items


# Display page with car firmware history
def FirmwareHistory(request, hashedVin):
    # see https://django-tables2.readthedocs.io/en/latest/pages/table-data.html
    if not IsValidHash(hashedVin):
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    qs_desc = TeslaFirmwareHistory.objects.filter(hashedVin=hashedVin).order_by(
        "-Date", "-id"
    )
    qs_chrono = list(
        TeslaFirmwareHistory.objects.filter(hashedVin=hashedVin).order_by("Date", "id")
    )
    table = TeslaFirmwareHistoryTable(qs_desc)
    context = _vehicle_chrome_context(request, hashedVin)
    context["table"] = table
    context["firmware_timeline"] = _firmware_timeline(qs_chrono)
    return render(request, "personalstats/FirmwareHistory.html", context)


# returns CSV with firmware history for the car
def FirmwareHistoryCSV(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    if TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count() == 0:
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    query = (
        'select "Version","Date" from matesla_teslafirmwarehistory '
        f"where \"hashedVin\"='{hashedVin}' order by 2 desc;"
    )
    return PrepareCSVFromQuery(query)
