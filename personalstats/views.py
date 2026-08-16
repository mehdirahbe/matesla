import django
from django.db.models import Max, Min, Avg, Count, F, FloatField, Case, When, Q
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.template import loader
from django.views.decorators.http import require_GET
from django_tables2 import SingleTableView
from matplotlib.dates import DateFormatter, MonthLocator, num2date
from matplotlib.ticker import FuncFormatter, MultipleLocator

from matesla.degradation_graphs import (
    PrepareCSVFromQuery,
    GetXandYFromBatteryDegradResult,
    GenerateScatterGraph,
    GeneratePngFromGraph,
    degradation_scatter_queryset,
    aggregate_scatter_daily_median,
    load_degradation_scatter_xy,
    DEGRADATION_SCATTER_FALLBACK_SOC,
)
from matesla.graphstyle import (
    ACCENT,
    ACCENT_SOFT,
    AXES_BG,
    DANGER,
    ENERGY,
    MUTED,
    SERIES_COLORS,
    SPINE,
    TEXT,
    WARM,
    finish_figure,
    graph_size_from_request,
    make_figure,
    style_axes,
    style_legend,
    style_suptitle,
)
from matesla.models.FleetApiCall import FleetApiCall, KIND_VEHICLE_DATA
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.VinHash import IsKnownHashedVin, IsValidHash
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.translation import get_language, gettext as _
from datetime import date, datetime, timedelta, timezone as dt_timezone
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo
import json
import re

# Short-lived PNG cache (per car / field / period / size / language).
# Cuts repeat loads of the stats grid; invalidate by waiting or process restart
# (LocMem). Not a substitute for correct period filters.
GRAPH_PNG_CACHE_SECONDS = 300

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
        "fleet_poll_cost",
    }
)

# Tesla Fleet "Data" (vehicle_data) pay-per-use: 500 requests / $1 (USD list price).
# Local estimate from MaTesla capture samples only (this car).
FLEET_DATA_USD_PER_REQUEST = 0.002
# Approximate USD→EUR for EU Fleet apps (Tesla portal converts; not live ECB).
FLEET_USD_TO_EUR = 0.92
# Cost graph is always day-by-day and never longer than this.
FLEET_POLL_MAX_DAYS = 30

# Calendar "day" for history maps: user mental model is local civil date, not UTC midnight
DAY_MAP_TZ = ZoneInfo("Europe/Brussels")
# Max points sent to the browser for the polyline (downsample long TeslaFi days)
DAY_MAP_MAX_POINTS = 800
# Min parked duration (minutes) to list a stop
DAY_MAP_STOP_MIN_MINUTES = 8
# Speed at or below this (mi/h) counts as stopped
DAY_MAP_STOP_SPEED = 1.0
# Day-end GPS vs last drive arrival: flag a missing tail trip (sparse capture)
DAYMAP_TAIL_GAP_MIN_M = 150.0  # ignore GPS noise / same parking bay
DAYMAP_TAIL_GAP_SHORT_MAX_M = 2500.0  # ≤ this → likely too short for poll interval
# Efficiency charts (inspired by TeslaFi; drives ≥ 10 km)
EFFICIENCY_MIN_KM = 10.0
EFFICIENCY_MIN_PCT = 25.0
EFFICIENCY_MAX_PCT = 140.0
EFFICIENCY_SPEED_BIN_KMH = 5
EFFICIENCY_TEMP_BIN_C = 5
# Gap longer than this between drive samples starts a new trip
EFFICIENCY_TRIP_GAP = timedelta(minutes=15)
# Cap drive samples for binning (enough for stable histograms; not raw time series)
EFFICIENCY_MAX_DRIVE_ROWS = 8000
# Lifetime map (stats page): path + summary KPIs for the selected period
LIFETIME_MAP_MAX_POINTS = 4000
# Split polyline when consecutive drive samples are farther apart than this
LIFETIME_MAP_GAP = timedelta(minutes=30)
# Ignore GPS jitter while "driving" but barely moving (~180 m)
LIFETIME_MAP_MIN_MOVE_M = 180.0
# Count as a trip for KPIs if at least this distance (km)
LIFETIME_MAP_MIN_TRIP_KM = 1.0
# Soft cache so flipping chart period does not re-scan every time
LIFETIME_MAP_CACHE_SECONDS = 600
# Even-stride cap: process at most this many drive-GPS rows for lifetime map
LIFETIME_MAP_MAX_SCAN = 16000
# Charge sessions shorter than this are ignored (plug glitches)
CHARGE_SESSION_MIN_MINUTES = 5
CHARGE_SESSION_GAP = timedelta(minutes=30)
# Max charging samples scanned for session histograms (even stride if denser)
CHARGE_SESSION_MAX_SCAN = 40000
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
DRIVE_SENSOR_MAX_SCAN = 25000
# When integrating power×Δt to kWh, ignore gaps larger than this
# (TeslaFi / adaptive capture can be 10–60 s between drive samples)
DRIVE_POWER_ENERGY_MAX_GAP = timedelta(seconds=90)

# --- Drives leaderboard (personalstats/Drives) ---
# Skip short errands (Delhaize / school hop); TeslaFi-style "real trips" only.
DRIVES_MIN_KM = 20.0
# Max trips kept after scan (per period). Ranking is among these; display pages
# slice further. Does not reduce scan cost — scan is the bottleneck.
DRIVES_MAX_TRIPS = 100
DRIVES_CACHE_SECONDS = 900
DRIVES_PAGE_SIZES = frozenset({10, 25, 50})
DRIVES_DEFAULT_PAGE_SIZE = 25
# Gap between successive *drive* samples starts a new trip (park/charge omitted
# from the scan for speed — same idea as efficiency charts).
DRIVES_TRIP_GAP = timedelta(minutes=15)
# sort_key → (trip dict field, reverse for "higher is more extreme")
DRIVES_SORT_SPECS = {
    "longest": ("km", True),
    "elev_up": ("elev_gain_m", True),
    "elev_down": ("elev_loss_m", True),
    "hot": ("temp_max_c", True),
    "cold": ("temp_min_c", False),  # lower temp = colder
    # Lowest arrival SoC first — "arrived near empty"
    "soc_end": ("soc_end", False),
}
DRIVES_SORT_DEFAULT = "longest"


def GetTitleForFieldDico(unit=None):
    from matesla.units import unit_labels

    dist = unit_labels(unit)["distance"]
    return {
        "outside_temp": _("Outside temperature (°C)"),
        "driver_temp_setting": _("Driver temperature (°C)"),
        "inside_temp": _("Inside temperature (°C)"),
        "passenger_temp_setting": _("Passenger temperature (°C)"),
        "odometer": _("Odometer (%(u)s)") % {"u": dist},
        # Drive samples histogram (API stores mph; converted at bin time)
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
        "est_battery_range": _("Estimated battery range (%(u)s)") % {"u": dist},
        "usable_battery_level": _("Usable battery level (%)"),
        "battery_degradation": _("Battery degradation (%)"),
        # Extrapolated full-charge range: battery_range / soc * 100
        # (wording avoids bare "%" — breaks gettext python-format matching)
        "range_at_100": _("Range at full charge (%(u)s)") % {"u": dist},
        "range_at_100_odometer": _("Range at full charge vs odometer (%(u)s)")
        % {"u": dist},
        # Trip efficiency vs rated range drop (100% = matched EPA-rated prediction)
        "efficiency_by_speed": _("Efficiency vs average speed"),
        "efficiency_by_temp": _("Efficiency vs outside temperature"),
        # Local estimate of Fleet Data $ from stored vehicle_data samples
        "fleet_poll_cost": _("Fleet poll cost (estimate)"),
    }


# Return a nice title for field
def GetTitleForField(field, unit=None):
    if field is None:
        return field
    dico = GetTitleForFieldDico(unit)
    if field in dico:
        return dico[field]
    # not found, return as is
    return field


# Snapshot fields stored in miles that should convert for km display
_MILES_VALUE_FIELDS = frozenset(
    {
        "odometer",
        "battery_range",
        "est_battery_range",
        "ideal_battery_range",
        "range_at_100",
    }
)


def _scale_miles_series(values, unit):
    """Convert a list of mile values to the active display unit (km or mi)."""
    from matesla.units import is_km, MILES_TO_KM

    if values is None:
        return values
    if not is_km(unit):
        return values
    return [v * MILES_TO_KM if v is not None else None for v in values]


def _range_at_100_from_entry(entry):
    """Rated range extrapolated to 100% SoC (miles). Same basis as degradation."""
    battery_range = entry.battery_range
    level = entry.usable_battery_level
    if level is None or level <= 0:
        level = entry.battery_level
    if battery_range is None or level is None or level <= 0:
        return None
    return float(battery_range) / float(level) * 100.0


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
        battery_range = _entry_get(entry, "battery_range")
        if battery_range is None or level is None or float(level) <= 0:
            continue
        range_at_100 = float(battery_range) / float(level) * 100.0
        x_value = _entry_get(entry, xfield)
        if x_value is None:
            continue
        xvalues.append(x_value)
        yvalues.append(range_at_100)
        dates.append(_entry_get(entry, "Date"))
    if daily_median:
        return aggregate_scatter_daily_median(xvalues, yvalues, dates)
    return xvalues, yvalues


def annotate_range_at_100(queryset):
    """Add computed range_at_100 column (miles) for aggregation."""
    return queryset.annotate(
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

def _fleet_poll_window_days(desiredperiod) -> int:
    """Honor period selector but never exceed FLEET_POLL_MAX_DAYS (day-by-day)."""
    if desiredperiod is None or desiredperiod <= 0:
        return FLEET_POLL_MAX_DAYS
    # desiredperiod is weeks on the stats page; 1 Month (4) and above → full 30 days
    week_days = int(desiredperiod) * 7
    if week_days >= FLEET_POLL_MAX_DAYS:
        return FLEET_POLL_MAX_DAYS
    return max(1, week_days)


def _fleet_cost_currency() -> tuple[str, str, float]:
    """
    Display currency for Fleet Data estimates.

    Tesla list price is USD; EU developer apps usually see EUR on the portal.
    We key off the configured Fleet API region (not VIN plant — Shanghai-built
    cars used in Europe still bill on the EU app).
    Returns (code, symbol, price_per_request_in_that_currency).
    """
    base = ""
    try:
        from matesla.models.TeslaAppSettings import TeslaAppSettings

        settings_row = TeslaAppSettings.objects.order_by("pk").first()
        if settings_row and settings_row.api_base:
            base = settings_row.api_base
    except Exception:
        pass
    if not base:
        try:
            from matesla.TeslaConnect import fleet_api_base

            base = fleet_api_base() or ""
        except Exception:
            base = ""
    low = base.lower()
    if ".eu." in low or "eu.vn" in low:
        return (
            "EUR",
            "€",
            FLEET_DATA_USD_PER_REQUEST * FLEET_USD_TO_EUR,
        )
    # NA and rest of world: keep Tesla's USD list price
    return "USD", "$", FLEET_DATA_USD_PER_REQUEST


def _fmt_fleet_money(amount: float, symbol: str) -> str:
    if amount >= 0.01:
        return f"{symbol}{amount:.2f}"
    return f"{symbol}{amount:.3f}"


def _fleet_poll_buckets(hashed_vin: str, *, days: int):
    """
    Count billable vehicle_data HTTP calls per local civil day (Europe/Brussels).

    Source of truth: FleetApiCall rows logged at request time (not snapshots).
    Tesla bills status < 500; we store that as billable=True.

    Returns (labels, counts) for the last `days` local days including today.
    """
    from collections import Counter

    days = max(1, min(int(days), FLEET_POLL_MAX_DAYS))
    now_local = timezone.now().astimezone(DAY_MAP_TZ)
    end = now_local.date()
    start = end - timedelta(days=days - 1)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=DAY_MAP_TZ)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=DAY_MAP_TZ)

    day_counts: Counter = Counter()
    for raw in (
        FleetApiCall.objects.filter(
            kind=KIND_VEHICLE_DATA,
            billable=True,
            hashedVin=hashed_vin,
            at__gte=start_dt,
            at__lt=end_dt,
        )
        .order_by("at")
        .values_list("at", flat=True)
        .iterator()
    ):
        if raw is None:
            continue
        dt = raw
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, dt_timezone.utc)
        local = dt.astimezone(DAY_MAP_TZ)
        day_counts[local.date()] += 1

    language = get_language() or "en"
    labels = []
    counts = []
    cursor = start
    # US-style mm/dd only for English; European locales use day-first.
    day_first = not language.startswith("en")
    while cursor <= end:
        if day_first:
            labels.append(cursor.strftime("%d/%m"))
        else:
            labels.append(cursor.strftime("%m/%d"))
        counts.append(int(day_counts.get(cursor, 0)))
        cursor += timedelta(days=1)
    return labels, counts


def GenerateFleetPollCostGraph(
    labels,
    counts,
    title,
    size="full",
    *,
    currency_code: str = "USD",
    currency_symbol: str = "$",
    price_per_request: float = FLEET_DATA_USD_PER_REQUEST,
):
    """
    Bar chart: estimated Fleet Data cost per day for one car (MaTesla only).

    Y-axis = display currency (EUR for EU Fleet apps, else USD).
    """
    figure, style_config = make_figure(size, bar=True)
    axes = figure.subplots()
    total_n = sum(counts) if counts else 0
    total_cost = total_n * price_per_request

    if labels and counts and total_n > 0:
        cost_per_bar = [c * price_per_request for c in counts]
        x = list(range(len(labels)))
        bars = axes.bar(
            x,
            cost_per_bar,
            color=ACCENT_SOFT,
            edgecolor=ACCENT,
            linewidth=0.5,
            alpha=0.92,
            zorder=2,
        )
        ymax = max(cost_per_bar) if cost_per_bar else price_per_request
        axes.set_ylim(0, ymax * 1.22)
        label_font = max(5.5, style_config["tick_size"] - 1.0)
        annotate = len(labels) <= 31
        for bar, count, cost in zip(bars, counts, cost_per_bar):
            if count <= 0 or not annotate:
                continue
            text = f"{count}\n{_fmt_fleet_money(cost, currency_symbol)}"
            axes.annotate(
                text,
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 2),
                textcoords="offset points",
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=label_font,
                zorder=3,
                clip_on=False,
            )
        axes.set_xticks(x)
        rotation = 45 if len(labels) > 14 else 22
        axes.set_xticklabels(labels, rotation=rotation, ha="right")
        if currency_code == "EUR":
            axes.set_ylabel(_("Estimated cost (EUR)"), color=MUTED)
        else:
            axes.set_ylabel(_("Estimated cost (USD)"), color=MUTED)
        axes.set_xlabel(_("Day"), color=MUTED)

        # Secondary axis: raw request counts (same bars, different scale)
        axes2 = axes.twinx()
        axes2.set_ylim(
            axes.get_ylim()[0] / price_per_request,
            axes.get_ylim()[1] / price_per_request,
        )
        axes2.set_ylabel(_("Data requests"), color=MUTED)
        axes2.tick_params(colors=MUTED, labelsize=style_config["tick_size"])
        for spine in axes2.spines.values():
            spine.set_color(SPINE)
            spine.set_linewidth(style_config["spine_width"])
        axes2.set_facecolor(AXES_BG)
        axes2.grid(False)

        foot = _(
            "n=%(n)s billable vehicle_data · est. %(money)s (@ %(rate)s/req) · "
            "this car · from request log · max %(days)s days"
        ) % {
            "n": total_n,
            "money": _fmt_fleet_money(total_cost, currency_symbol),
            "rate": _fmt_fleet_money(price_per_request, currency_symbol),
            "days": FLEET_POLL_MAX_DAYS,
        }
        figure.text(
            0.5,
            0.01,
            foot,
            ha="center",
            va="bottom",
            color=MUTED,
            fontsize=max(6.0, style_config["tick_size"] - 0.5),
        )
        finish_figure(figure, axes, title, style_config)
        try:
            figure.tight_layout(rect=(0.02, 0.06, 0.98, 0.92))
        except Exception:
            pass
    else:
        axes.text(
            0.5,
            0.5,
            _("No logged vehicle_data calls in this period"),
            ha="center",
            va="center",
            color=MUTED,
            fontsize=style_config["label_size"],
            transform=axes.transAxes,
        )
        finish_figure(figure, axes, title, style_config)

    return GeneratePngFromGraph(figure, size=size)


def _last_series_point(dates, values):
    """Last (date, y) already plotted — walk back past trailing nulls."""
    if not dates or not values:
        return None, None
    for day, value in zip(reversed(dates), reversed(values)):
        if value is None:
            continue
        try:
            return day, float(value)
        except (TypeError, ValueError):
            continue
    return None, None


def _odometer_graph_footer(display_value, when, unit):
    """Footer under the odometer chart: Y of the last plotted point."""
    from django.utils.formats import date_format
    from matesla.units import format_number, unit_labels

    text = format_number(display_value, 0)
    if text is None:
        return None
    dist = f"{text} {unit_labels(unit)['distance']}"
    if when is None:
        return _("Latest reading: %(dist)s") % {"dist": dist}
    return _("Latest reading: %(dist)s · %(date)s") % {
        "dist": dist,
        "date": date_format(when, format="SHORT_DATE_FORMAT", use_l10n=True),
    }


def GenerateDateGraph(
    datesList, maxvalues, minvalues, avgvalues, title, size="full", footer=None
):
    # matplotlib 3.9+ removed Axes.plot_date — use plot() with date objects
    figure, style_config = make_figure(size)

    language = django.utils.translation.get_language() or "en"
    if language.startswith("en"):
        formatter = DateFormatter("%m/%d/%y")
    else:
        formatter = DateFormatter("%d/%m/%y")

    axes = figure.subplots()
    if datesList is not None and minvalues is not None and len(datesList) > 0:
        line_width = style_config["linewidth"]
        # Lines only (no markers) — clearer on dense multi-week series
        axes.plot(
            datesList,
            minvalues,
            color=SERIES_COLORS[0],
            linestyle="-",
            linewidth=line_width,
            label=_("Minimum"),
            zorder=2,
        )
        axes.plot(
            datesList,
            avgvalues,
            color=SERIES_COLORS[1],
            linestyle="-",
            linewidth=line_width + 0.25,
            label=_("Average"),
            zorder=3,
        )
        axes.plot(
            datesList,
            maxvalues,
            color=SERIES_COLORS[2],
            linestyle="-",
            linewidth=line_width,
            label=_("Maximum"),
            zorder=2,
        )
        style_legend(axes, style_config)
        axes.xaxis.set_major_formatter(formatter)
        # One day of data still plots fine; widen x-axis so a single point is not clipped
        if len(datesList) == 1:
            only_day = datesList[0]
            axes.set_xlim(
                only_day - timedelta(days=1), only_day + timedelta(days=1)
            )
        figure.autofmt_xdate()
    finish_figure(figure, axes, title, style_config)
    if footer:
        figure.text(
            0.5,
            0.01,
            footer,
            ha="center",
            va="bottom",
            color=MUTED,
            fontsize=max(6.0, style_config["tick_size"] - 0.5),
        )
        try:
            figure.tight_layout(rect=(0.02, 0.06, 0.98, 0.92))
        except Exception:
            pass
    return GeneratePngFromGraph(figure, size=size)


# Only real snapshot columns allowed in raw monthly-temp SQL (injection-safe).
_MONTHLY_TEMP_FIELDS = frozenset({"outside_temp", "inside_temp"})


def _monthly_temp_series(hashed_vin, field_name, desiredperiod=None):
    """
    Build one min / average / max temperature (°C) per calendar month.

    Daily aggregates first (DateOnlyDay index), then roll into months so the
    monthly average is the mean of *daily* averages (not sample-weighted).
    Raw SQL avoids ORM annotate overhead on multi-year cars (~500k samples).
    """
    if field_name not in _MONTHLY_TEMP_FIELDS:
        raise ValueError(f"unsupported temp field: {field_name}")

    where = [
        "hashedVin = %s",
        f"{field_name} IS NOT NULL",
        "DateOnlyDay IS NOT NULL",
    ]
    params: list = [hashed_vin]
    mindate = _lifetime_map_period_mindate(desiredperiod)
    if mindate is not None:
        where.append("DateOnlyDay >= %s")
        params.append(mindate.date() if hasattr(mindate, "date") else mindate)

    where_sql = " AND ".join(where)
    table = TeslaCarDataSnapshot._meta.db_table
    # Nested: daily min/max/avg → monthly min/max and AVG(daily avg).
    # HAVING drops sensor glitches (same clamps as the former Python path).
    sql = f"""
        SELECT y, m, MIN(dmin), MAX(dmax), AVG(davg)
        FROM (
            SELECT CAST(strftime('%%Y', DateOnlyDay) AS INTEGER) AS y,
                   CAST(strftime('%%m', DateOnlyDay) AS INTEGER) AS m,
                   MIN({field_name}) AS dmin,
                   MAX({field_name}) AS dmax,
                   AVG({field_name}) AS davg
            FROM {table}
            WHERE {where_sql}
            GROUP BY DateOnlyDay
            HAVING MIN({field_name}) >= -50
               AND MAX({field_name}) <= 90
               AND MIN({field_name}) <= MAX({field_name})
        )
        GROUP BY y, m
        ORDER BY y, m
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    month_dates = []
    monthly_minimums = []
    monthly_maximums = []
    monthly_averages = []
    for year, month, month_min, month_max, month_avg in rows:
        if year is None or month is None:
            continue
        if month_min is None or month_max is None or month_avg is None:
            continue
        if month_min > month_max:
            continue
        try:
            month_dates.append(date(int(year), int(month), 1))
            monthly_minimums.append(float(month_min))
            monthly_maximums.append(float(month_max))
            monthly_averages.append(float(month_avg))
        except (TypeError, ValueError):
            continue
    return month_dates, monthly_minimums, monthly_maximums, monthly_averages


def _graph_png_cache_key(
    hashed_vin,
    desired_field,
    desired_period_weeks,
    size,
    *,
    kind="stats",
    unit=None,
):
    """
    Cache key for generated graph PNGs.

    `kind` separates endpoints that share a field name (e.g. odometer time-series
    on StatsOnCarGraph vs odometer scatter on BatteryDegradationGraph).
    Language and distance unit are included because axis titles / scales differ.
    """
    from matesla.units import normalize_unit

    language = get_language() or "en"
    dist_unit = normalize_unit(unit)
    return (
        f"matesla:png:v5:{kind}:{hashed_vin}:{desired_field}:"
        f"{desired_period_weeks}:{size}:{language}:{dist_unit}"
    )


def _png_response_from_bytes(png_bytes: bytes, size: str, *, cache_status: str):
    """Build a graph PNG HttpResponse (headers aligned with render_png)."""
    response = HttpResponse(png_bytes, content_type="image/png")
    response["Content-Length"] = str(len(png_bytes))
    response["Cache-Control"] = "private, max-age=120"
    response["X-MaTesla-Graph-Size"] = "thumb" if size == "thumb" else "full"
    response["X-MaTesla-Graph-Cache"] = cache_status
    return response


def _cache_graph_png(cache_key: str, response: HttpResponse, size: str):
    """
    Store successful PNG bodies in LocMem for a few minutes.

    Speeds period switching when the user revisits a window already computed.
    """
    if (
        response is not None
        and getattr(response, "status_code", 500) == 200
        and response.content
        and response.content[:8] == b"\x89PNG\r\n\x1a\n"
    ):
        try:
            cache.set(cache_key, response.content, GRAPH_PNG_CACHE_SECONDS)
        except Exception:
            pass
        response["X-MaTesla-Graph-Cache"] = "MISS"
    return response


def _format_month_label(month_date, language):
    """Short month label for record annotations on climate charts."""
    if month_date is None:
        return ""
    # Numeric mm/YYYY for non-English (locale-independent, matches FR charts).
    if language and not language.startswith("en"):
        return month_date.strftime("%m/%Y")
    return month_date.strftime("%b %Y")


def _calendar_month_index(month_date):
    """Absolute month number for gap detection (year * 12 + month)."""
    if isinstance(month_date, datetime):
        return month_date.year * 12 + month_date.month
    return month_date.year * 12 + month_date.month


def _split_monthly_segments(month_dates, monthly_minimums, monthly_maximums, monthly_averages):
    """
    Split monthly series into contiguous calendar-month runs.

    Why: if logging has a hole of one or more months, drawing one continuous
    line invents a fake slope across the gap. Each segment is plotted separately.
    """
    if not month_dates:
        return []
    segments = []
    segment_start_index = 0
    for index in range(1, len(month_dates)):
        months_between = (
            _calendar_month_index(month_dates[index])
            - _calendar_month_index(month_dates[index - 1])
        )
        if months_between > 1:
            segments.append(
                (
                    month_dates[segment_start_index:index],
                    monthly_minimums[segment_start_index:index],
                    monthly_maximums[segment_start_index:index],
                    monthly_averages[segment_start_index:index],
                )
            )
            segment_start_index = index
    segments.append(
        (
            month_dates[segment_start_index:],
            monthly_minimums[segment_start_index:],
            monthly_maximums[segment_start_index:],
            monthly_averages[segment_start_index:],
        )
    )
    return segments


def GenerateMonthlyTempRibbonGraph(
    month_dates, monthly_minimums, monthly_maximums, monthly_averages, title, size="full"
):
    """
    Monthly temperature ribbon chart for personal stats climate cards.

    Draws a filled band between monthly min and max, a yellow average line,
    and annotates the period record cold (lowest monthly min) and hot
    (highest monthly max). Logging gaps of ≥1 month break the series so we
    never invent a straight line across missing data.
    """
    figure, style_config = make_figure(size)
    language = django.utils.translation.get_language()

    def year_axis_tick_label(axis_value, _position=None):
        """Label only January major ticks (year anchors); other quarter ticks stay bare."""
        try:
            tick_date = num2date(axis_value)
        except (ValueError, OverflowError, TypeError):
            return ""
        if tick_date.month != 1:
            return ""
        if language is not None and not language.startswith("en"):
            return tick_date.strftime("%m/%Y")
        return tick_date.strftime("%b %Y")

    axes = figure.subplots()
    if month_dates and monthly_minimums and monthly_maximums and len(month_dates) > 0:
        line_width = style_config["linewidth"]

        def mid_month_datetime(month_date):
            """Place each monthly point on the 15th for cleaner x spacing."""
            if isinstance(month_date, datetime):
                return month_date
            return datetime(month_date.year, month_date.month, 15)

        # Full x series for record markers (all months that have data)
        all_x_positions = [mid_month_datetime(month_date) for month_date in month_dates]
        contiguous_segments = _split_monthly_segments(
            month_dates, monthly_minimums, monthly_maximums, monthly_averages
        )

        for segment_index, (
            segment_months,
            segment_minimums,
            segment_maximums,
            segment_averages,
        ) in enumerate(contiguous_segments):
            segment_x = [mid_month_datetime(month_date) for month_date in segment_months]
            # Legend labels only on the first segment so entries are not duplicated
            label_range = _("Monthly range (min–max)") if segment_index == 0 else None
            label_minimum = _("Monthly minimum") if segment_index == 0 else None
            label_average = _("Monthly average") if segment_index == 0 else None
            label_maximum = _("Monthly maximum") if segment_index == 0 else None

            if len(segment_x) == 1:
                # Isolated month (surrounded by logging gaps): markers only, no fake line
                axes.fill_between(
                    [segment_x[0], segment_x[0]],
                    [segment_minimums[0], segment_minimums[0]],
                    [segment_maximums[0], segment_maximums[0]],
                    color=ACCENT,
                    alpha=0.28,
                    linewidth=0,
                    zorder=1,
                    label=label_range,
                )
                axes.plot(
                    segment_x,
                    segment_minimums,
                    color=SERIES_COLORS[0],
                    marker="o",
                    markersize=style_config["markersize"] + 1.5,
                    linestyle="None",
                    zorder=2,
                    label=label_minimum,
                )
                axes.plot(
                    segment_x,
                    segment_averages,
                    color=SERIES_COLORS[1],
                    marker="o",
                    markersize=style_config["markersize"] + 1.5,
                    linestyle="None",
                    zorder=3,
                    label=label_average,
                )
                axes.plot(
                    segment_x,
                    segment_maximums,
                    color=SERIES_COLORS[2],
                    marker="o",
                    markersize=style_config["markersize"] + 1.5,
                    linestyle="None",
                    zorder=2,
                    label=label_maximum,
                )
                continue

            axes.fill_between(
                segment_x,
                segment_minimums,
                segment_maximums,
                color=ACCENT,
                alpha=0.28,
                linewidth=0,
                zorder=1,
                label=label_range,
            )
            axes.plot(
                segment_x,
                segment_minimums,
                color=SERIES_COLORS[0],
                linestyle="-",
                linewidth=line_width,
                alpha=0.9,
                zorder=2,
                label=label_minimum,
            )
            axes.plot(
                segment_x,
                segment_averages,
                color=SERIES_COLORS[1],
                linestyle="-",
                linewidth=line_width + 0.35,
                zorder=3,
                label=label_average,
            )
            axes.plot(
                segment_x,
                segment_maximums,
                color=SERIES_COLORS[2],
                linestyle="-",
                linewidth=line_width,
                alpha=0.9,
                zorder=2,
                label=label_maximum,
            )

        # Record cold = lowest monthly minimum; record hot = highest monthly maximum
        record_min_index = min(
            range(len(monthly_minimums)), key=lambda index: monthly_minimums[index]
        )
        record_max_index = max(
            range(len(monthly_maximums)), key=lambda index: monthly_maximums[index]
        )
        record_minimum_celsius = monthly_minimums[record_min_index]
        record_maximum_celsius = monthly_maximums[record_max_index]
        record_minimum_month = month_dates[record_min_index]
        record_maximum_month = month_dates[record_max_index]
        record_minimum_x = all_x_positions[record_min_index]
        record_maximum_x = all_x_positions[record_max_index]

        # Highlight record cold / hot months with markers + text callouts
        axes.scatter(
            [record_minimum_x],
            [record_minimum_celsius],
            s=style_config["scatter_size"] + 18,
            color=SERIES_COLORS[0],
            edgecolors=TEXT,
            linewidths=0.6,
            zorder=5,
        )
        axes.scatter(
            [record_maximum_x],
            [record_maximum_celsius],
            s=style_config["scatter_size"] + 18,
            color=SERIES_COLORS[2],
            edgecolors=TEXT,
            linewidths=0.6,
            zorder=5,
        )

        annotation_font_size = style_config["tick_size"]
        record_minimum_label = _("Record min %(t).1f °C · %(when)s") % {
            "t": record_minimum_celsius,
            "when": _format_month_label(record_minimum_month, language),
        }
        record_maximum_label = _("Record max %(t).1f °C · %(when)s") % {
            "t": record_maximum_celsius,
            "when": _format_month_label(record_maximum_month, language),
        }
        temperature_span = (
            max(monthly_maximums) - min(monthly_minimums)
            if monthly_maximums and monthly_minimums
            else 10.0
        )
        vertical_padding = max(1.5, temperature_span * 0.06)
        axes.annotate(
            record_minimum_label,
            xy=(record_minimum_x, record_minimum_celsius),
            xytext=(0, -14),
            textcoords="offset points",
            ha="center",
            va="top",
            color=SERIES_COLORS[0],
            fontsize=annotation_font_size,
            zorder=6,
            clip_on=False,
        )
        axes.annotate(
            record_maximum_label,
            xy=(record_maximum_x, record_maximum_celsius),
            xytext=(0, 12),
            textcoords="offset points",
            ha="center",
            va="bottom",
            color=SERIES_COLORS[2],
            fontsize=annotation_font_size,
            zorder=6,
            clip_on=False,
        )

        axes.set_ylim(
            min(monthly_minimums) - vertical_padding * 2.2,
            max(monthly_maximums) + vertical_padding * 2.8,
        )
        style_legend(axes, style_config)
        # Graduations: small tick every month (no label), larger every 3 months
        # (Jan/Apr/Jul/Oct). Labels only on January so multi-year stays readable.
        month_point_count = len(all_x_positions)
        axes.xaxis.set_minor_locator(MonthLocator())
        axes.xaxis.set_major_locator(MonthLocator(bymonth=(1, 4, 7, 10)))
        axes.xaxis.set_major_formatter(FuncFormatter(year_axis_tick_label))
        axes.tick_params(axis="x", which="minor", labelbottom=False)
        if month_point_count == 1:
            axes.set_xlim(
                all_x_positions[0] - timedelta(days=40),
                all_x_positions[0] + timedelta(days=40),
            )
        style_axes(axes, style_config)
        # Re-apply locators after style_axes (it may reset tick styling)
        axes.xaxis.set_minor_locator(MonthLocator())
        axes.xaxis.set_major_locator(MonthLocator(bymonth=(1, 4, 7, 10)))
        axes.tick_params(
            axis="x",
            which="major",
            length=8.0,
            width=1.0,
            colors=MUTED,
            labelsize=style_config["tick_size"],
        )
        axes.tick_params(
            axis="x",
            which="minor",
            length=3.2,
            width=0.55,
            colors=MUTED,
            labelbottom=False,
        )
        for label in axes.get_xticklabels():
            label.set_rotation(0)
            label.set_ha("center")
        # Y: small mark every 5 °C, larger every 10 °C (labels only on 10 °C majors)
        axes.yaxis.set_minor_locator(MultipleLocator(5))
        axes.yaxis.set_major_locator(MultipleLocator(10))
        axes.tick_params(
            axis="y",
            which="major",
            length=7.5,
            width=0.9,
            colors=MUTED,
            labelsize=style_config["tick_size"],
        )
        axes.tick_params(
            axis="y",
            which="minor",
            length=3.2,
            width=0.55,
            colors=MUTED,
            labelleft=False,
        )
        style_suptitle(figure, title, style_config)
        try:
            figure.tight_layout(rect=(0.02, 0.07, 0.98, 0.90))
        except Exception:
            pass
        figure.text(
            0.5,
            0.01,
            _(
                "One min / avg / max per calendar month · band = monthly range "
                "· gaps = no data"
            ),
            ha="center",
            va="bottom",
            color=MUTED,
            fontsize=style_config["tick_size"] - 0.5,
        )
    else:
        finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)


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
    current_segment = [rows[0]]
    for sample in rows[1:]:
        prev_time, sample_time = current_segment[-1]["t"], sample["t"]
        if (
            prev_time is None
            or sample_time is None
            or (sample_time - prev_time) > EFFICIENCY_TRIP_GAP
        ):
            if len(current_segment) >= 2:
                segments.append(current_segment)
            current_segment = [sample]
        else:
            current_segment.append(sample)
    if len(current_segment) >= 2:
        segments.append(current_segment)

    trips = []
    for points in segments:
        start_pt, end_pt = points[0], points[-1]
        seconds = max(0.0, (end_pt["t"] - start_pt["t"]).total_seconds())
        if seconds < 60:
            continue
        odo_start, odo_end = start_pt.get("odometer"), end_pt.get("odometer")
        if odo_start is None or odo_end is None or odo_end < odo_start:
            continue
        miles = float(odo_end) - float(odo_start)
        km = miles * 1.609344
        if km < EFFICIENCY_MIN_KM:
            continue
        range_start = _rated_range_miles(
            start_pt.get("battery_range"), start_pt.get("ideal_battery_range")
        )
        range_end = _rated_range_miles(
            end_pt.get("battery_range"), end_pt.get("ideal_battery_range")
        )
        eff = _trip_efficiency_pct(range_start, range_end, miles)
        if eff is None:
            continue
        hours = seconds / 3600.0
        avg_kmh = km / hours if hours > 0.01 else None
        if avg_kmh is None or avg_kmh < 5 or avg_kmh > 160:
            continue
        temps = []
        for sample in points:
            outside_temp = sample.get("outside_temp")
            if outside_temp is not None:
                try:
                    temps.append(float(outside_temp))
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
    for trip in trips:
        val = trip.get(key)
        if val is None:
            continue
        start = int(val // bin_width) * bin_width
        if key == "avg_kmh" and start < 10:
            continue  # match TeslaFi-style focus on real road speeds
        bucket = buckets.setdefault(start, {"effs": [], "km": 0.0})
        bucket["effs"].append(trip["efficiency_pct"])
        bucket["km"] += trip["km"]

    if not buckets:
        return [], [], []

    labels = []
    effs = []
    kms = []
    for start in sorted(buckets.keys()):
        bucket = buckets[start]
        if not bucket["effs"]:
            continue
        labels.append(label_fmt(start, start + bin_width))
        effs.append(sum(bucket["effs"]) / len(bucket["effs"]))
        kms.append(bucket["km"])
    return labels, effs, kms


def _downsample_rows_inplace(rows, max_rows):
    """Keep chronological order; thin evenly to at most max_rows."""
    count = len(rows)
    if count <= max_rows:
        return rows
    step = max(1, (count + max_rows - 1) // max_rows)
    return rows[::step][:max_rows]


def _load_efficiency_trips(hashed_vin, desiredperiod=None):
    """
    Shared trip list for 1D/2D efficiency charts.

    Drive-only samples, thinned to EFFICIENCY_MAX_DRIVE_ROWS with the same
    progressive even subsample as before — but via raw SQL tuples (not ORM
    .values().iterator()), which dominates cold efficiency_by_* PNG time.
    """
    where = [
        "hashedVin = %s",
        "(shift_state IN ('D', 'R', 'N') OR speed > %s)",
    ]
    params: list = [hashed_vin, DAY_MAP_STOP_SPEED]
    mindate = _lifetime_map_period_mindate(desiredperiod)
    if mindate is not None:
        where.append("DateOnlyDay >= %s")
        params.append(mindate.date() if hasattr(mindate, "date") else mindate)

    where_sql = " AND ".join(where)
    table = TeslaCarDataSnapshot._meta.db_table
    # speed unused for trip KPIs; keep columns minimal for the scan
    cols = "Date, odometer, battery_range, ideal_battery_range, outside_temp"

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {cols} FROM {table} WHERE {where_sql} ORDER BY Date",
            params,
        )
        raw_rows = cursor.fetchall()

    cap = EFFICIENCY_MAX_DRIVE_ROWS
    # Progressive even thin on tuples (same semantics as the old dict path)
    kept: list = []
    for sample_time, odometer, battery_range, ideal_battery_range, outside_temp in raw_rows:
        if sample_time is None:
            continue
        kept.append(
            (sample_time, odometer, battery_range, ideal_battery_range, outside_temp)
        )
        if len(kept) >= cap * 2:
            kept = kept[::2]

    if len(kept) > cap:
        step = max(1, (len(kept) + cap - 1) // cap)
        kept = kept[::step][:cap]

    rows = [
        {
            "t": sample_time,
            "odometer": odometer,
            "battery_range": battery_range,
            "ideal_battery_range": ideal_battery_range,
            "outside_temp": outside_temp,
        }
        for sample_time, odometer, battery_range, ideal_battery_range, outside_temp in kept
    ]
    return _extract_efficiency_trips_from_drive_rows(rows)


def _efficiency_bins_for_car(hashed_vin, desiredperiod, *, by_speed: bool, unit=None):
    """1D histograms: efficiency vs speed or temperature."""
    from matesla.units import is_km, km_to_display, unit_labels

    trips = _load_efficiency_trips(hashed_vin, desiredperiod)
    if by_speed:
        labels, eff, kms = _bin_trips(
            trips,
            key="avg_kmh",
            bin_width=EFFICIENCY_SPEED_BIN_KMH,
            label_fmt=lambda lo, hi: f"{int(lo)}–{int(hi)}",
        )
        # Convert bin labels + distance totals when displaying miles
        if not is_km(unit):
            converted = []
            for label in labels:
                try:
                    lo_s, hi_s = label.split("–")
                    lo = km_to_display(float(lo_s), unit)
                    hi = km_to_display(float(hi_s), unit)
                    converted.append(f"{int(lo)}–{int(hi)}")
                except Exception:
                    converted.append(label)
            labels = converted
            kms = [km_to_display(k, unit) or 0 for k in kms]
        speed_u = unit_labels(unit)["speed"]
        xlabel = _("Average speed (%(u)s)") % {"u": speed_u}
    else:
        labels, eff, kms = _bin_trips(
            trips,
            key="avg_temp_c",
            bin_width=EFFICIENCY_TEMP_BIN_C,
            label_fmt=lambda lo, hi: f"{int(lo)}–{int(hi)}",
        )
        if not is_km(unit):
            kms = [km_to_display(k, unit) or 0 for k in kms]
        xlabel = _("Outside temperature (°C)")
    return labels, eff, kms, xlabel


def _charge_limit_bucket_index(limit_pct: float) -> int:
    """Map a charge limit % into CHARGE_LIMIT_BUCKET_LABELS index."""
    limit_value = float(limit_pct)
    if limit_value >= 99.5:
        return 0  # 100%
    if limit_value >= 95:
        return 1  # 95–99
    if limit_value >= 90:
        return 2  # 90–94
    if limit_value >= 80:
        return 3  # 80–89
    if limit_value >= 70:
        return 4  # 70–79
    return 5  # < 70


def _range_bucket_index(value: float, buckets) -> int:
    """Map value into bucket list of (label, lo, hi) high→low; lo/hi may be None."""
    number = float(value)
    for index, (_label, lower, upper) in enumerate(buckets):
        if lower is not None and number < lower:
            continue
        if upper is not None and number >= upper:
            continue
        return index
    return len(buckets) - 1


def _iter_drive_sensor(queryset, *extra_fields, extra_q=None):
    """
    Stream drive-motion samples in time order (constant memory via iterator).

    When the car has denser history than DRIVE_SENSOR_MAX_SCAN, keep an even
    stride so multi-year speed/power histograms stay interactive without
    loading every row into a list.
    """
    field_names = {"Date", *extra_fields}
    drive_queryset = queryset.filter(_drive_filter_q())
    if extra_q is not None:
        drive_queryset = drive_queryset.filter(extra_q)
    drive_queryset = drive_queryset.order_by("Date").values(*field_names)
    try:
        total_matching_rows = drive_queryset.count()
    except Exception:
        total_matching_rows = 0
    sample_stride = (
        max(
            1,
            (total_matching_rows + DRIVE_SENSOR_MAX_SCAN - 1) // DRIVE_SENSOR_MAX_SCAN,
        )
        if total_matching_rows
        else 1
    )
    row_index = 0
    samples_yielded = 0
    for sample_row in drive_queryset.iterator(chunk_size=4000):
        if sample_row.get("Date") is None:
            continue
        row_index += 1
        if sample_stride > 1 and (row_index % sample_stride) != 0:
            continue
        yield sample_row
        samples_yielded += 1
        if samples_yielded >= DRIVE_SENSOR_MAX_SCAN:
            break


# mph buckets roughly aligned with km/h chart (≈ same road-speed bands)
DRIVE_SPEED_BUCKETS_MPH = (
    ("0–12", 0, 12),
    ("12–25", 12, 25),
    ("25–37", 25, 37),
    ("37–56", 37, 56),
    ("56–68", 56, 68),
    ("68–81", 68, 81),
    ("≥ 81", 81, None),
)


def _drive_speed_histogram(queryset, unit=None):
    """
    Histogram of driving speed in the active unit (km/h or mph).

    Tesla API stores speed in mi/h.
    """
    from matesla.units import is_km, MILES_TO_KM

    buckets = DRIVE_SPEED_BUCKETS_KMH if is_km(unit) else DRIVE_SPEED_BUCKETS_MPH
    bucket_counts = [0] * len(buckets)
    for sample_row in _iter_drive_sensor(
        queryset,
        "speed",
        extra_q=Q(speed__isnull=False) & Q(speed__gt=DAY_MAP_STOP_SPEED),
    ):
        speed_raw = sample_row.get("speed")
        if speed_raw is None:
            continue
        try:
            speed_mph = float(speed_raw)
        except (TypeError, ValueError):
            continue
        if speed_mph < 0 or speed_mph > 200:
            continue
        if speed_mph <= DAY_MAP_STOP_SPEED:
            continue
        speed_display = speed_mph * MILES_TO_KM if is_km(unit) else speed_mph
        bucket_counts[_range_bucket_index(speed_display, buckets)] += 1

    sample_total = sum(bucket_counts)
    percentages = [
        (100.0 * count / sample_total) if sample_total else 0.0
        for count in bucket_counts
    ]
    labels = [bucket[0] for bucket in buckets]
    return labels, bucket_counts, percentages


def _drive_power_histogram(queryset):
    """
    Histogram of drive_state.power (kW). Negative values = regenerative braking.

    Also estimates traction vs regen energy by integrating power × Δt between
    consecutive samples (when the time gap is small enough). That recovers the
    “% recovered” footer users care about after multi-year logging.

    Returns (labels, counts, percentages, metadata) where metadata holds
    traction_kwh, regen_kwh, regen_pct, and which estimation mode was used.
    """
    bucket_counts = [0] * len(DRIVE_POWER_BUCKETS_KW)
    # Power × seconds accumulates as kW·s (numerically same as kJ)
    traction_kilowatt_seconds = 0.0
    regen_kilowatt_seconds = 0.0
    # Fallback when gaps are too large for time integration: sum of |power|
    traction_power_magnitude_sum = 0.0
    regen_power_magnitude_sum = 0.0
    previous_sample_time = None
    previous_power_kw = None
    sample_count = 0

    for sample_row in _iter_drive_sensor(
        queryset, "power", extra_q=Q(power__isnull=False)
    ):
        sample_time = sample_row.get("Date")
        power_raw = sample_row.get("power")
        if sample_time is None or power_raw is None:
            continue
        try:
            power_kw = float(power_raw)
        except (TypeError, ValueError):
            continue
        if power_kw < -400 or power_kw > 400:
            continue

        bucket_counts[_range_bucket_index(power_kw, DRIVE_POWER_BUCKETS_KW)] += 1
        sample_count += 1
        if power_kw > 0.5:
            traction_power_magnitude_sum += power_kw
        elif power_kw < -0.5:
            regen_power_magnitude_sum += -power_kw

        # Trapezoid average power over the interval between two samples
        if previous_sample_time is not None and previous_power_kw is not None:
            time_delta = sample_time - previous_sample_time
            if timedelta(0) < time_delta <= DRIVE_POWER_ENERGY_MAX_GAP:
                average_power_kw = 0.5 * (previous_power_kw + power_kw)
                interval_seconds = time_delta.total_seconds()
                if average_power_kw > 0.5:
                    traction_kilowatt_seconds += average_power_kw * interval_seconds
                elif average_power_kw < -0.5:
                    regen_kilowatt_seconds += (-average_power_kw) * interval_seconds
        previous_sample_time = sample_time
        previous_power_kw = power_kw

    sample_total = sum(bucket_counts)
    percentages = [
        (100.0 * count / sample_total) if sample_total else 0.0
        for count in bucket_counts
    ]
    labels = [bucket[0] for bucket in DRIVE_POWER_BUCKETS_KW]
    traction_kwh = traction_kilowatt_seconds / 3600.0
    regen_kwh = regen_kilowatt_seconds / 3600.0

    # Prefer time-integrated energy when we accumulated a meaningful amount;
    # otherwise fall back to Σ|P| ratio (works when sampling is irregular).
    if traction_kwh >= 1.0:
        regen_percent = 100.0 * regen_kwh / traction_kwh
        estimation_mode = "energy"
    elif traction_power_magnitude_sum > 1.0:
        regen_percent = (
            100.0 * regen_power_magnitude_sum / traction_power_magnitude_sum
        )
        estimation_mode = "sample"
        traction_kwh = None
        regen_kwh = None
    else:
        regen_percent = None
        estimation_mode = None

    metadata = {
        "n_samples": sample_count,
        "traction_kwh": traction_kwh,
        "regen_kwh": regen_kwh,
        "regen_pct": regen_percent,
        "mode": estimation_mode,
    }
    return labels, bucket_counts, percentages, metadata


def _session_charge_limit(points) -> float | None:
    """
    Representative charge limit for a charge session.
    Prefer charge_limit_soc (median of samples); else peak SoC reached.
    """
    limits = []
    peaks = []
    for point in points:
        limit = point.get("charge_limit_soc")
        if limit is not None:
            try:
                limits.append(float(limit))
            except (TypeError, ValueError):
                pass
        soc = point.get("usable_battery_level")
        if soc is None:
            soc = point.get("battery_level")
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


def _session_float_max(points, key) -> float | None:
    best = None
    for point in points:
        value = point.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number < 0:
            continue
        if best is None or number > best:
            best = number
    return best


def _session_delta_field(points, key) -> float | None:
    """Positive delta of a cumulative field over the session (max - min of samples)."""
    vals = []
    for point in points:
        value = point.get(key)
        if value is None:
            continue
        try:
            vals.append(float(value))
        except (TypeError, ValueError):
            continue
    if len(vals) < 2:
        return None
    delta = max(vals) - min(vals)
    return delta if delta > 0.01 else None


def _iter_charge_sessions(queryset, extra_fields=()):
    """
    Yield lists of sample dicts for each charge session (≥ min duration).
    SQL: charging samples only; split on CHARGE_SESSION_GAP.

    Even-stride when denser than CHARGE_SESSION_MAX_SCAN so 10y histories
    stay under ~1s for session histograms.
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
    base = (
        queryset.filter(_charge_filter_q()).order_by("Date").values(*fields)
    )
    try:
        total = base.count()
    except Exception:
        total = 0
    stride = (
        max(1, (total + CHARGE_SESSION_MAX_SCAN - 1) // CHARGE_SESSION_MAX_SCAN)
        if total
        else 1
    )

    current_session = []
    sample_index = 0
    kept = 0
    for row in base.iterator(chunk_size=4000):
        sample_time = row.get("Date")
        if sample_time is None:
            continue
        sample_index += 1
        if stride > 1 and (sample_index % stride) != 0:
            continue
        kept += 1
        point = dict(row)
        point["t"] = sample_time
        if current_session and (
            sample_time - current_session[-1]["t"]
        ) > CHARGE_SESSION_GAP:
            if _charge_session_ok(current_session):
                yield current_session
            current_session = [point]
        else:
            current_session.append(point)
        if kept >= CHARGE_SESSION_MAX_SCAN and not current_session:
            break
    if current_session and _charge_session_ok(current_session):
        yield current_session


def _charge_session_ok(session_points) -> bool:
    """True if the session is long enough to ignore plug glitches."""
    if len(session_points) < 2:
        return False
    duration_minutes = (
        max(0.0, (session_points[-1]["t"] - session_points[0]["t"]).total_seconds())
        / 60.0
    )
    return duration_minutes >= CHARGE_SESSION_MIN_MINUTES


def _charge_filter_q():
    """SQL filter: rows that look like an active charge (state or power)."""
    # Exact match (not iexact/LIKE) so SQLite can use partial charge indexes.
    return Q(charging_state__in=["Charging", "Starting"]) | Q(charger_power__gt=0.5)


def _charge_sessions_sql_by_number(hashed_vin, desiredperiod=None):
    """
    Fast path for TeslaFi history: each session has a stable charge_number.

    One SQL GROUP BY replaces walking every charging sample in Python. Uses the
    partial index matesla_snap_charge_sess_idx when present.

    Returns a list of session summary dicts, or None when charge_number is
    missing (typical pure Fleet capture) so callers fall back to streaming.
    """
    table = TeslaCarDataSnapshot._meta.db_table
    where = [
        "hashedVin = %s",
        "charge_number IS NOT NULL",
        "charging_state IN ('Charging', 'Starting')",
    ]
    params: list = [hashed_vin]
    mindate = _lifetime_map_period_mindate(desiredperiod)
    if mindate is not None:
        # Date (not DateOnlyDay) pairs with partial charge index on hashedVin.
        where.append('"Date" >= %s')
        params.append(mindate)

    where_sql = " AND ".join(where)

    with connection.cursor() as cursor:
        # Probe: any TeslaFi session id in this window?
        cursor.execute(
            f"SELECT 1 FROM {table} WHERE {where_sql} LIMIT 1",
            params,
        )
        if cursor.fetchone() is None:
            return None

        cursor.execute(
            f"""
            SELECT charge_number,
                   MAX(charger_power),
                   MAX(charge_rate),
                   MAX(charge_limit_soc),
                   MIN(charge_energy_added),
                   MAX(charge_energy_added),
                   MIN(charge_miles_added_rated),
                   MAX(charge_miles_added_rated),
                   MIN(battery_range),
                   MAX(battery_range),
                   MAX(usable_battery_level),
                   MAX(battery_level),
                   MIN("Date"),
                   MAX("Date"),
                   COUNT(*)
            FROM {table}
            WHERE {where_sql}
            GROUP BY charge_number
            """,
            params,
        )
        raw_sessions = cursor.fetchall()

    def _as_dt(value):
        """SQLite may return ISO text for Date columns on raw queries."""
        if value is None or hasattr(value, "total_seconds"):
            return value
        if isinstance(value, str):
            # "YYYY-MM-DD HH:MM:SS[.ffffff]" or with T
            normalized = value.replace("T", " ", 1)
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                return None
        return value

    session_summaries = []
    for row in raw_sessions:
        (
            _charge_number,
            peak_power,
            peak_rate,
            charge_limit,
            energy_min,
            energy_max,
            miles_min,
            miles_max,
            range_min,
            range_max,
            usable_max,
            battery_max,
            session_start_time,
            session_end_time,
            sample_count,
        ) = row
        session_start_time = _as_dt(session_start_time)
        session_end_time = _as_dt(session_end_time)
        if session_start_time is None or session_end_time is None:
            continue

        duration_minutes = (
            max(0.0, (session_end_time - session_start_time).total_seconds())
            / 60.0
        )
        sample_count = sample_count or 0
        # Drop very short plug glitches (same rule as the streaming path)
        if duration_minutes < CHARGE_SESSION_MIN_MINUTES and sample_count < 3:
            continue

        energy_added_kwh = None
        if (
            energy_min is not None
            and energy_max is not None
            and energy_max > energy_min
        ):
            energy_added_kwh = float(energy_max) - float(energy_min)

        miles_added_rated = None
        if miles_min is not None and miles_max is not None and miles_max > miles_min:
            miles_added_rated = float(miles_max) - float(miles_min)
        if miles_added_rated is None:
            if (
                range_min is not None
                and range_max is not None
                and range_max > range_min
            ):
                miles_added_rated = float(range_max) - float(range_min)

        peak_soc = usable_max if usable_max is not None else battery_max

        session_summaries.append(
            {
                "peak_charger_power_kw": peak_power,
                "peak_charge_rate_mi_per_h": peak_rate,
                "charge_limit_soc": charge_limit,
                "energy_added_kwh": energy_added_kwh,
                "miles_added_rated": miles_added_rated,
                "peak_soc_percent": (
                    float(peak_soc) if peak_soc is not None else None
                ),
            }
        )
    return session_summaries


def _charge_limit_session_histogram(hashed_vin, desiredperiod=None):
    """
    How often charge sessions used each SoC limit band (100%, 80–89%, …).

    Prefer the SQL charge_number path; fall back to streaming session splits
    when TeslaFi session ids are absent.
    """
    bucket_counts = [0] * len(CHARGE_LIMIT_BUCKET_LABELS)
    session_summaries = _charge_sessions_sql_by_number(hashed_vin, desiredperiod)
    if session_summaries is not None:
        for session in session_summaries:
            charge_limit = session.get("charge_limit_soc")
            if charge_limit is None:
                continue
            try:
                bucket_index = _charge_limit_bucket_index(float(charge_limit))
            except (TypeError, ValueError):
                continue
            bucket_counts[bucket_index] += 1
    else:
        queryset = _period_filter(
            TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin),
            desiredperiod,
        )
        for session_points in _iter_charge_sessions(queryset):
            charge_limit = _session_charge_limit(session_points)
            if charge_limit is None:
                continue
            bucket_counts[_charge_limit_bucket_index(charge_limit)] += 1

    session_total = sum(bucket_counts)
    bucket_percentages = [
        (100.0 * count / session_total) if session_total else 0.0
        for count in bucket_counts
    ]
    return list(CHARGE_LIMIT_BUCKET_LABELS), bucket_counts, bucket_percentages


def _charge_peak_histogram(hashed_vin, desiredperiod=None, *, metric: str):
    """
    Bin charge sessions by peak charger power (kW) or peak charge rate (mi/h).

    Returns (labels, session_counts, amount_per_bucket, percentages, amount_unit).
    amount_unit is "kwh" (energy added) for power bins, or "mi" (rated miles) for rate.
    """
    if metric == "charger_power":
        buckets = CHARGER_POWER_BUCKETS
        peak_field_key = "charger_power"
        amount_unit = "kwh"
    else:
        buckets = CHARGE_RATE_BUCKETS
        peak_field_key = "charge_rate"
        amount_unit = "mi"

    bucket_count = len(buckets)
    session_counts = [0] * bucket_count
    amount_per_bucket = [0.0] * bucket_count

    session_summaries = _charge_sessions_sql_by_number(hashed_vin, desiredperiod)
    if session_summaries is not None:
        for session in session_summaries:
            if peak_field_key == "charger_power":
                peak_value = session.get("peak_charger_power_kw")
            else:
                peak_value = session.get("peak_charge_rate_mi_per_h")

            # Fleet/TeslaFi often omit charger_power on AC; infer AC from low rate
            if (
                (peak_value is None or peak_value <= 0)
                and peak_field_key == "charger_power"
            ):
                charge_rate = session.get("peak_charge_rate_mi_per_h")
                if charge_rate is not None and 0 < float(charge_rate) <= 49:
                    peak_value = 7.0  # typical home AC, for bucketing only

            if peak_value is None or peak_value <= 0:
                continue
            try:
                peak_float = float(peak_value)
            except (TypeError, ValueError):
                continue

            bucket_index = _range_bucket_index(peak_float, buckets)
            session_counts[bucket_index] += 1

            if amount_unit == "kwh":
                amount = session.get("energy_added_kwh")
                if amount is None and session.get("miles_added_rated"):
                    # ~0.22 kWh/mi fleet average when energy counter is missing
                    amount = float(session["miles_added_rated"]) * 0.22
            else:
                amount = session.get("miles_added_rated")
            if amount is not None and amount > 0:
                amount_per_bucket[bucket_index] += float(amount)
    else:
        # Streaming fallback (no TeslaFi charge_number)
        queryset = _period_filter(
            TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin),
            desiredperiod,
        )
        for session_points in _iter_charge_sessions(queryset):
            peak_value = _session_float_max(session_points, peak_field_key)
            if (
                (peak_value is None or peak_value <= 0)
                and peak_field_key == "charger_power"
            ):
                charge_rate = _session_float_max(session_points, "charge_rate")
                if charge_rate is not None and 0 < charge_rate <= 49:
                    peak_value = 7.0
            if peak_value is None or peak_value <= 0:
                continue
            bucket_index = _range_bucket_index(peak_value, buckets)
            session_counts[bucket_index] += 1
            if amount_unit == "kwh":
                amount = _session_delta_field(session_points, "charge_energy_added")
                if amount is None:
                    miles = _session_delta_field(
                        session_points, "charge_miles_added_rated"
                    )
                    if miles is None:
                        miles = _session_delta_field(session_points, "battery_range")
                    if miles is not None and miles > 0:
                        amount = miles * 0.22
            else:
                amount = _session_delta_field(
                    session_points, "charge_miles_added_rated"
                )
                if amount is None:
                    amount = _session_delta_field(session_points, "battery_range")
            if amount is not None and amount > 0:
                amount_per_bucket[bucket_index] += amount

    session_total = sum(session_counts)
    percentages = [
        (100.0 * count / session_total) if session_total else 0.0
        for count in session_counts
    ]
    labels = [bucket[0] for bucket in buckets]
    return labels, session_counts, amount_per_bucket, percentages, amount_unit


def _end_soc_bucket_index(soc: float) -> int:
    """Peak SoC during charge → CHARGE_END_SOC_BUCKET_LABELS."""
    peak_soc = float(soc)
    if peak_soc >= 99.5:
        return 0
    if peak_soc >= 95:
        return 1
    if peak_soc >= 90:
        return 2
    if peak_soc >= 80:
        return 3
    if peak_soc >= 70:
        return 4
    if peak_soc >= 50:
        return 5
    return 6


def _daily_min_soc_bucket_index(soc: float) -> int:
    """Daily min SoC → DAILY_MIN_SOC_BUCKET_LABELS."""
    soc_value = float(soc)
    if soc_value < 5:
        return 0
    if soc_value < 10:
        return 1
    if soc_value < 20:
        return 2
    if soc_value < 30:
        return 3
    if soc_value < 40:
        return 4
    if soc_value < 50:
        return 5
    return 6


def _sample_soc(sample) -> float | None:
    usable = sample.get("usable_battery_level")
    if usable is not None:
        try:
            return float(usable)
        except (TypeError, ValueError):
            pass
    battery = sample.get("battery_level")
    if battery is not None:
        try:
            return float(battery)
        except (TypeError, ValueError):
            pass
    return None


def _charge_end_soc_histogram(hashed_vin, desiredperiod=None):
    """
    Sessions classified by peak SoC reached while charging (end-of-charge habit).

    Uses charge_number SQL summaries when available; otherwise streams samples.
    """
    bucket_counts = [0] * len(CHARGE_END_SOC_BUCKET_LABELS)
    session_summaries = _charge_sessions_sql_by_number(hashed_vin, desiredperiod)
    if session_summaries is not None:
        for session in session_summaries:
            peak_soc = session.get("peak_soc_percent")
            if peak_soc is None:
                continue
            bucket_counts[_end_soc_bucket_index(peak_soc)] += 1
    else:
        queryset = _period_filter(
            TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin),
            desiredperiod,
        )
        for session_points in _iter_charge_sessions(queryset):
            peak_soc = None
            for sample in session_points:
                state_of_charge = _sample_soc(sample)
                if state_of_charge is None:
                    continue
                if peak_soc is None or state_of_charge > peak_soc:
                    peak_soc = state_of_charge
            if peak_soc is None:
                continue
            bucket_counts[_end_soc_bucket_index(peak_soc)] += 1
    session_total = sum(bucket_counts)
    percentages = [
        (100.0 * count / session_total) if session_total else 0.0
        for count in bucket_counts
    ]
    return list(CHARGE_END_SOC_BUCKET_LABELS), bucket_counts, percentages


def _daily_min_soc_histogram(queryset):
    """Calendar days classified by minimum SoC that day."""
    day_mins = (
        queryset.exclude(battery_level__isnull=True, usable_battery_level__isnull=True)
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
        min_usable, min_battery = row.get("min_u"), row.get("min_b")
        # Prefer usable min when available, else battery_level min
        day_min = None
        if min_usable is not None:
            try:
                day_min = float(min_usable)
            except (TypeError, ValueError):
                day_min = None
        if day_min is None and min_battery is not None:
            try:
                day_min = float(min_battery)
            except (TypeError, ValueError):
                day_min = None
        if day_min is None:
            continue
        counts[_daily_min_soc_bucket_index(day_min)] += 1
    total = sum(counts)
    pcts = [(100.0 * count / total) if total else 0.0 for count in counts]
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
    figure, style_config = make_figure(size, bar=True)
    axes = figure.subplots()
    if labels and counts and sum(counts) > 0:
        x = list(range(len(labels)))
        if bar_colors and len(bar_colors) >= len(labels):
            colors = list(bar_colors[: len(labels)])
        else:
            colors = ACCENT_SOFT
        bars = axes.bar(
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
        axes.set_ylim(0, ymax * 1.18)
        label_font_size = max(6.0, style_config["tick_size"] - 0.5)
        for index, (bar, count, percentage) in enumerate(zip(bars, counts, pcts)):
            if count <= 0:
                continue
            # Compact: "73 (5%)" or "12.4k (20%)" for large drive-sample counts
            if count >= 10000:
                count_text = f"{count / 1000:.0f}k"
            elif count >= 1000:
                count_text = f"{count / 1000:.1f}k"
            else:
                count_text = str(count)
            label = f"{count_text} ({percentage:.0f}%)"
            amount = None
            if amount_per_bucket is not None and index < len(amount_per_bucket):
                amount = amount_per_bucket[index]
            if amount is not None and amount >= 0.5:
                if amount_unit == "kwh":
                    label = f"{count_text} ({percentage:.0f}%)\n{amount:.0f} kWh"
                else:
                    label = f"{count_text} ({percentage:.0f}%)\n{amount:.0f} mi"
            axes.annotate(
                label,
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=label_font_size,
                fontweight="normal",
                zorder=3,
                clip_on=False,
            )
        axes.set_xticks(x)
        axes.set_xticklabels(labels, rotation=22, ha="right")
        axes.set_ylabel(count_ylabel or _("Charge sessions"), color=MUTED)
        axes.set_xlabel(xlabel, color=MUTED)
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
        style_axes(axes, style_config)
        style_suptitle(figure, title, style_config)
        try:
            figure.tight_layout(rect=(0.02, 0.08, 0.98, 0.90))
        except Exception:
            pass
        figure.text(
            0.5,
            0.01,
            foot,
            ha="center",
            va="bottom",
            color=MUTED,
            fontsize=style_config["tick_size"] - 0.5,
        )
    else:
        finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)


def GenerateEfficiencyBinGraph(
    labels, efficiency, km_totals, title, xlabel, size="full", unit=None
):
    """
    Dual-axis chart: bars = distance recorded in bin, line = mean efficiency %.
    Dark MaTesla style (not a TeslaFi clone).
    """
    from matesla.units import unit_labels

    dist_u = unit_labels(unit)["distance"]
    figure, style_config = make_figure(size)
    axes = figure.subplots()
    if labels and efficiency and len(labels) > 0:
        x = list(range(len(labels)))
        axes_secondary = axes.twinx()
        # Bars behind the line
        bar_w = 0.72
        axes_secondary.bar(
            x,
            km_totals,
            width=bar_w,
            color=ACCENT_SOFT,
            alpha=0.35,
            edgecolor=ACCENT,
            linewidth=0.4,
            zorder=1,
            label=_("Distance recorded (%(u)s)") % {"u": dist_u},
        )
        axes.plot(
            x,
            efficiency,
            color=ENERGY,
            linestyle="-",
            linewidth=style_config["linewidth"] + 0.35,
            marker="o",
            markersize=style_config["markersize"] + 1.2,
            markerfacecolor=ENERGY,
            markeredgecolor="#0b1220",
            markeredgewidth=0.6,
            zorder=3,
            label=_("Efficiency"),
        )
        # Annotate a few efficiency values when not too crowded
        if len(labels) <= 18:
            for index, efficiency_pct in enumerate(efficiency):
                axes.annotate(
                    f"{efficiency_pct:.0f}%",
                    (index, efficiency_pct),
                    textcoords="offset points",
                    xytext=(0, 7),
                    ha="center",
                    fontsize=style_config["tick_size"] - 0.5,
                    color=TEXT,
                    zorder=4,
                )
        axes.set_xticks(x)
        axes.set_xticklabels(labels, rotation=35, ha="right")
        axes.set_ylabel(_("Efficiency (%)"), color=MUTED)
        axes_secondary.set_ylabel(_("Distance (%(u)s)") % {"u": dist_u}, color=MUTED)
        axes.set_xlabel(xlabel, color=MUTED)
        axes.set_ylim(bottom=max(0, min(efficiency) - 12), top=min(145, max(efficiency) + 12))
        axes_secondary.set_ylim(bottom=0, top=max(km_totals) * 1.25 if km_totals else 1)
        style_axes(axes, style_config)
        axes_secondary.set_facecolor("none")
        axes_secondary.tick_params(colors=MUTED, labelsize=style_config["tick_size"], length=3.5, width=0.7)
        axes_secondary.yaxis.label.set_color(MUTED)
        for spine in axes_secondary.spines.values():
            spine.set_color("#3a5070")
            spine.set_linewidth(style_config["spine_width"])
        # Combined legend
        h1, l1 = axes.get_legend_handles_labels()
        h2, l2 = axes_secondary.get_legend_handles_labels()
        leg = axes.legend(
            h1 + h2,
            l1 + l2,
            facecolor="#162338",
            edgecolor="#3a5070",
            labelcolor=TEXT,
            fontsize=style_config["legend_size"],
            framealpha=0.92,
            loc="best",
        )
        if leg is not None:
            leg.get_frame().set_linewidth(0.8)
        # Subtitle hint
        axes.text(
            0.01,
            0.02,
            _("Trips ≥ 10 km · 100% = matched rated range use"),
            transform=axes.transAxes,
            fontsize=style_config["tick_size"] - 0.5,
            color=MUTED,
            va="bottom",
            zorder=5,
        )
        style_suptitle(figure, title, style_config)
        try:
            figure.tight_layout(rect=(0.02, 0.02, 0.98, 0.92))
        except Exception:
            pass
    else:
        finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)


def _unknown_hashed_vin_response(request, hashedVin, *, kind="html"):
    """
    None if hashedVin is a safe token for a known vehicle.

    Otherwise 404, not 500: the URL does not name a car, the server is fine.
    kind: html (themed page, no vehicle chrome), json, raw (PNG/CSV).
    """
    if IsValidHash(hashedVin) and IsKnownHashedVin(hashedVin):
        return None
    if kind == "json":
        return JsonResponse({"ok": False, "error": "invalid_hash"}, status=404)
    if kind == "raw":
        return HttpResponseNotFound(_("Unknown vehicle."))
    return render(request, "personalstats/unknown_vehicle.html", status=404)


# Check params and ensure that they are not a potential SQL injection
# return response + False if problem, None + True if fine
def SecurityChecks(hashedVin, desiredfield):
    if not IsValidHash(hashedVin) or not IsKnownHashedVin(hashedVin):
        # malformed token or no such vehicle — 404, not an empty graph
        return HttpResponseNotFound(_("Unknown vehicle.")), False
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


def _period_filter(queryset, desiredperiod):
    """desiredperiod is expressed in weeks; 0 / None means all data."""
    if desiredperiod is not None and desiredperiod > 0:
        # most recent data
        mindate = datetime.now() - timedelta(weeks=desiredperiod)
        return queryset.filter(DateOnlyDay__gte=mindate)
    return queryset


# create a graph showing the evolution of field for a car identified by hashed
# vin.
# desiredperiod is expressed in weeks, 0 means all.
# allow to disable cache when improving graphs and you want a constant reload
# @never_cache
def _distance_unit_from_request(request):
    """Prefer explicit ?unit= on graph URLs (cache-bust), else cookie preference."""
    from matesla.units import get_distance_unit, normalize_unit

    raw = request.GET.get("unit")
    if raw:
        return normalize_unit(raw)
    return get_distance_unit(request)


def StatsOnCarGraph(request, hashedVin, desiredfield, desiredperiod):
    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response
    unit = _distance_unit_from_request(request)
    size = graph_size_from_request(request)
    cache_key = _graph_png_cache_key(
        hashedVin, desiredfield, desiredperiod, size, unit=unit
    )
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None:
        return _png_response_from_bytes(hit, size, cache_status="HIT")

    response = _stats_on_car_graph_uncached(
        request, hashedVin, desiredfield, desiredperiod, size, unit=unit
    )
    return _cache_graph_png(cache_key, response, size)


def _stats_on_car_graph_uncached(
    request, hashedVin, desiredfield, desiredperiod, size, unit=None
):
    """Actual PNG generation (no cache). Called by StatsOnCarGraph."""
    from matesla.units import get_distance_unit, is_km, unit_labels

    unit = unit or get_distance_unit(request)
    labels = unit_labels(unit)
    title = GetTitleForField(desiredfield, unit=unit)
    # Fleet cost uses request log — works even with zero snapshots for this VIN
    if desiredfield == "fleet_poll_cost":
        days = _fleet_poll_window_days(desiredperiod)
        labels, counts = _fleet_poll_buckets(hashedVin, days=days)
        cur_code, cur_symbol, price = _fleet_cost_currency()
        return GenerateFleetPollCostGraph(
            labels,
            counts,
            title,
            size=size,
            currency_code=cur_code,
            currency_symbol=cur_symbol,
            price_per_request=price,
        )

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
        if desiredfield in ("outside_temp", "inside_temp"):
            return GenerateMonthlyTempRibbonGraph(
                [], [], [], [], title, size=size
            )
        return GenerateDateGraph(None, None, None, None, title, size=size)

    # Trip efficiency histograms (not a raw time series field)
    if desiredfield in ("efficiency_by_speed", "efficiency_by_temp"):
        eff_labels, eff, kms, xlabel = _efficiency_bins_for_car(
            hashedVin,
            desiredperiod,
            by_speed=(desiredfield == "efficiency_by_speed"),
            unit=unit,
        )
        return GenerateEfficiencyBinGraph(
            eff_labels, eff, kms, title, xlabel, size=size, unit=unit
        )

    # Drive speed distribution (replaces noisy min/avg/max time series)
    if desiredfield == "speed":
        queryset = _period_filter(base, desiredperiod)
        speed_labels, counts, pcts = _drive_speed_histogram(queryset, unit=unit)
        foot = (
            _("drive samples only · Tesla speed converted mph→km/h")
            if is_km(unit)
            else _("drive samples only · Tesla speed in mph")
        )
        return GenerateChargeSessionHistogram(
            speed_labels,
            counts,
            pcts,
            title,
            size=size,
            xlabel=_("Speed (%(u)s)") % {"u": labels["speed"]},
            amount_per_bucket=None,
            amount_unit=None,
            count_ylabel=_("Samples"),
            foot_extra=foot,
        )

    # Drive power distribution + regen vs traction energy estimate
    if desiredfield == "power":
        queryset = _period_filter(base, desiredperiod)
        labels, counts, pcts, meta = _drive_power_histogram(queryset)
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
        labels, counts, pcts = _charge_limit_session_histogram(
            hashedVin, desiredperiod
        )
        return GenerateChargeLimitHistogram(labels, counts, pcts, title, size=size)

    # Peak power / charge-rate session histograms (DC vs AC, Supercharge peaks)
    if desiredfield in ("charger_power", "charge_rate"):
        labels, counts, amounts, pcts, amount_unit = _charge_peak_histogram(
            hashedVin, desiredperiod, metric=desiredfield
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
        labels, counts, pcts = _charge_end_soc_histogram(hashedVin, desiredperiod)
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
        queryset = _period_filter(base, desiredperiod)
        labels, counts, pcts = _daily_min_soc_histogram(queryset)
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

    # Temperature: monthly min–max ribbon (seasonal + extremes), not noisy daily lines
    if desiredfield in ("outside_temp", "inside_temp"):
        months, mins, maxs, avgs = _monthly_temp_series(
            hashedVin, desiredfield, desiredperiod
        )
        return GenerateMonthlyTempRibbonGraph(
            months, mins, maxs, avgs, title, size=size
        )

    # range_at_100 is not a DB column: battery_range / SoC * 100 (full-charge miles)
    if desiredfield == "range_at_100":
        queryset = annotate_range_at_100(_period_filter(base, desiredperiod))
        results = (
            queryset.values("DateOnlyDay")
            .annotate(
                max_val=Max("range_at_100"),
                min_val=Min("range_at_100"),
                avg_val=Avg("range_at_100"),
            )
            .order_by("DateOnlyDay")
        )
    else:
        queryset = _period_filter(base, desiredperiod)
        results = (
            queryset.values("DateOnlyDay")
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
    if desiredfield in _MILES_VALUE_FIELDS:
        maxvalues = _scale_miles_series(maxvalues, unit)
        minvalues = _scale_miles_series(minvalues, unit)
        avgvalues = _scale_miles_series(avgvalues, unit)
    footer = None
    if desiredfield == "odometer":
        last_day, last_y = _last_series_point(dates, maxvalues)
        footer = _odometer_graph_footer(last_y, last_day, unit)
    return GenerateDateGraph(
        dates, maxvalues, minvalues, avgvalues, title, size=size, footer=footer
    )

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
        from matesla.TeslaConnect import (
            list_user_vehicles,
            resolve_active_vehicle,
            serialize_vehicle_for_chrome,
        )

        vehicles = list_user_vehicles(user)
        active = resolve_active_vehicle(user, request)
        context["user_vehicles"] = [
            serialize_vehicle_for_chrome(vehicle) for vehicle in vehicles
        ]
        context["active_vehicle_api_id"] = active.api_id if active else None
        context["active_vehicle_label"] = active.label if active else None
    return context


# allow to disable cache when improving HTML and you want a constant reload
# @never_cache
def Stats(request, hashedVin):
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied
    from matesla.units import get_distance_unit

    template = loader.get_template('personalstats/carstats.html')
    context = _vehicle_chrome_context(request, hashedVin)
    # Dropdown titles must follow km/mi preference (default was always km).
    context.update(GetTitleForFieldDico(get_distance_unit(request)))
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
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        try:
            return date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        except ValueError:
            return None
    # 31/12/2024 or 31-12-2024
    match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", raw)
    if match:
        try:
            return date(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            )
        except ValueError:
            return None
    return None


def _downsample_indices(count, max_points):
    if count <= max_points or max_points < 2:
        return list(range(count))
    # always keep first and last
    indices = {0, count - 1}
    step = (count - 1) / (max_points - 1)
    for index in range(1, max_points - 1):
        indices.add(int(round(index * step)))
    return sorted(indices)


def _haversine_m(lat1, lon1, lat2, lon2):
    """
    Great-circle distance in metres between two WGS84 points (as-the-crow-flies).

    GPS lat/lon are angles, not planar metres: 1° of longitude shrinks toward the
    poles. Haversine maps the angular separation of two points on a sphere to the
    shortest surface arc (the great-circle path).

    With φ, λ in radians, Δφ = φ2−φ1, Δλ = λ2−λ1, and R ≈ 6_371_000 m:

        a = sin²(Δφ/2) + cos(φ1)·cos(φ2)·sin²(Δλ/2)
        d = 2·R·arcsin(√a)

    Here `a` is the haversine (half-versed sine) of the central angle; arcsin(√a)
    is half that angle, so 2·R·… is the arc length in metres.

    Notes:
      - Road distance / elevation are not modelled (odometer can be larger).
      - Sphere vs WGS84 ellipsoid error is negligible under a few km.
      - min(1, √a) guards floating-point drift so arcsin stays defined.
    """
    earth_radius_m = 6371000.0
    # Trig functions need radians; keep degrees only at the API boundary.
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    # `a` ∈ [0, 1]: haversine of the central angle between the two points.
    haversine = (
        sin(dphi / 2.0) ** 2
        + cos(phi1) * cos(phi2) * sin(dlambda / 2.0) ** 2
    )
    return 2.0 * earth_radius_m * asin(min(1.0, sqrt(haversine)))


def _format_gap_distance_m(gap_m):
    """Human label for a gap: metres under 1 km, else kilometres."""
    if gap_m is None or gap_m < 0:
        return ""
    if gap_m < 1000.0:
        return _("%(n)s m") % {"n": int(round(gap_m))}
    return _("%(n)s km") % {"n": f"{gap_m / 1000.0:.1f}"}


def _daymap_unmonitored_tail(drives, end_lat, end_lon):
    """
    Detect when the day's final GPS is far from the last recorded drive arrival.

    Sparse capture (cost-driven poll interval) often misses short final trips:
    the last drive ends at the shop, then the car is home with no drive samples
    in between. Returns a context dict for the template, or None.
    """
    if not drives or end_lat is None or end_lon is None:
        return None
    last = drives[-1]
    drive_end_lat = last.get("end_lat")
    drive_end_lon = last.get("end_lon")
    if drive_end_lat is None or drive_end_lon is None:
        return None
    try:
        gap_m = _haversine_m(
            float(drive_end_lat),
            float(drive_end_lon),
            float(end_lat),
            float(end_lon),
        )
    except (TypeError, ValueError):
        return None
    if gap_m < DAYMAP_TAIL_GAP_MIN_M:
        return None
    dist_label = _format_gap_distance_m(gap_m)
    if gap_m <= DAYMAP_TAIL_GAP_SHORT_MAX_M:
        kind = "short"
        message = _(
            "The last trip was not recorded — it was likely too short for the "
            "capture interval (about %(dist)s between the last logged arrival "
            "and the car's final position)."
        ) % {"dist": dist_label}
    else:
        kind = "long"
        message = _(
            "The last trip was not recorded — missing telemetry between the "
            "last logged arrival and the car's final position (about %(dist)s). "
            "This can happen after a capture gap or a technical issue."
        ) % {"dist": dist_label}
    return {
        "kind": kind,
        "gap_m": gap_m,
        "gap_label": dist_label,
        "message": message,
    }


def _thin_segments_to_cap(segments, max_points):
    """
    Proportionally downsample polyline segments so total points ≤ max_points.

    First evenly samples *which* segments to keep (so late trips are not
    dropped when the budget runs out), then thins points inside each.
    Always keeps ≥2 points on retained multi-point segments.
    """
    multi_point = [segment for segment in segments if len(segment) >= 2]
    if not multi_point:
        return [segment for segment in segments if segment]
    total = sum(len(segment) for segment in multi_point)
    if total <= max_points or max_points < 2:
        return multi_point

    # Each kept segment needs ≥2 points → hard cap on segment count
    max_segments = max(1, max_points // 2)
    if len(multi_point) > max_segments:
        keep_indices = _downsample_indices(len(multi_point), max_segments)
        multi_point = [multi_point[index] for index in keep_indices]

    weights = [len(segment) for segment in multi_point]
    weight_sum = float(sum(weights) or 1)
    # Proportional target, then fix so sum(allow) == max_points and each ≥2
    raw_allow = [
        max(2, int(round(max_points * (weight / weight_sum)))) for weight in weights
    ]
    # Clamp to segment length
    allow = [
        min(len(segment), allowed)
        for segment, allowed in zip(multi_point, raw_allow)
    ]
    # Redistribute if over/under budget
    diff = max_points - sum(allow)
    cursor = 0
    count = len(allow)
    # Prefer giving extra points to longer segments
    order = sorted(
        range(count), key=lambda index: weights[index], reverse=(diff > 0)
    )
    guard = 0
    while diff != 0 and count and guard < max_points * 4:
        order_index = order[cursor % count]
        if diff > 0 and allow[order_index] < len(multi_point[order_index]):
            allow[order_index] += 1
            diff -= 1
        elif diff < 0 and allow[order_index] > 2:
            allow[order_index] -= 1
            diff += 1
        cursor += 1
        guard += 1

    thinned = []
    for segment, allowed in zip(multi_point, allow):
        indices = _downsample_indices(
            len(segment), max(2, min(allowed, len(segment)))
        )
        thinned.append([segment[index] for index in indices])
    # Hard safety: never exceed max_points even if redistribution stalled
    total_out = sum(len(segment) for segment in thinned)
    if total_out > max_points and len(thinned) > 1:
        keep_count = max(1, max_points // 2)
        keep_indices = _downsample_indices(
            len(thinned), min(len(thinned), keep_count)
        )
        thinned = [thinned[index] for index in keep_indices]
        # final even thin of all remaining points
        flat_budget = max_points
        per_segment = max(2, flat_budget // max(1, len(thinned)))
        thinned = [
            [
                segment[index]
                for index in _downsample_indices(
                    len(segment), min(len(segment), per_segment)
                )
            ]
            for segment in thinned
        ]
    return thinned


def _lifetime_map_period_mindate(desired_period_weeks):
    """Match _period_filter: weeks → lower bound, or None for all history."""
    if desired_period_weeks is not None and desired_period_weeks > 0:
        return datetime.now() - timedelta(weeks=int(desired_period_weeks))
    return None


def _fetch_lifetime_drive_gps_rows(hashed_vin, desired_period_weeks):
    """
    All drive-GPS rows for the lifetime map as plain tuples (not ORM dicts).

    Avoid Django .values().iterator() over 100k+ rows — raw SQL + tuples is
    ~2–3× faster cold and keeps trip KPIs exact (every sample).
    """
    where = [
        "hashedVin = %s",
        "(shift_state IN ('D', 'R', 'N') OR speed > %s)",
        "latitude IS NOT NULL",
        "longitude IS NOT NULL",
    ]
    params: list = [hashed_vin, DAY_MAP_STOP_SPEED]
    mindate = _lifetime_map_period_mindate(desired_period_weeks)
    if mindate is not None:
        # Same field as _period_filter (DateOnlyDay index on hashedVin).
        where.append("DateOnlyDay >= %s")
        params.append(mindate.date() if hasattr(mindate, "date") else mindate)

    where_sql = " AND ".join(where)
    table = TeslaCarDataSnapshot._meta.db_table
    cols = (
        "Date, latitude, longitude, odometer, battery_range, "
        "ideal_battery_range, outside_temp"
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {cols} FROM {table} WHERE {where_sql} ORDER BY Date",
            params,
        )
        return cursor.fetchall()


def _build_lifetime_map_payload(hashed_vin, desired_period_weeks):
    """
    Build lifetime-map JSON: drive GPS polylines + summary KPIs for a period.

    Performance strategy:
    - Raw SQL tuples (not ORM iterator) for the full drive-GPS stream → KPIs exact.
    - Path uses even stride + distance thinning (LIFETIME_MAP_MAX_SCAN / POINTS).
    """
    sample_rows = _fetch_lifetime_drive_gps_rows(
        hashed_vin, desired_period_weeks
    )
    total_drive_gps_samples = len(sample_rows)
    sample_stride = (
        max(
            1,
            (total_drive_gps_samples + LIFETIME_MAP_MAX_SCAN - 1)
            // LIFETIME_MAP_MAX_SCAN,
        )
        if total_drive_gps_samples
        else 1
    )
    minimum_move_meters = LIFETIME_MAP_MIN_MOVE_M * (
        1.0 + 0.5 * (sample_stride - 1)
    )

    # ---------- Map path ----------
    path_segments = []
    current_path = []
    last_kept_latitude = last_kept_longitude = None
    path_point_count = 0
    progressive_point_cap = LIFETIME_MAP_MAX_POINTS * 2

    # ---------- KPI accumulators ----------
    drive_count = 0
    kilometers_driven = 0.0
    rated_miles_used = 0.0
    driving_hours_total = 0.0
    outside_temp_sum = 0.0
    outside_temp_sample_count = 0

    trip_start_time = trip_start_odometer = trip_start_rated_range = None
    trip_end_time = trip_end_odometer = trip_end_rated_range = None

    last_sample_time = None
    raw_gps_sample_count = 0
    scan_row_index = 0

    def finalize_trip():
        nonlocal drive_count, kilometers_driven, rated_miles_used, driving_hours_total
        nonlocal trip_start_time, trip_start_odometer, trip_start_rated_range
        nonlocal trip_end_time, trip_end_odometer, trip_end_rated_range

        if trip_start_time is None or trip_end_time is None:
            trip_start_time = trip_start_odometer = trip_start_rated_range = None
            trip_end_time = trip_end_odometer = trip_end_rated_range = None
            return

        if (
            trip_start_odometer is not None
            and trip_end_odometer is not None
            and trip_end_odometer >= trip_start_odometer
        ):
            miles_driven = float(trip_end_odometer) - float(trip_start_odometer)
            kilometers = miles_driven * 1.609344
        else:
            kilometers = 0.0

        duration_seconds = max(
            0.0, (trip_end_time - trip_start_time).total_seconds()
        )

        if kilometers >= LIFETIME_MAP_MIN_TRIP_KM and duration_seconds >= 60:
            drive_count += 1
            kilometers_driven += kilometers
            driving_hours_total += duration_seconds / 3600.0

            if (
                trip_start_rated_range is not None
                and trip_end_rated_range is not None
                and trip_start_rated_range > trip_end_rated_range
            ):
                rated_range_drop = float(trip_start_rated_range) - float(
                    trip_end_rated_range
                )
                if rated_range_drop > 0.25:
                    rated_miles_used += rated_range_drop

        trip_start_time = trip_start_odometer = trip_start_rated_range = None
        trip_end_time = trip_end_odometer = trip_end_rated_range = None

    def flush_current_path():
        nonlocal current_path, last_kept_latitude, last_kept_longitude
        if len(current_path) >= 1:
            path_segments.append(current_path)
        current_path = []
        last_kept_latitude = last_kept_longitude = None

    # ---------- Main loop ----------
    for sample_row in sample_rows:
        scan_row_index += 1
        (
            sample_time,
            latitude_raw,
            longitude_raw,
            odometer_raw,
            battery_range_raw,
            ideal_battery_range_raw,
            outside_temp_raw,
        ) = sample_row

        if sample_time is None or latitude_raw is None or longitude_raw is None:
            continue

        try:
            latitude = float(latitude_raw)
            longitude = float(longitude_raw)
        except (TypeError, ValueError):
            continue

        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue
        if abs(latitude) < 1e-5 and abs(longitude) < 1e-5:
            continue

        raw_gps_sample_count += 1

        # ----- Trip KPIs on every sample (odometer / rated range) -----
        if (
            last_sample_time is not None
            and (sample_time - last_sample_time) > LIFETIME_MAP_GAP
        ):
            finalize_trip()
            flush_current_path()

        try:
            odometer_miles = (
                float(odometer_raw) if odometer_raw is not None else None
            )
        except (TypeError, ValueError):
            odometer_miles = None

        rated_range_miles = _rated_range_miles(
            battery_range_raw,
            ideal_battery_range_raw,
        )

        if outside_temp_raw is not None:
            try:
                outside_temp_sum += float(outside_temp_raw)
                outside_temp_sample_count += 1
            except (TypeError, ValueError):
                pass

        if trip_start_time is None:
            trip_start_time = sample_time
            trip_start_odometer = odometer_miles
            trip_start_rated_range = rated_range_miles

        trip_end_time = sample_time
        if odometer_miles is not None:
            trip_end_odometer = odometer_miles
        if rated_range_miles is not None:
            trip_end_rated_range = rated_range_miles

        # ----- Path: even stride + distance thinning -----
        is_stride_sample = (sample_stride == 1) or (
            scan_row_index % sample_stride == 0
        )
        if is_stride_sample:
            keep_path_point = False
            if last_kept_latitude is None:
                keep_path_point = True
            else:
                distance_meters = _haversine_m(
                    last_kept_latitude,
                    last_kept_longitude,
                    latitude,
                    longitude,
                )
                if distance_meters >= minimum_move_meters:
                    keep_path_point = True

            if keep_path_point:
                current_path.append([round(latitude, 5), round(longitude, 5)])
                last_kept_latitude = latitude
                last_kept_longitude = longitude
                path_point_count += 1

                if path_point_count > progressive_point_cap:
                    if current_path:
                        path_segments.append(current_path)
                        current_path = []
                        last_kept_latitude = last_kept_longitude = None
                    path_segments = _thin_segments_to_cap(
                        path_segments, LIFETIME_MAP_MAX_POINTS
                    )
                    path_point_count = sum(len(s) for s in path_segments)
                    progressive_point_cap = LIFETIME_MAP_MAX_POINTS * 2

        last_sample_time = sample_time

    finalize_trip()
    flush_current_path()
    path_segments = _thin_segments_to_cap(path_segments, LIFETIME_MAP_MAX_POINTS)

    multi_point_segments = [s for s in path_segments if len(s) >= 2]
    if multi_point_segments:
        path_segments = multi_point_segments

    # ---------- Final KPIs ----------
    path_points = sum(len(s) for s in path_segments)
    rated_kilometers_used = (
        rated_miles_used * 1.609344 if rated_miles_used > 0 else 0.0
    )

    efficiency_percent = None
    if rated_kilometers_used > 1.0 and kilometers_driven > 1.0:
        efficiency_percent = 100.0 * kilometers_driven / rated_kilometers_used
        if efficiency_percent < 10 or efficiency_percent > 200:
            efficiency_percent = None

    energy_used_kwh = rated_miles_used * 0.22 if rated_miles_used > 0 else None
    watt_hours_per_km = None
    if energy_used_kwh is not None and kilometers_driven > 1.0:
        watt_hours_per_km = (energy_used_kwh * 1000.0) / kilometers_driven

    average_speed_kmh = (
        (kilometers_driven / driving_hours_total)
        if driving_hours_total > 0.05
        else None
    )
    average_outside_temp_c = (
        (outside_temp_sum / outside_temp_sample_count)
        if outside_temp_sample_count
        else None
    )

    total_minutes = int(round(driving_hours_total * 60.0))
    drive_days = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    drive_hours = remaining_minutes // 60
    drive_minutes = remaining_minutes % 60

    return {
        "ok": True,
        "period_weeks": int(desired_period_weeks) if desired_period_weeks else 0,
        "segments": path_segments,
        "path_points": path_points,
        "raw_gps_samples": raw_gps_sample_count,
        "sample_stride": sample_stride,
        "drives": drive_count,
        "km_driven": round(kilometers_driven, 1) if kilometers_driven > 0 else 0.0,
        "rated_km_used": (
            round(rated_kilometers_used, 1) if rated_kilometers_used > 0 else 0.0
        ),
        "wh_per_km": (
            round(watt_hours_per_km) if watt_hours_per_km is not None else None
        ),
        "efficiency_pct": (
            round(efficiency_percent, 2) if efficiency_percent is not None else None
        ),
        "kwh_used": (
            round(energy_used_kwh, 1) if energy_used_kwh is not None else None
        ),
        "avg_kmh": (
            round(average_speed_kmh, 1) if average_speed_kmh is not None else None
        ),
        "avg_temp_c": (
            round(average_outside_temp_c, 1)
            if average_outside_temp_c is not None
            else None
        ),
        "drive_time": {
            "days": drive_days,
            "hours": drive_hours,
            "minutes": drive_minutes,
            "total_hours": round(driving_hours_total, 2),
        },
        "has_track": path_points > 0,
    }

def _point_kind(sample):
    """Classify a sample: charge | drive | park."""
    charging_state = (sample.get("charging_state") or "").strip().lower()
    charger_power = sample.get("charger_power")
    # Starting: power ramp just after plug; treat as charge so it stays in-session.
    if charging_state in ("charging", "starting") or (
        charger_power is not None and charger_power > 0.5
    ):
        return "charge"
    shift_state = (sample.get("shift_state") or "").strip().upper()
    speed = sample.get("speed")
    if shift_state in ("D", "R", "N") or (
        speed is not None and speed > DAY_MAP_STOP_SPEED
    ):
        return "drive"
    return "park"


def _soc(sample):
    """Prefer usable_battery_level, else battery_level."""
    usable = sample.get("usable_battery_level")
    if usable is not None:
        return float(usable)
    battery = sample.get("battery_level")
    return float(battery) if battery is not None else None


def _is_integer_percent(value):
    """True when value is a whole percent (typical Fleet API SoC)."""
    if value is None:
        return False
    return abs(float(value) - round(float(value))) < 1e-6


def _soc_delta_from_range(start_sample, end_sample, soc_ref, rising):
    """
    Estimate SoC change from rated battery_range delta.

    Fleet API usually reports battery_level as an integer. On short trips the
    displayed SoC does not move, while battery_range still does (tenths of a
    mile). TeslaFi historical imports already have fractional SoC — callers
    should prefer that when available.
    """
    range_start = start_sample.get("battery_range")
    range_end = end_sample.get("battery_range")
    if range_start is None or range_end is None or soc_ref is None or soc_ref <= 1:
        return None
    if rising:
        if range_end <= range_start:
            return None
        delta_range = range_end - range_start
    else:
        if range_start <= range_end:
            return None
        delta_range = range_start - range_end
    full_pack_range = range_start / (soc_ref / 100.0)
    if full_pack_range < 50:
        return None
    return delta_range / full_pack_range * 100.0


def _drive_soc_metrics(start_sample, end_sample):
    """
    SoC start/end/used for a drive segment.

    Prefer API SoC when it is fractional (TeslaFi). When both ends are whole
    percents and the API delta is ~0, refine used (and displayed end) from
    battery_range so short trips still get a kWh/100 km estimate.
    """
    soc_a, soc_b = _soc(start_sample), _soc(end_sample)
    used_api = None
    if soc_a is not None and soc_b is not None:
        used_api = soc_a - soc_b

    used_range = _soc_delta_from_range(
        start_sample,
        end_sample,
        soc_a if soc_a is not None else soc_b,
        rising=False,
    )
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


def _drive_trip_extras(points, geo_start, geo_end):
    """
    Extra metrics for leaderboard ranking (elevation, GPS extremes, outside temp).

    Elevation gain/loss: cumulative sample-to-sample Δh on the drive path
    (plus park anchors when present). Not net end−start.
    Temperature: exterior only (outside_temp), never cabin.
    """
    elev_series = []
    for sample in (geo_start, *points, geo_end):
        if sample is None:
            continue
        elev = sample.get("elevation")
        if elev is None:
            continue
        try:
            elev_series.append(float(elev))
        except (TypeError, ValueError):
            pass

    elev_gain_m = 0.0
    elev_loss_m = 0.0
    if len(elev_series) >= 2:
        previous = elev_series[0]
        for elev in elev_series[1:]:
            delta = elev - previous
            if delta > 0:
                elev_gain_m += delta
            elif delta < 0:
                elev_loss_m += -delta
            previous = elev

    latitudes = []
    longitudes = []
    for sample in (geo_start, *points, geo_end):
        if sample is None:
            continue
        lat, lon = sample.get("lat"), sample.get("lon")
        if lat is None or lon is None:
            continue
        try:
            latitudes.append(float(lat))
            longitudes.append(float(lon))
        except (TypeError, ValueError):
            pass

    temps = []
    for sample in points:
        outside = sample.get("outside_temp")
        if outside is None:
            continue
        try:
            temps.append(float(outside))
        except (TypeError, ValueError):
            pass

    return {
        "elev_gain_m": elev_gain_m if len(elev_series) >= 2 else None,
        "elev_loss_m": elev_loss_m if len(elev_series) >= 2 else None,
        "lat_max": max(latitudes) if latitudes else None,
        "lat_min": min(latitudes) if latitudes else None,
        "lon_max": max(longitudes) if longitudes else None,
        "lon_min": min(longitudes) if longitudes else None,
        "temp_max_c": max(temps) if temps else None,
        "temp_min_c": min(temps) if temps else None,
    }


def _as_float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finalize_drive_segment(start_pt, end_pt, acc, *, min_km, pack_kwh):
    """
    Build one leaderboard trip from first/last drive sample + running accumulators.
    Returns None if under min_km / too short.
    """
    if start_pt is None or end_pt is None:
        return None
    start_t, end_t = start_pt.get("t"), end_pt.get("t")
    if start_t is None or end_t is None:
        return None
    seconds = max(0.0, (end_t - start_t).total_seconds())
    minutes = seconds / 60.0
    hours = seconds / 3600.0
    if minutes < 1.0 and acc["n"] < 3:
        return None

    odo_start = start_pt.get("odometer")
    odo_end = end_pt.get("odometer")
    miles = None
    if odo_start is not None and odo_end is not None and odo_end >= odo_start:
        miles = odo_end - odo_start
    if miles is not None and miles < 0.05 and minutes < 3:
        return None
    km = miles * 1.609344 if miles is not None else None
    if km is None or km < min_km:
        return None

    soc_start, soc_end, soc_used = _drive_soc_metrics(start_pt, end_pt)
    kwh_used = None
    if soc_used is not None and soc_used > 0:
        kwh_used = soc_used / 100.0 * pack_kwh
    kwh_per_100km = None
    if kwh_used is not None and km > 0.2:
        kwh_per_100km = kwh_used / km * 100.0

    day_local = start_t.astimezone(DAY_MAP_TZ).date()
    minutes_int = int(round(minutes))
    hours_part, mins_part = divmod(max(0, minutes_int), 60)
    if hours_part:
        duration_label = _("%(h)s h %(m)s min") % {"h": hours_part, "m": mins_part}
    else:
        duration_label = _("%(m)s min") % {"m": mins_part}
    return {
        "start": start_t,
        "end": end_t,
        "start_local": start_t.astimezone(DAY_MAP_TZ).strftime("%H:%M"),
        "end_local": end_t.astimezone(DAY_MAP_TZ).strftime("%H:%M"),
        "day_iso": day_local.isoformat(),
        "date_display": day_local.strftime("%d/%m/%Y"),
        "minutes": minutes_int,
        "duration_label": duration_label,
        "miles": miles,
        "km": km,
        "avg_mph": (miles / hours) if miles is not None and hours > 0.01 else None,
        "avg_kmh": (km / hours) if hours > 0.01 else None,
        "soc_start": soc_start,
        "soc_end": soc_end,
        "soc_used": soc_used,
        "kwh_used": kwh_used,
        "kwh_per_100km": kwh_per_100km,
        "lat": start_pt.get("lat"),
        "lon": start_pt.get("lon"),
        "end_lat": end_pt.get("lat"),
        "end_lon": end_pt.get("lon"),
        "elev_gain_m": acc["elev_gain"] if acc["elev_n"] >= 2 else None,
        "elev_loss_m": acc["elev_loss"] if acc["elev_n"] >= 2 else None,
        "lat_max": acc["lat_max"],
        "lat_min": acc["lat_min"],
        "lon_max": acc["lon_max"],
        "lon_min": acc["lon_min"],
        "temp_max_c": acc["temp_max"],
        "temp_min_c": acc["temp_min"],
        # Mean outside temp over drive samples (cabin is irrelevant for pack use)
        "temp_avg_c": (
            (acc["temp_sum"] / acc["temp_n"]) if acc["temp_n"] else None
        ),
    }


def _new_drive_acc():
    return {
        "n": 0,
        "elev_prev": None,
        "elev_n": 0,
        "elev_gain": 0.0,
        "elev_loss": 0.0,
        "lat_max": None,
        "lat_min": None,
        "lon_max": None,
        "lon_min": None,
        "temp_max": None,
        "temp_min": None,
        "temp_sum": 0.0,
        "temp_n": 0,
    }


def _acc_add_sample(acc, sample):
    """Update running elev / GPS extremes / outside temp for one drive sample."""
    acc["n"] += 1
    elev = sample.get("elevation")
    if elev is not None:
        if acc["elev_prev"] is not None:
            delta = elev - acc["elev_prev"]
            if delta > 0:
                acc["elev_gain"] += delta
            elif delta < 0:
                acc["elev_loss"] += -delta
        acc["elev_prev"] = elev
        acc["elev_n"] += 1
    lat, lon = sample.get("lat"), sample.get("lon")
    if lat is not None and lon is not None:
        acc["lat_max"] = lat if acc["lat_max"] is None else max(acc["lat_max"], lat)
        acc["lat_min"] = lat if acc["lat_min"] is None else min(acc["lat_min"], lat)
        acc["lon_max"] = lon if acc["lon_max"] is None else max(acc["lon_max"], lon)
        acc["lon_min"] = lon if acc["lon_min"] is None else min(acc["lon_min"], lon)
    temp = sample.get("outside_temp")
    if temp is not None:
        acc["temp_max"] = temp if acc["temp_max"] is None else max(acc["temp_max"], temp)
        acc["temp_min"] = temp if acc["temp_min"] is None else min(acc["temp_min"], temp)
        acc["temp_sum"] += temp
        acc["temp_n"] += 1


def _load_ranked_drives(
    hashed_vin, weeks, *, min_km=DRIVES_MIN_KM, max_trips=DRIVES_MAX_TRIPS
):
    """
    Segment drives ≥ min_km for the period (drive samples only).

    Two-pass strategy (cold ~2–3× faster on dense TeslaFi histories):
      1. Stream Date + odometer only → trip windows + km, keep longest N.
      2. Per-window detail query (elev / GPS / SoC / temp) for those N only.

    SQLite uses partial drive index matesla_snapshot_drive_hv_date when the OR
    condition is *literals* (not bound parameters). Multi-day trips stay one
    segment while samples stay within DRIVES_TRIP_GAP. Cache is sort-agnostic;
    the view re-orders among the capped list.

    ``max_trips``: keep the longest N windows (default leaderboard cap). Use a
    large value for seasonal consumption (need short trips too, not only top-N).
    """
    try:
        max_trips_i = int(max_trips) if max_trips is not None else DRIVES_MAX_TRIPS
    except (TypeError, ValueError):
        max_trips_i = DRIVES_MAX_TRIPS
    if max_trips_i < 1:
        max_trips_i = DRIVES_MAX_TRIPS
    cache_key = (
        f"drives_list_v8:{hashed_vin}:{int(weeks) if weeks else 0}:"
        f"{min_km}:{max_trips_i}"
    )
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    from matesla.BatteryDegradation import pack_kwh_for_vehicle

    pack_kwh = pack_kwh_for_vehicle(hashed_vin=hashed_vin)

    period_sql = ""
    period_params: list = [hashed_vin]
    if weeks is not None and weeks > 0:
        period_sql = 'AND "Date" >= %s '
        period_params.append(timezone.now() - timedelta(weeks=weeks))

    drive_sql = (
        "AND (shift_state IN ('D', 'R', 'N') OR speed > 1.0) "
    )

    # ----- Pass 1: cheap odometer stream → candidate windows -----
    sql_odo = (
        'SELECT "Date", odometer FROM matesla_teslacardatasnapshot '
        f"WHERE hashedVin = %s {period_sql}{drive_sql}"
        'ORDER BY "Date" ASC'
    )
    windows = []  # (start_t, end_t, km)
    seg_start_t = seg_end_t = None
    seg_odo0 = seg_odo1 = None
    last_t = None

    def flush_window():
        nonlocal seg_start_t, seg_end_t, seg_odo0, seg_odo1
        if (
            seg_start_t is not None
            and seg_end_t is not None
            and seg_odo0 is not None
            and seg_odo1 is not None
            and seg_odo1 >= seg_odo0
        ):
            miles = float(seg_odo1) - float(seg_odo0)
            km = miles * 1.609344
            if km >= min_km:
                windows.append((seg_start_t, seg_end_t, km))
        seg_start_t = seg_end_t = None
        seg_odo0 = seg_odo1 = None

    with connection.cursor() as cursor:
        cursor.execute(sql_odo, period_params)
        while True:
            batch = cursor.fetchmany(8000)
            if not batch:
                break
            for sample_t, odometer in batch:
                if sample_t is None:
                    continue
                if last_t is not None and (sample_t - last_t) > DRIVES_TRIP_GAP:
                    flush_window()
                if seg_start_t is None:
                    seg_start_t = sample_t
                    seg_odo0 = odometer
                seg_end_t = sample_t
                if odometer is not None:
                    seg_odo1 = odometer
                last_t = sample_t
    flush_window()

    # Longest first; secondary rankings (elev/temp) only among these.
    windows.sort(key=lambda item: item[2], reverse=True)
    if len(windows) > max_trips_i:
        windows = windows[:max_trips_i]

    # ----- Pass 2: full metrics only inside the kept windows -----
    detail_sql = (
        'SELECT "Date", latitude, longitude, odometer, '
        "battery_level, usable_battery_level, battery_range, "
        "elevation, outside_temp "
        "FROM matesla_teslacardatasnapshot "
        "WHERE hashedVin = %s AND \"Date\" >= %s AND \"Date\" <= %s "
        f"{drive_sql}"
        'ORDER BY "Date" ASC'
    )

    result = []
    with connection.cursor() as cursor:
        for start_t, end_t, _km in windows:
            cursor.execute(detail_sql, [hashed_vin, start_t, end_t])
            samples = cursor.fetchall()
            if not samples:
                continue
            start_pt = end_pt = None
            acc = _new_drive_acc()
            for sample in samples:
                (
                    sample_t,
                    lat,
                    lon,
                    odometer,
                    battery_level,
                    usable_battery_level,
                    battery_range,
                    elevation,
                    outside_temp,
                ) = sample
                if sample_t is None:
                    continue
                # SQLite already returns floats for real columns; keep None-safe.
                row = {
                    "t": sample_t,
                    "lat": lat if lat is None else float(lat),
                    "lon": lon if lon is None else float(lon),
                    "odometer": odometer if odometer is None else float(odometer),
                    "battery_level": (
                        battery_level
                        if battery_level is None
                        else float(battery_level)
                    ),
                    "usable_battery_level": (
                        usable_battery_level
                        if usable_battery_level is None
                        else float(usable_battery_level)
                    ),
                    "battery_range": (
                        battery_range
                        if battery_range is None
                        else float(battery_range)
                    ),
                    "elevation": (
                        elevation if elevation is None else float(elevation)
                    ),
                    "outside_temp": (
                        outside_temp
                        if outside_temp is None
                        else float(outside_temp)
                    ),
                }
                if start_pt is None:
                    start_pt = row
                end_pt = row
                _acc_add_sample(acc, row)
            trip = _finalize_drive_segment(
                start_pt, end_pt, acc, min_km=min_km, pack_kwh=pack_kwh
            )
            if trip is not None:
                result.append(trip)

    try:
        cache.set(cache_key, result, DRIVES_CACHE_SECONDS)
    except Exception:
        pass
    return result


def _drives_sort_label(sort_key):
    labels = {
        "longest": _("Longest"),
        "elev_up": _("Most elevation gain"),
        "elev_down": _("Most elevation loss"),
        "hot": _("Hottest"),
        "cold": _("Coldest"),
        "soc_end": _("Lowest end SoC"),
    }
    return labels.get(sort_key, labels[DRIVES_SORT_DEFAULT])


def _drives_score_label(sort_key):
    labels = {
        "longest": _("Distance"),
        "elev_up": _("Elevation gain"),
        "elev_down": _("Elevation loss"),
        "hot": _("Max outside temp"),
        "cold": _("Min outside temp"),
        "soc_end": _("SoC end"),
    }
    return labels.get(sort_key, labels[DRIVES_SORT_DEFAULT])


def _format_drive_score(sort_key, trip, unit=None):
    """Human score for the active ranking criterion."""
    from matesla.units import format_distance, get_distance_unit

    field, _ = DRIVES_SORT_SPECS.get(sort_key, DRIVES_SORT_SPECS[DRIVES_SORT_DEFAULT])
    value = trip.get(field)
    if value is None:
        return "—"
    if sort_key == "longest":
        # Prefer raw miles when present so unit preference is applied once
        miles = trip.get("miles")
        if miles is not None:
            return format_distance(miles, unit or get_distance_unit(), decimals=1)
        return format_distance(
            float(value) / 1.609344, unit or get_distance_unit(), decimals=1
        )
    if sort_key in ("elev_up", "elev_down"):
        return f"{value:.0f} m"
    if sort_key in ("hot", "cold"):
        return f"{value:.1f} °C"
    if sort_key == "soc_end":
        return f"{value:.1f} %"
    return str(value)


def _sort_ranked_drives(drives, sort_key):
    field, reverse = DRIVES_SORT_SPECS.get(
        sort_key, DRIVES_SORT_SPECS[DRIVES_SORT_DEFAULT]
    )

    def sort_key_fn(trip):
        value = trip.get(field)
        # Missing metric: always rank last
        if value is None:
            return (1, 0)
        return (0, -value if reverse else value)

    return sorted(drives, key=sort_key_fn)


def _charge_soc_metrics(start_sample, end_sample):
    """SoC start/end/added for a charge segment (same coarse-SoC refinement)."""
    soc_a, soc_b = _soc(start_sample), _soc(end_sample)
    added_api = None
    if soc_a is not None and soc_b is not None:
        added_api = soc_b - soc_a

    added_range = _soc_delta_from_range(
        start_sample,
        end_sample,
        soc_a if soc_a is not None else soc_b,
        rising=True,
    )
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
      - Addresses / odo: prefer adjacent park/charge samples so the map
        shows the real parking GPS, not the last mid-road D sample.
      - Drive SoC end: when the next group is charge, use the last in-gear
        sample — a late Supercharger poll may already be mid-session with
        SoC much higher than arrival (would look like regen over 200 km).
      - Charge SoC start: if the first charge poll is ≥8 pts above the last
        drive SoC, backfill start from that drive sample (and keep single
        late charge polls instead of dropping them).
      - Charge clock / kWh (sparse SC stops): Tesla ``charge_energy_added`` is
        a *session total* (resets on plug-in), not a lifetime counter. Use the
        max over Charging samples plus the immediate post-charge park/drive
        sample when it still holds the session total. Extend start time to a
        prior Stopped park or to the last drive sample when the first charge
        poll is already mid-session; extend end time to the first sample after
        unplug so short Supercharges are not truncated to 2 poll ticks.
    """
    if not rows:
        return [], []

    # Group consecutive points of the same kind
    groups = []
    current_kind = _point_kind(rows[0])
    current_points = [rows[0]]
    for sample in rows[1:]:
        kind = _point_kind(sample)
        if kind == current_kind:
            current_points.append(sample)
        else:
            groups.append((current_kind, current_points))
            current_kind = kind
            current_points = [sample]
    groups.append((current_kind, current_points))

    # Max gap (minutes) to borrow pre/post samples for charge timing & energy.
    charge_anchor_gap_min = 8.0

    drives = []
    charges = []
    for group_index, (kind, points) in enumerate(groups):
        if len(points) < 1:
            continue
        start_pt, end_pt = points[0], points[-1]
        seconds = max(0.0, (end_pt["t"] - start_pt["t"]).total_seconds())
        minutes = seconds / 60.0
        hours = seconds / 3600.0

        if kind == "drive":
            # Timing anchors: actual drive samples only for start clock
            time_start_pt = points[0]
            time_end_pt = points[-1]
            # Address / odo anchors (may use parking or first charge GPS)
            geo_start = time_start_pt
            geo_end = time_end_pt
            next_kind = None
            if group_index > 0 and groups[group_index - 1][0] in ("park", "charge"):
                previous_points = groups[group_index - 1][1]
                if previous_points:
                    geo_start = previous_points[-1]
            if group_index + 1 < len(groups) and groups[group_index + 1][0] in (
                "park",
                "charge",
            ):
                next_kind = groups[group_index + 1][0]
                next_points = groups[group_index + 1][1]
                if next_points:
                    # Arrival park/charge: end clock + GPS (trip finished when parked)
                    time_end_pt = next_points[0]
                    geo_end = next_points[0]

            seconds = max(
                0.0, (time_end_pt["t"] - time_start_pt["t"]).total_seconds()
            )
            minutes = seconds / 60.0
            hours = seconds / 3600.0

            # Need a real movement span
            if minutes < 1.0 and len(points) < 3:
                continue
            # Odo from parking/charge GPS when available (full trip), else drive ends
            odo_start = geo_start.get("odometer")
            odo_end = geo_end.get("odometer")
            miles = None
            if odo_start is not None and odo_end is not None and odo_end >= odo_start:
                miles = odo_end - odo_start
            # Tiny odo blips are noise
            if miles is not None and miles < 0.05 and minutes < 3:
                continue
            km = miles * 1.609344 if miles is not None else None
            # SoC must not use a mid-session charge poll as "arrival" energy.
            # Sparse capture often first sees a Supercharger after many kWh
            # already went in (end SoC >> last drive SoC). GPS/odo still use
            # geo_end; consumption uses last in-gear sample when next is charge.
            soc_end_pt = geo_end
            if next_kind == "charge":
                soc_end_pt = points[-1]
            soc_start, soc_end, soc_used = _drive_soc_metrics(geo_start, soc_end_pt)
            kwh_used = None
            if soc_used is not None and soc_used > 0:
                kwh_used = soc_used / 100.0 * pack_kwh
            kwh_per_100km = None
            if kwh_used is not None and km is not None and km > 0.2:
                kwh_per_100km = kwh_used / km * 100.0
            avg_mph = (
                (miles / hours) if (miles is not None and hours > 0.01) else None
            )
            avg_kmh = (km / hours) if (km is not None and hours > 0.01) else None
            trip_extras = _drive_trip_extras(points, geo_start, geo_end)
            drives.append(
                {
                    "kind": "drive",
                    "start": time_start_pt["t"],
                    "end": time_end_pt["t"],
                    "start_local": time_start_pt["t"]
                    .astimezone(DAY_MAP_TZ)
                    .strftime("%H:%M"),
                    "end_local": time_end_pt["t"]
                    .astimezone(DAY_MAP_TZ)
                    .strftime("%H:%M"),
                    "minutes": int(round(minutes)),
                    "miles": miles,
                    "km": km,
                    "avg_mph": avg_mph,
                    "avg_kmh": avg_kmh,
                    "soc_start": soc_start,
                    "soc_end": soc_end,
                    "soc_used": soc_used,
                    "kwh_used": kwh_used,
                    "kwh_per_100km": kwh_per_100km,
                    "lat": geo_start.get("lat"),
                    "lon": geo_start.get("lon"),
                    "end_lat": geo_end.get("lat"),
                    "end_lon": geo_end.get("lon"),
                    **trip_extras,
                }
            )
        elif kind == "charge":
            # Previous / next groups: anchors when sparse capture misses plug-in
            # and unplug edges of short Supercharger stops.
            prev_kind = groups[group_index - 1][0] if group_index > 0 else None
            prev_pts = groups[group_index - 1][1] if group_index > 0 else None
            next_kind = (
                groups[group_index + 1][0]
                if group_index + 1 < len(groups)
                else None
            )
            next_pts = (
                groups[group_index + 1][1]
                if group_index + 1 < len(groups)
                else None
            )

            prev_drive_end = None
            if prev_kind == "drive" and prev_pts:
                prev_drive_end = prev_pts[-1]

            time_start_pt = start_pt
            time_end_pt = end_pt
            # Samples that may still hold this session's charge_energy_added
            energy_points = list(points)

            # --- Post-charge anchor (Disconnected / first drive away) ---
            # charge_energy_added often keeps rising until the sample after
            # charging_state leaves Charging; SoC too.
            if next_pts and next_kind in ("park", "drive"):
                next_first = next_pts[0]
                gap_post = (
                    max(0.0, (next_first["t"] - end_pt["t"]).total_seconds()) / 60.0
                )
                if gap_post <= charge_anchor_gap_min:
                    last_e = end_pt.get("charge_energy_added")
                    next_e = next_first.get("charge_energy_added")
                    end_soc = _soc(end_pt)
                    next_soc = _soc(next_first)
                    # New session reset (counter dropped) → do not extend
                    energy_reset = (
                        next_e is not None
                        and last_e is not None
                        and next_e < float(last_e) - 1.0
                        and next_e < float(last_e) * 0.5
                    )
                    energy_continues = (
                        next_e is not None
                        and float(next_e) > 0.3
                        and (
                            last_e is None
                            or float(next_e) + 0.05 >= float(last_e)
                        )
                    )
                    soc_continues = (
                        next_soc is not None
                        and end_soc is not None
                        and next_soc + 0.5 >= end_soc
                    )
                    if not energy_reset and (energy_continues or soc_continues):
                        time_end_pt = next_first
                        energy_points = list(points) + [next_first]

            soc_start, soc_end, soc_added = _charge_soc_metrics(
                start_pt, time_end_pt
            )
            # Backfill start SoC from last drive when first charge sample is late
            mid_session_backfill = False
            if prev_drive_end is not None:
                prev_soc = _soc(prev_drive_end)
                first_charge_soc = _soc(start_pt)
                # ≥8 pts: first poll is mid-session (missed early SC kWh).
                # Normal dense capture is only +1–5 pts by the first charge row.
                if (
                    prev_soc is not None
                    and first_charge_soc is not None
                    and first_charge_soc >= prev_soc + 8.0
                ):
                    mid_session_backfill = True
                    soc_start = prev_soc
                    if soc_end is not None:
                        soc_added = soc_end - soc_start
                    else:
                        soc_added = first_charge_soc - prev_soc
                        soc_end = first_charge_soc

            # --- Pre-charge anchor (Stopped park or mid-session arrival) ---
            if prev_pts:
                prev_last = prev_pts[-1]
                gap_pre = (
                    max(0.0, (start_pt["t"] - prev_last["t"]).total_seconds())
                    / 60.0
                )
                if gap_pre <= charge_anchor_gap_min:
                    prev_state = (prev_last.get("charging_state") or "").strip().lower()
                    if prev_kind == "park" and prev_state in (
                        "stopped",
                        "starting",
                        "complete",
                    ):
                        # Plugged / waiting for power before first Charging row
                        time_start_pt = prev_last
                        energy_points = [prev_last] + energy_points
                    elif prev_kind == "drive" and mid_session_backfill:
                        # Arrival sample is the best plug-in clock we have
                        time_start_pt = prev_last

            seconds = max(
                0.0, (time_end_pt["t"] - time_start_pt["t"]).total_seconds()
            )
            minutes = seconds / 60.0

            sparse_single = minutes < 1.0 and len(points) < 2
            if sparse_single and not mid_session_backfill:
                # Single glitchy charge poll with no clear energy gain — skip
                continue
            if sparse_single and mid_session_backfill:
                # Keep the stop visible even with one late poll
                minutes = max(minutes, 1.0)

            # kWh: Tesla charge_energy_added is the *session total* since plug-in.
            # Max over the session window (incl. post-unplug sample) — not a
            # delta between first and last Charging polls (that misses both
            # edges on short Supercharges).
            session_energy_vals = []
            for point in energy_points:
                energy = point.get("charge_energy_added")
                if energy is not None:
                    try:
                        energy_f = float(energy)
                    except (TypeError, ValueError):
                        continue
                    if energy_f > 0.05:
                        session_energy_vals.append(energy_f)
            kwh_added = None
            if session_energy_vals:
                kwh_added = max(session_energy_vals)
            elif soc_added is not None and soc_added > 0:
                kwh_added = soc_added / 100.0 * pack_kwh
            # Effective kW: charger_power when set, else V×I×phases (AC wall
            # often has charger_power=0 in TeslaFi while current is filled).
            # Min skips Supercharger ramp on DC only; AC uses plain min/max.
            from personalstats.dc_charge import (
                charge_power_min_max_excluding_ramp,
                effective_charger_power_kw,
            )

            timed_powers = []
            for point in points:
                if point.get("t") is None:
                    continue
                power_kw = effective_charger_power_kw(point)
                if power_kw is not None:
                    timed_powers.append((point["t"], power_kw))
            min_power, max_power = charge_power_min_max_excluding_ramp(timed_powers)
            # Energy / duration fallback when Tesla reports neither power nor current
            if (max_power is None or max_power < 0.3) and kwh_added and minutes >= 2:
                avg_kw = float(kwh_added) / (minutes / 60.0)
                if avg_kw >= 0.3:
                    max_power = avg_kw
                    if min_power is None or min_power < 0.3:
                        min_power = avg_kw
            # mid GPS for address
            gps_points = [
                point
                for point in points
                if point.get("lat") is not None and point.get("lon") is not None
            ]
            mid = gps_points[len(gps_points) // 2] if gps_points else start_pt
            # Flag DC-ish stops for async Supercharger map matching (not AC wall).
            from personalstats.dc_charge import DC_SESSION_PEAK_KW_MIN

            is_dc_candidate = (
                max_power is not None and float(max_power) >= DC_SESSION_PEAK_KW_MIN
            )
            # Late single SC poll often still reports high charger_power
            if not is_dc_candidate and mid_session_backfill:
                peak = end_pt.get("charger_power")
                if peak is not None and float(peak) >= DC_SESSION_PEAK_KW_MIN:
                    is_dc_candidate = True
                    if max_power is None or max_power < float(peak):
                        max_power = float(peak)
            try:
                start_ts = int(time_start_pt["t"].timestamp())
            except Exception:
                start_ts = None
            charges.append(
                {
                    "kind": "charge",
                    "start": time_start_pt["t"],
                    "end": time_end_pt["t"],
                    "start_ts": start_ts,
                    "start_local": time_start_pt["t"]
                    .astimezone(DAY_MAP_TZ)
                    .strftime("%H:%M"),
                    "end_local": time_end_pt["t"]
                    .astimezone(DAY_MAP_TZ)
                    .strftime("%H:%M"),
                    "minutes": int(round(minutes)),
                    "soc_start": soc_start,
                    "soc_end": soc_end,
                    "soc_added": soc_added,
                    "kwh_added": kwh_added,
                    "min_power_kw": min_power,
                    "max_power_kw": max_power,
                    "is_dc_candidate": is_dc_candidate,
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
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied

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
    queryset = (
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
            "charger_actual_current",
            "charger_voltage",
            "charger_phases",
            "charge_energy_added",
            "battery_range",
        )
    )

    raw_rows = []
    vin = None
    for sample in queryset.iterator(chunk_size=2000):
        if vin is None and sample.vin:
            vin = sample.vin
        raw_rows.append(
            {
                "t": sample.Date,
                "lat": float(sample.latitude) if sample.latitude is not None else None,
                "lon": float(sample.longitude) if sample.longitude is not None else None,
                "speed": float(sample.speed) if sample.speed is not None else None,
                "odometer": float(sample.odometer) if sample.odometer is not None else None,
                "shift_state": sample.shift_state,
                "battery_level": float(sample.battery_level)
                if sample.battery_level is not None
                else None,
                "usable_battery_level": float(sample.usable_battery_level)
                if sample.usable_battery_level is not None
                else None,
                "battery_range": float(sample.battery_range)
                if sample.battery_range is not None
                else None,
                "charging_state": sample.charging_state,
                "charger_power": float(sample.charger_power)
                if sample.charger_power is not None
                else None,
                "charger_actual_current": float(sample.charger_actual_current)
                if sample.charger_actual_current is not None
                else None,
                "charger_voltage": float(sample.charger_voltage)
                if sample.charger_voltage is not None
                else None,
                "charger_phases": float(sample.charger_phases)
                if sample.charger_phases is not None
                else None,
                "charge_energy_added": float(sample.charge_energy_added)
                if sample.charge_energy_added is not None
                else None,
            }
        )

    # Map polyline: GPS only
    gps_rows = [
        point
        for point in raw_rows
        if point["lat"] is not None and point["lon"] is not None
    ]
    total_points = len(gps_rows)
    indices = _downsample_indices(total_points, DAY_MAP_MAX_POINTS)
    path = [
        {
            "lat": gps_rows[index]["lat"],
            "lon": gps_rows[index]["lon"],
            "t": gps_rows[index]["t"].astimezone(DAY_MAP_TZ).strftime("%H:%M:%S"),
            "speed": gps_rows[index]["speed"],
        }
        for index in indices
    ]

    # Pack size for kWh estimates
    epa = None
    if vin:
        from matesla.models.TeslaCarInfo import TeslaCarInfo

        info = TeslaCarInfo.objects.filter(vin=vin).first()
        if info and info.EPARange:
            epa = info.EPARange
    from matesla.BatteryDegradation import estimate_new_pack_kwh

    pack_kwh = estimate_new_pack_kwh(epa)

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
    for drive in drives:
        drive["start_address"] = addr_cached(drive.get("lat"), drive.get("lon"))
        drive["end_address"] = addr_cached(drive.get("end_lat"), drive.get("end_lon"))
    for charge in charges:
        charge["address"] = addr_cached(charge.get("lat"), charge.get("lon"))
    # Sparse poll: last drive may end elsewhere than the car's final GPS
    unmonitored_tail = _daymap_unmonitored_tail(drives, end_lat, end_lon)

    # Day totals from drives (same metrics as the drives table)
    miles_driven = sum(drive["miles"] or 0 for drive in drives) or None
    miles_driven_km = sum(drive["km"] or 0 for drive in drives) or None
    if miles_driven == 0:
        miles_driven = None
    if miles_driven_km == 0:
        miles_driven_km = None
    drive_hours = sum(
        max(0.0, (drive["end"] - drive["start"]).total_seconds()) / 3600.0
        for drive in drives
    )
    day_drive_minutes = (
        int(round(sum(drive.get("minutes") or 0 for drive in drives)))
        if drives
        else None
    )
    # Human label for the day summary (e.g. "1 h 24 min" / "6 min")
    day_duration_label = None
    if day_drive_minutes is not None:
        hours, mins = divmod(max(0, day_drive_minutes), 60)
        if hours:
            day_duration_label = _("%(h)s h %(m)s min") % {"h": hours, "m": mins}
        else:
            day_duration_label = _("%(m)s min") % {"m": mins}
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

    #Mehdi 2//2026: logic on getting soc begin/end from drives is bad, to have soc at begin and end of day, just use respectively first and last row of the day.
    day_soc_start = _soc(raw_rows[0]) if raw_rows else None
    day_soc_end = _soc(raw_rows[-1]) if raw_rows else None

    soc_used_vals = [
        drive["soc_used"] for drive in drives if drive.get("soc_used") is not None
    ]
    day_soc_used = sum(soc_used_vals) if soc_used_vals else None
    # Residual SoC drop not explained by drives (park climate, sentry, dog/camp,
    # preconditioning, vampire drain). Between first drive start and last drive
    # end: (start − end) − drive_used + charged.
    day_soc_charged = sum(
        charge["soc_added"]
        for charge in charges
        if charge.get("soc_added") is not None
    )
    day_soc_non_drive = None
    if (
        day_soc_start is not None
        and day_soc_end is not None
        and day_soc_used is not None
    ):
        day_soc_non_drive = (
            (day_soc_start - day_soc_end) - day_soc_used + day_soc_charged
        )
        # Measurement noise can go slightly negative; clamp for display
        if day_soc_non_drive < 0:
            day_soc_non_drive = 0.0
    kwh_used_vals = [
        drive["kwh_used"] for drive in drives if drive.get("kwh_used") is not None
    ]
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
        [{"type": "drive", **drive} for drive in drives]
        + [{"type": "charge", **charge} for charge in charges],
        key=lambda item: item["start"],
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
            "unmonitored_tail": unmonitored_tail,
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
            "day_drive_minutes": day_drive_minutes,
            "day_duration_label": day_duration_label,
            "day_soc_start": day_soc_start,
            "day_soc_end": day_soc_end,
            "day_soc_used": day_soc_used,
            "day_soc_non_drive": day_soc_non_drive,
            "day_kwh_per_100km": day_kwh_per_100km,
            "pack_kwh_estimate": pack_kwh,
            "parse_error": parse_error,
            "has_track": total_points > 0 or len(raw_rows) > 0,
            "has_gps": total_points > 0,
        }
    )
    return render(request, "personalstats/daymap.html", context)


@require_GET
def Drives(request, hashedVin):
    """
    Multi-criteria drive leaderboard (≥ 20 km): longest, elevation, cardinals, temp.

    Not charts (My data) and not a single-day replay (Day map) — ranked trip list.
    """
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied

    weeks = resolve_stats_period(request)
    sort_key = (request.GET.get("sort") or DRIVES_SORT_DEFAULT).strip().lower()
    if sort_key not in DRIVES_SORT_SPECS:
        sort_key = DRIVES_SORT_DEFAULT

    try:
        page_size = int(request.GET.get("page_size") or DRIVES_DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DRIVES_DEFAULT_PAGE_SIZE
    if page_size not in DRIVES_PAGE_SIZES:
        page_size = DRIVES_DEFAULT_PAGE_SIZE

    try:
        page = int(request.GET.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    all_drives = _load_ranked_drives(hashedVin, weeks, min_km=DRIVES_MIN_KM)
    ranked = _sort_ranked_drives(all_drives, sort_key)
    total = len(ranked)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page > total_pages:
        page = total_pages
    start_index = (page - 1) * page_size
    page_drives = ranked[start_index : start_index + page_size]

    from matesla.models.AddressFromLatLong import LookupCachedAddress

    def addr_cached(lat, lon):
        if lat is None or lon is None:
            return None
        try:
            return LookupCachedAddress(round(float(lat), 4), round(float(lon), 4))
        except Exception:
            return None

    from matesla.units import get_distance_unit, km_to_display

    unit = get_distance_unit(request)
    for rank_offset, drive in enumerate(page_drives, start=start_index + 1):
        drive["rank"] = rank_offset
        drive["score_display"] = _format_drive_score(sort_key, drive, unit=unit)
        drive["start_address"] = addr_cached(drive.get("lat"), drive.get("lon"))
        drive["end_address"] = addr_cached(drive.get("end_lat"), drive.get("end_lon"))

    sort_choices = [
        {"key": key, "label": _drives_sort_label(key)} for key in DRIVES_SORT_SPECS
    ]
    from matesla.BatteryDegradation import pack_kwh_for_vehicle

    context = _vehicle_chrome_context(request, hashedVin)
    context.update(
        {
            "drives": page_drives,
            "total_drives": total,
            "min_km": DRIVES_MIN_KM,
            "min_distance_display": km_to_display(DRIVES_MIN_KM, unit),
            "max_trips": DRIVES_MAX_TRIPS,
            "sort_key": sort_key,
            "sort_label": _drives_sort_label(sort_key),
            # When ranking by distance or end SoC, that column *is* the score —
            # hide the redundant score column.
            "show_score_column": sort_key not in ("longest", "soc_end"),
            "score_column_label": _drives_score_label(sort_key),
            "sort_choices": sort_choices,
            "stats_period": weeks,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "page_sizes": sorted(DRIVES_PAGE_SIZES),
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1,
            "next_page": page + 1,
            "pack_kwh_estimate": pack_kwh_for_vehicle(hashed_vin=hashedVin),
        }
    )
    return render(request, "personalstats/drives.html", context)


@require_GET
def LifetimeMapData(request, hashedVin):
    """
    JSON for the personal-stats lifetime map card.

    Query: ?period=<weeks> (same values as #DesiredPeriod; 0 = all history).
    Returns polylines + summary KPIs (drives, km, efficiency, …).
    """
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="json")
    if denied:
        return denied
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

    from matesla.units import (
        get_distance_unit,
        is_km,
        km_to_display,
        unit_labels,
        wh_per_km_to_display,
    )

    unit = get_distance_unit(request)
    # Cache metric (km) payload only; convert per request for unit preference
    cache_key = f"lifetime_map_v2:{hashedVin}:{weeks}"
    cache_backend = None
    payload = None
    try:
        from django.core.cache import cache as cache_backend

        payload = cache_backend.get(cache_key)
    except Exception:
        cache_backend = None

    if payload is None:
        payload = _build_lifetime_map_payload(hashedVin, weeks)
        if cache_backend is not None:
            try:
                cache_backend.set(cache_key, payload, LIFETIME_MAP_CACHE_SECONDS)
            except Exception:
                pass

    # Shallow copy + unit-aware KPI fields (paths stay lat/lon)
    out = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(out, dict) and out.get("ok"):
        labels = unit_labels(unit)
        km_driven = out.get("km_driven")
        rated_km = out.get("rated_km_used")
        wh_km = out.get("wh_per_km")
        avg_kmh = out.get("avg_kmh")
        dist = km_to_display(km_driven, unit)
        rated = km_to_display(rated_km, unit)
        wh = wh_per_km_to_display(wh_km, unit)
        avg_speed = km_to_display(avg_kmh, unit)
        out = {
            **out,
            "distance_unit": unit,
            "u_dist": labels["distance"],
            "u_speed": labels["speed"],
            "u_wh_dist": labels["wh_dist"],
            "distance_driven": round(dist, 1) if dist is not None else dist,
            "rated_distance_used": round(rated, 1) if rated is not None else rated,
            "wh_per_distance": round(wh) if wh is not None else wh,
            "avg_speed": round(avg_speed, 1) if avg_speed is not None else avg_speed,
            # Keep legacy keys for older clients, already in display unit
            "km_driven": round(dist, 1) if dist is not None else dist,
            "rated_km_used": round(rated, 1) if rated is not None else rated,
            "wh_per_km": round(wh) if wh is not None else wh,
            "avg_kmh": round(avg_speed, 1) if avg_speed is not None else avg_speed,
            "is_metric": is_km(unit),
        }
    return JsonResponse(out)


@require_GET
def ResolveAddress(request):
    """
    Async reverse-geocode for DayMap (and similar).

    Query: ?lat=50.7868&lon=4.3517
    Returns JSON: {ok, lat, lon, address, cached, error?}
    Cache hits are instant; misses call Geoapify (if key) or Nominatim under quota.
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

    lat_rounded, lon_rounded = round(lat, 4), round(lon, 4)
    from matesla.models.AddressFromLatLong import (
        LookupCachedAddress,
        GetAddressFromLatLong,
    )

    cached = LookupCachedAddress(lat_rounded, lon_rounded)
    if cached is not None:
        return JsonResponse(
            {
                "ok": True,
                "lat": lat_rounded,
                "lon": lon_rounded,
                "address": cached,
                "cached": True,
            }
        )

    # Interactive (day-map / drives AJAX): may use the full hard daily cap.
    # Capture backfill uses a softer budget so this path still works daytime.
    address = GetAddressFromLatLong(lat_rounded, lon_rounded)
    if not address or address == "Unknown":
        return JsonResponse(
            {
                "ok": False,
                "lat": lat_rounded,
                "lon": lon_rounded,
                "address": None,
                "cached": False,
                # Usually Nominatim daily budget exhausted (or network failure).
                # Not blocked by Tailscale read-only (GET is whitelisted).
                "error": "unresolved_or_quota",
            }
        )
    return JsonResponse(
        {
            "ok": True,
            "lat": lat_rounded,
            "lon": lon_rounded,
            "address": address,
            "cached": False,
        }
    )


@require_GET
def MatchSupercharger(request):
    """
    Async: nearest Tesla Supercharger within ~400 m of lat/lon (if any).

    Day map calls this after paint for DC charge stops only — never blocks
    the initial HTML. Directory is cached 12 h (supercharge.info).

    Query: ?lat=&lon=
    JSON: {ok, match: null | {name, power_kw, stalls, distance_m, url, ...}}
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

    from matesla.superchargers import nearest_supercharger

    match = nearest_supercharger(lat, lon)
    return JsonResponse(
        {
            "ok": True,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "match": match,
        }
    )


# returns data stored in db for the user is CSV-->the only info from the car
# we need is the vin to filter results
def view_AllMyDataAsCSV(request, hashedVin):
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="raw")
    if denied:
        return denied
    if TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count() == 0:
        return HttpResponseNotFound(_("Unknown vehicle."))
    query = "select * from matesla_teslacardatasnapshot where \"hashedVin\"='" + hashedVin + "';"
    return PrepareCSVFromQuery(query)


def BatteryDegradationGraph(request, hashedVin, desiredfield, desiredperiod=0):
    """
    Scatter graphs for battery health tab.

    - odometer: X=odometer, Y=battery_degradation (%)
    - range_at_100_odometer: X=odometer, Y=extrapolated range at 100% SoC

    desiredperiod is weeks (0 = all), same meaning as StatsOnCarGraph / #DesiredPeriod.
    Optional query ?size=thumb|full (default full).
    """
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="raw")
    if denied:
        return denied
    unit = _distance_unit_from_request(request)
    size = graph_size_from_request(request)
    cache_key = _graph_png_cache_key(
        hashedVin, desiredfield, desiredperiod, size, kind="degrad", unit=unit
    )
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None:
        return _png_response_from_bytes(hit, size, cache_status="HIT")

    response = _battery_degradation_graph_uncached(
        hashedVin, desiredfield, desiredperiod, size, unit=unit
    )
    return _cache_graph_png(cache_key, response, size)


def _battery_degradation_graph_uncached(
    hashedVin, desiredfield, desiredperiod, size, unit=None
):
    # Computed scatter (Y = range at 100%), not a real model field on X axis alone
    if desiredfield == "range_at_100_odometer":
        if not IsValidHash(hashedVin):
            return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
        title = GetTitleForField(desiredfield, unit=unit)
        # Full period via raw SQL (no ORM dict walk / no early-history row cap).
        xvalues, yvalues = load_degradation_scatter_xy(
            hashedVin,
            "odometer",
            desiredperiod,
            y_mode="range_at_100",
        )
        if not xvalues:
            return GenerateScatterGraph(None, None, title, size=size, unit=unit)
        xvalues = _scale_miles_series(xvalues, unit)
        yvalues = _scale_miles_series(yvalues, unit)
        return GenerateScatterGraph(xvalues, yvalues, title, size=size, unit=unit)

    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response

    title = GetTitleForField(desiredfield, unit=unit)
    # odometer (and any future X field mapped in load_degradation_scatter_xy)
    if desiredfield == "odometer":
        xvalues, yvalues = load_degradation_scatter_xy(
            hashedVin,
            desiredfield,
            desiredperiod,
            y_mode="battery_degradation",
        )
        if not xvalues:
            return GenerateScatterGraph(None, None, title, size=size, unit=unit)
        if desiredfield in _MILES_VALUE_FIELDS:
            xvalues = _scale_miles_series(xvalues, unit)
        return GenerateScatterGraph(xvalues, yvalues, title, size=size, unit=unit)

    # Fallback ORM path for any other scatter X still routed here
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    queryset = _period_filter(degradation_scatter_queryset(base), desiredperiod)
    if not queryset.exists():
        return GenerateScatterGraph(None, None, title, size=size, unit=unit)

    results = queryset.order_by("Date").values(
        desiredfield,
        "battery_degradation",
        "Date",
        "usable_battery_level",
        "battery_level",
        "charging_state",
    )
    xvalues, yvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    if desiredfield in _MILES_VALUE_FIELDS:
        xvalues = _scale_miles_series(xvalues, unit)
    return GenerateScatterGraph(xvalues, yvalues, title, size=size, unit=unit)

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
    entry_count = len(entries_chrono)
    for index, row in enumerate(entries_chrono):
        start = row.Date
        if index + 1 < entry_count:
            end = entries_chrono[index + 1].Date
        else:
            end = today
        days_on = None
        if start and end:
            try:
                days_on = max(0, (end - start).days)
            except Exception:
                days_on = None
        version_text = (row.Version or "").strip()
        # "2025.20.3 8252e1d331" → primary "2025.20.3", build hash aside
        parts = version_text.split(None, 1)
        items.append(
            {
                "date": start,
                "version": version_text,
                "version_short": parts[0] if parts else version_text,
                "version_build": parts[1] if len(parts) > 1 else "",
                "days_on": days_on,
                "is_current": not row.IsArchive and index == entry_count - 1,
                "is_archive": bool(row.IsArchive),
            }
        )
    # Newest first (left); scroll right for older builds
    items.reverse()
    return items


# Display page with car firmware history
def FirmwareHistory(request, hashedVin):
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied
    qs_chrono = list(
        TeslaFirmwareHistory.objects.filter(hashedVin=hashedVin).order_by("Date", "id")
    )
    context = _vehicle_chrome_context(request, hashedVin)
    context["firmware_timeline"] = _firmware_timeline(qs_chrono)
    return render(request, "personalstats/FirmwareHistory.html", context)


# returns CSV with firmware history for the car
def FirmwareHistoryCSV(request, hashedVin):
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="raw")
    if denied:
        return denied
    if TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count() == 0:
        return HttpResponseNotFound(_("Unknown vehicle."))
    query = (
        'select "Version","Date" from matesla_teslafirmwarehistory '
        f"where \"hashedVin\"='{hashedVin}' order by 2 desc;"
    )
    return PrepareCSVFromQuery(query)

# ---------------------------------------------------------------------------
# DC fast-charge analytics (power vs SoC, SoC vs time by start SoC)
# ---------------------------------------------------------------------------

from personalstats.dc_charge import (
    ENVELOPE_MODES,
    OUTLIER_MODES,
    RANGE_Y_MODES,
    SEASONAL_LOOKBACK_WEEKS,
    SEASONAL_TRIP_MIN_KM,
    charge_session_curve_series,
    envelope_mode_label,
    filter_outlier_sessions,
    full_real_range_km,
    load_dc_sessions,
    outlier_mode_label,
    power_curve_extreme_rows,
    power_vs_soc_curve,
    range_y_mode_label,
    scale_soc_time_curves_to_range,
    select_seasonal_kwh_per_100km,
    soc_vs_time_curves,
    summarize_sessions,
)
from matesla.graphstyle import CYAN, render_png


def _parse_dc_outlier_mode(raw) -> str:
    mode = (raw or "robust").strip().lower()
    return mode if mode in OUTLIER_MODES else "robust"


def _parse_dc_envelope_mode(raw) -> str:
    mode = (raw or "p10_p90").strip().lower()
    return mode if mode in ENVELOPE_MODES else "p10_p90"


def _parse_dc_range_y_mode(raw, *, default: str = "real") -> str:
    mode = (raw or default).strip().lower()
    return mode if mode in RANGE_Y_MODES else default


def _epa_kwh_per_100km_for_hashed_vin(hashed_vin) -> float | None:
    """EPA energy intensity (kWh/100 km) from catalog range + pack estimate."""
    from matesla.BatteryDegradation import pack_kwh_for_vehicle
    from matesla.models.TeslaCarInfo import TeslaCarInfo

    info = TeslaCarInfo.objects.filter(hashedVin=hashed_vin).first()
    try:
        epa_miles = float(info.EPARange) if info and info.EPARange else None
    except (TypeError, ValueError):
        epa_miles = None
    if not epa_miles or epa_miles < 50:
        return None
    pack_kwh = pack_kwh_for_vehicle(
        hashed_vin=hashed_vin,
        vin=getattr(info, "vin", None),
        epa_range_miles=epa_miles,
    )
    epa_km = epa_miles * 1.609344
    if epa_km <= 0 or not pack_kwh or pack_kwh <= 0:
        return None
    return pack_kwh / epa_km * 100.0


def _vehicle_drive_kwh_per_100km(hashed_vin, *, weeks=52) -> float | None:
    """
    Distance-weighted real driving consumption (kWh/100 km) over ``weeks``.

    Legacy helper (simple rolling window). Prefer
    ``_seasonal_drive_kwh_per_100km`` for range charts.
    """
    trips = _load_ranked_drives(
        hashed_vin,
        weeks,
        min_km=max(float(EFFICIENCY_MIN_KM), 10.0),
        max_trips=5000,
    )
    total_kwh = 0.0
    total_km = 0.0
    for trip in trips:
        kwh = trip.get("kwh_used")
        km = trip.get("km")
        k100 = trip.get("kwh_per_100km")
        if kwh is None or km is None or k100 is None:
            continue
        try:
            kwh_f = float(kwh)
            km_f = float(km)
            k100_f = float(k100)
        except (TypeError, ValueError):
            continue
        if km_f < 10.0 or k100_f < 5.0 or k100_f > 45.0:
            continue
        total_kwh += kwh_f
        total_km += km_f
    if total_km < 50.0:
        return None
    return total_kwh / total_km * 100.0


def _dc_session_start_times(hashed_vin, weeks: int) -> list:
    """Sorted start datetimes of DC sessions (for SC-approach trip filter)."""
    cache_key = f"dc_starts_v1:{hashed_vin}:{int(weeks) if weeks else 0}"
    try:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    except Exception:
        pass
    queryset = _dc_charge_period_queryset(hashed_vin, weeks)
    sessions = load_dc_sessions(queryset)
    starts = sorted(
        session.points[0].t for session in sessions if session.points
    )
    try:
        cache.set(cache_key, starts, GRAPH_PNG_CACHE_SECONDS)
    except Exception:
        pass
    return starts


def _seasonal_drive_kwh_per_100km(hashed_vin) -> dict | None:
    """
    Seasonal kWh/100 km: same calendar season last year (±30 d), then −2y,
    then last ≤3 months. Excludes Supercharger-approach legs (preconditioning).
    """
    today = timezone.localdate() if hasattr(timezone, "localdate") else date.today()
    try:
        from zoneinfo import ZoneInfo as _Z

        today = datetime.now(_Z("Europe/Brussels")).date()
    except Exception:
        today = date.today()

    cache_key = f"seasonal_kwh100_v1:{hashed_vin}:{today.isoformat()}"
    try:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    except Exception:
        pass

    trips = _load_ranked_drives(
        hashed_vin,
        SEASONAL_LOOKBACK_WEEKS,
        min_km=SEASONAL_TRIP_MIN_KM,
        max_trips=8000,
    )
    dc_starts = _dc_session_start_times(hashed_vin, SEASONAL_LOOKBACK_WEEKS)
    result = select_seasonal_kwh_per_100km(
        trips, dc_starts_sorted=dc_starts, today=today
    )
    try:
        cache.set(cache_key, result, GRAPH_PNG_CACHE_SECONDS)
    except Exception:
        pass
    return result


def _full_rated_miles_for_hashed_vin(hashed_vin) -> float | None:
    """Current implied 100% rated range (miles), accounts for degradation."""
    from matesla.models.TeslaCarInfo import TeslaCarInfo
    from matesla.soc_refine import estimate_pack_rated_miles

    info = TeslaCarInfo.objects.filter(hashedVin=hashed_vin).first()
    vin = getattr(info, "vin", None) if info else None
    if not vin:
        # Fall back to any snapshot VIN
        row = (
            TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin)
            .exclude(vin__isnull=True)
            .exclude(vin="")
            .values_list("vin", flat=True)
            .first()
        )
        vin = row
    if not vin:
        try:
            epa = float(info.EPARange) if info and info.EPARange else None
        except (TypeError, ValueError):
            epa = None
        return epa if epa and epa >= 50 else None
    pack = estimate_pack_rated_miles(vin)
    if pack is not None and pack >= 50:
        return float(pack)
    try:
        epa = float(info.EPARange) if info and info.EPARange else None
    except (TypeError, ValueError):
        epa = None
    return epa if epa and epa >= 50 else None


def _seasonal_source_label(source: str | None) -> str:
    if source == "yoy_1":
        return _("same season last year")
    if source == "yoy_2":
        return _("same season two years ago")
    if source == "recent_3m":
        return _("last three months (short history)")
    return _("recent driving")


def _dc_charge_period_queryset(hashed_vin, weeks: int):
    base = TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin)
    return _period_filter(base, weeks)


def _load_dc_analysis(hashed_vin, weeks: int, outlier_mode: str):
    """Load DC sessions, filter outliers, build curve payloads + summary."""
    cache_key = (
        f"dc_charge_v5:{hashed_vin}:{weeks}:{outlier_mode}:{get_language() or 'en'}"
    )
    try:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    except Exception:
        pass

    queryset = _dc_charge_period_queryset(hashed_vin, weeks)
    all_sessions = load_dc_sessions(queryset)
    kept, rejected = filter_outlier_sessions(all_sessions, mode=outlier_mode)
    power_curve = power_vs_soc_curve(kept)
    time_curves = soc_vs_time_curves(kept)
    summary = summarize_sessions(kept, rejected)
    # Extremes without range rates; DCCharge re-annotates with EPA/real conso.
    payload = {
        "power_curve": power_curve,
        "time_curves": time_curves,
        "power_extremes": power_curve_extreme_rows(power_curve),
        "summary": summary,
    }
    try:
        cache.set(cache_key, payload, GRAPH_PNG_CACHE_SECONDS)
    except Exception:
        pass
    return payload


def GenerateDcPowerVsSocGraph(power_curve, title, *, envelope_mode, size="full"):
    """Median charger power vs SoC with P10–P90 or min/max band."""
    figure, style_config = make_figure(size)
    axes = figure.add_subplot(111)
    if not power_curve:
        axes.text(
            0.5,
            0.5,
            _("No DC charge samples in this period"),
            ha="center",
            va="center",
            color=MUTED,
            fontsize=style_config["label_size"],
            transform=axes.transAxes,
        )
        finish_figure(figure, axes, title, style_config)
        return render_png(figure, size)

    soc = [row["soc"] for row in power_curve]
    med = [row["median"] for row in power_curve]
    if envelope_mode == "min_max":
        lo = [row["min"] for row in power_curve]
        hi = [row["max"] for row in power_curve]
        band_label = _("Min–max")
    else:
        lo = [row["p10"] for row in power_curve]
        hi = [row["p90"] for row in power_curve]
        band_label = _("P10–P90")

    axes.fill_between(soc, lo, hi, color=ACCENT, alpha=0.22, linewidth=0, label=band_label)
    axes.plot(
        soc,
        med,
        color=ACCENT,
        linewidth=style_config["linewidth"],
        marker="o",
        markersize=style_config["markersize"],
        label=_("Median kW"),
    )
    axes.set_xlabel(_("Battery SoC (%)"))
    axes.set_ylabel(_("Charger power (kW)"))
    axes.set_xlim(0, 100)
    axes.set_ylim(bottom=0)
    style_legend(axes, style_config)
    finish_figure(figure, axes, title, style_config)
    return render_png(figure, size)


def GenerateDcSocVsTimeGraph(time_curves, title, size="full"):
    """Median SoC vs minutes since plug-in; one curve per start-SoC bucket."""
    figure, style_config = make_figure(size)
    axes = figure.add_subplot(111)
    if not time_curves:
        axes.text(
            0.5,
            0.5,
            _("Not enough DC sessions for start-SoC curves"),
            ha="center",
            va="center",
            color=MUTED,
            fontsize=style_config["label_size"],
            transform=axes.transAxes,
        )
        finish_figure(figure, axes, title, style_config)
        return render_png(figure, size)

    # Distinct colors for up to 5 start buckets
    palette = (ACCENT, WARM, ENERGY, CYAN, DANGER)
    for index, bucket in enumerate(sorted(time_curves.keys())):
        series = time_curves[bucket]
        color = palette[index % len(palette)]
        label = _("%(start)s%% start · n=%(n)s") % {
            "start": bucket,
            "n": series["n_sessions"],
        }
        axes.plot(
            series["times"],
            series["soc_median"],
            color=color,
            linewidth=style_config["linewidth"],
            label=label,
        )
    axes.set_xlabel(_("Minutes since charge start"))
    axes.set_ylabel(_("Battery SoC (%)"))
    axes.set_ylim(0, 100)
    axes.set_xlim(left=0)
    style_legend(axes, style_config)
    finish_figure(figure, axes, title, style_config)
    return render_png(figure, size)


def GenerateDcRangeVsTimeGraph(
    range_curves,
    title,
    *,
    distance_label: str,
    size="full",
    empty_message=None,
):
    """
    Median estimated range vs minutes since plug-in; same start-SoC palette
    as SoC-vs-time. Y is already in display units (km or mi).
    """
    figure, style_config = make_figure(size)
    axes = figure.add_subplot(111)
    if not range_curves:
        axes.text(
            0.5,
            0.5,
            empty_message or _("Not enough data for range-vs-time curves"),
            ha="center",
            va="center",
            color=MUTED,
            fontsize=style_config["label_size"],
            transform=axes.transAxes,
        )
        finish_figure(figure, axes, title, style_config)
        return render_png(figure, size)

    palette = (ACCENT, WARM, ENERGY, CYAN, DANGER)
    y_max = 0.0
    for index, bucket in enumerate(sorted(range_curves.keys())):
        series = range_curves[bucket]
        color = palette[index % len(palette)]
        label = _("%(start)s%% start · n=%(n)s") % {
            "start": bucket,
            "n": series.get("n_sessions", 0),
        }
        ys = series.get("range_median") or []
        if ys:
            y_max = max(y_max, max(ys))
        axes.plot(
            series["times"],
            ys,
            color=color,
            linewidth=style_config["linewidth"],
            label=label,
        )
    axes.set_xlabel(_("Minutes since charge start"))
    axes.set_ylabel(
        _("Estimated range (%(u)s)") % {"u": distance_label}
    )
    axes.set_ylim(bottom=0, top=max(y_max * 1.08, 1.0) if y_max > 0 else None)
    axes.set_xlim(left=0)
    style_legend(axes, style_config)
    finish_figure(figure, axes, title, style_config)
    return render_png(figure, size)


def _day_map_raw_rows(hashed_vin, chosen_day):
    """Load one civil day of snapshot rows (same shape as DayMap)."""
    day_start = datetime(
        chosen_day.year, chosen_day.month, chosen_day.day, 0, 0, 0, tzinfo=DAY_MAP_TZ
    )
    day_end = day_start + timedelta(days=1)
    queryset = (
        TeslaCarDataSnapshot.objects.filter(
            hashedVin=hashed_vin,
            Date__gte=day_start,
            Date__lt=day_end,
        )
        .order_by("Date")
        .only(
            "Date",
            "battery_level",
            "usable_battery_level",
            "charging_state",
            "charger_power",
            "charger_actual_current",
            "charger_voltage",
            "charger_phases",
            "shift_state",
            "speed",
        )
    )
    raw_rows = []
    for sample in queryset.iterator(chunk_size=2000):
        raw_rows.append(
            {
                "t": sample.Date,
                "battery_level": float(sample.battery_level)
                if sample.battery_level is not None
                else None,
                "usable_battery_level": float(sample.usable_battery_level)
                if sample.usable_battery_level is not None
                else None,
                "charging_state": sample.charging_state,
                "charger_power": float(sample.charger_power)
                if sample.charger_power is not None
                else None,
                "charger_actual_current": float(sample.charger_actual_current)
                if sample.charger_actual_current is not None
                else None,
                "charger_voltage": float(sample.charger_voltage)
                if sample.charger_voltage is not None
                else None,
                "charger_phases": float(sample.charger_phases)
                if sample.charger_phases is not None
                else None,
                "shift_state": sample.shift_state,
                "speed": float(sample.speed) if sample.speed is not None else None,
            }
        )
    return raw_rows


def _find_charge_session_points(raw_rows, start_ts: int, *, tol_s: int = 180):
    """
    Return samples for the day-map charge group whose start is near start_ts.

    Matching uses the same kind-grouping as _segment_day so the curve opens
    on the same stop the table row describes.
    """
    if not raw_rows or start_ts is None:
        return None
    try:
        target = int(start_ts)
    except (TypeError, ValueError):
        return None

    groups = []
    current_kind = _point_kind(raw_rows[0])
    current_points = [raw_rows[0]]
    for sample in raw_rows[1:]:
        kind = _point_kind(sample)
        if kind == current_kind:
            current_points.append(sample)
        else:
            groups.append((current_kind, current_points))
            current_kind = kind
            current_points = [sample]
    groups.append((current_kind, current_points))

    best = None
    best_delta = None
    for kind, points in groups:
        if kind != "charge" or not points:
            continue
        start_t = points[0].get("t")
        if start_t is None:
            continue
        try:
            delta = abs(int(start_t.timestamp()) - target)
        except Exception:
            continue
        if delta > tol_s:
            continue
        if best_delta is None or delta < best_delta:
            best = points
            best_delta = delta
    return best


DAY_CHARGE_SESSION_CHARTS = frozenset({"power_vs_time", "power_vs_soc"})


def GenerateDayChargeSessionGraph(series, title, *, chart: str, size="full"):
    """
    One PNG for a single fast-charge stop.

    chart:
      - power_vs_time → kW vs minutes since start
      - power_vs_soc  → kW vs battery SoC
    Separate files so each curve can be saved or printed alone.
    """
    figure, style_config = make_figure(size)
    axes = figure.add_subplot(111)
    empty_msg = _("No charge samples for this stop")

    if not series:
        axes.text(
            0.5,
            0.5,
            empty_msg,
            ha="center",
            va="center",
            color=MUTED,
            fontsize=style_config["label_size"],
            transform=axes.transAxes,
        )
        finish_figure(figure, axes, title, style_config)
        return render_png(figure, size)

    powers = [row["power_kw"] for row in series]
    if chart == "power_vs_soc":
        by_soc = sorted(
            ((row["soc"], row["power_kw"]) for row in series),
            key=lambda pair: pair[0],
        )
        axes.plot(
            [pair[0] for pair in by_soc],
            [pair[1] for pair in by_soc],
            color=ENERGY,
            linewidth=style_config["linewidth"],
            marker="o",
            markersize=style_config["markersize"],
        )
        axes.set_xlabel(_("Battery SoC (%)"))
        axes.set_xlim(0, 100)
    else:
        times = [row["elapsed_min"] for row in series]
        axes.plot(
            times,
            powers,
            color=ACCENT,
            linewidth=style_config["linewidth"],
            marker="o",
            markersize=style_config["markersize"],
        )
        axes.set_xlabel(_("Minutes since charge start"))
        axes.set_xlim(left=0)

    axes.set_ylabel(_("Charger power (kW)"))
    axes.set_ylim(bottom=0)
    finish_figure(figure, axes, title, style_config)
    return render_png(figure, size)


@require_GET
def DayChargeSessionGraph(request, hashedVin, day, start_ts, chart):
    """
    PNG for one axis of a fast-charge stop: power_vs_time | power_vs_soc.

    Linked from the day map for DC-ish charging stops (peak ≥ ~40 kW).
    Two separate PNGs so each can be saved or printed alone.
    """
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="raw")
    if denied:
        return denied

    chart_key = (chart or "").strip().lower()
    if chart_key not in DAY_CHARGE_SESSION_CHARTS:
        return HttpResponseNotFound("Unknown charge session chart " + (chart or ""))

    chosen = _parse_day_string(day)
    if chosen is None:
        return HttpResponseNotFound("Invalid day")

    try:
        start_ts_int = int(start_ts)
    except (TypeError, ValueError):
        return HttpResponseNotFound("Invalid start time")

    size = graph_size_from_request(request)
    cache_key = _graph_png_cache_key(
        hashedVin,
        f"day_charge_session_v2_{chart_key}_{chosen.isoformat()}_{start_ts_int}",
        0,
        size,
        kind="day_charge_session",
    )
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None:
        return _png_response_from_bytes(hit, size, cache_status="HIT")

    raw_rows = _day_map_raw_rows(hashedVin, chosen)
    points = _find_charge_session_points(raw_rows, start_ts_int)
    series = charge_session_curve_series(points or [])
    day_label = chosen.strftime("%d/%m/%Y")
    if chart_key == "power_vs_soc":
        title = _("Charge power vs SoC — %(day)s") % {"day": day_label}
    else:
        title = _("Charge power vs time — %(day)s") % {"day": day_label}
    response = GenerateDayChargeSessionGraph(
        series, title, chart=chart_key, size=size
    )
    return _cache_graph_png(cache_key, response, size)


@require_GET
def DCCharge(request, hashedVin):
    """
    DC fast-charge profiles: power vs SoC (with envelope), SoC vs time by
    arrival SoC, and range vs time (rated after degradation / real seasonal
    consumption). Outlier filter targets V2 sharing and cold crawls.
    """
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied

    from matesla.units import get_distance_unit, unit_labels

    weeks = resolve_stats_period(request)
    outlier_mode = _parse_dc_outlier_mode(request.GET.get("filter"))
    envelope_mode = _parse_dc_envelope_mode(request.GET.get("envelope"))

    analysis = _load_dc_analysis(hashedVin, weeks, outlier_mode)
    summary = analysis["summary"]
    # Day-map drill-down only useful when the envelope is the actual min/max
    # (P10–P90 is a statistical band, not a single sample day).
    epa_kwh_100 = _epa_kwh_per_100km_for_hashed_vin(hashedVin)
    seasonal = _seasonal_drive_kwh_per_100km(hashedVin)
    real_kwh_100 = seasonal["kwh_per_100km"] if seasonal else None
    full_rated_mi = _full_rated_miles_for_hashed_vin(hashedVin)
    has_rated_range = full_rated_mi is not None and full_rated_mi >= 50
    has_real_range = (
        has_rated_range
        and real_kwh_100 is not None
        and full_real_range_km(
            full_rated_miles=full_rated_mi,
            real_kwh_per_100km=real_kwh_100,
            epa_kwh_per_100km=epa_kwh_100,
        )
        is not None
    )
    # Default: real (seasonal conso) when available, else rated.
    default_range_mode = "real" if has_real_range else "rated"
    range_y_mode = _parse_dc_range_y_mode(
        request.GET.get("range_mode"), default=default_range_mode
    )
    if range_y_mode == "real" and not has_real_range:
        range_y_mode = "rated"
    if range_y_mode == "rated" and not has_rated_range and has_real_range:
        range_y_mode = "real"

    if envelope_mode == "min_max" and analysis.get("power_curve"):
        power_extremes = power_curve_extreme_rows(
            analysis["power_curve"],
            epa_kwh_per_100km=epa_kwh_100,
            real_kwh_per_100km=real_kwh_100,
        )
    else:
        power_extremes = []

    unit = get_distance_unit(request)
    labels = unit_labels(unit)
    seasonal_note = None
    if seasonal:
        start = seasonal["window_start"]
        end = seasonal["window_end"]
        seasonal_note = {
            "source": seasonal["source"],
            "source_label": _seasonal_source_label(seasonal["source"]),
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
            "window_start_display": start.strftime("%d/%m/%Y") if start else "—",
            "window_end_display": end.strftime("%d/%m/%Y") if end else "—",
            "total_km": seasonal.get("total_km"),
            "n_trips": seasonal.get("n_trips"),
            "kwh_per_100km": seasonal.get("kwh_per_100km"),
        }

    context = _vehicle_chrome_context(request, hashedVin)
    context.update(
        {
            "stats_period": weeks,
            "outlier_mode": outlier_mode,
            "envelope_mode": envelope_mode,
            "outlier_mode_label": outlier_mode_label(outlier_mode),
            "envelope_mode_label": envelope_mode_label(envelope_mode),
            "summary": summary,
            "has_power_curve": bool(analysis["power_curve"]),
            "has_time_curves": bool(analysis["time_curves"]),
            "has_range_curves": bool(analysis["time_curves"])
            and (has_rated_range or has_real_range),
            "has_rated_range": has_rated_range,
            "has_real_range": has_real_range,
            "range_y_mode": range_y_mode,
            "range_y_mode_label": range_y_mode_label(range_y_mode),
            "range_mode_choices": [
                {
                    "key": "real",
                    "label": range_y_mode_label("real"),
                    "available": has_real_range,
                },
                {
                    "key": "rated",
                    "label": range_y_mode_label("rated"),
                    "available": has_rated_range,
                },
            ],
            "power_extremes": power_extremes,
            "show_power_extremes": bool(power_extremes),
            "epa_kwh_per_100km": epa_kwh_100,
            "real_kwh_per_100km": real_kwh_100,
            "seasonal_conso": seasonal_note,
            "full_rated_miles": full_rated_mi,
            "distance_unit": unit,
            "u_dist": labels["distance"],
            "u_energy": labels["energy"],
            "u_range_rate": labels["range_rate"],
            "filter_choices": [
                {"key": "robust", "label": outlier_mode_label("robust")},
                {"key": "all", "label": outlier_mode_label("all")},
            ],
            "envelope_choices": [
                {"key": "p10_p90", "label": envelope_mode_label("p10_p90")},
                {"key": "min_max", "label": envelope_mode_label("min_max")},
            ],
        }
    )
    return render(request, "personalstats/dc_charge.html", context)


DC_CHARGE_CHARTS = frozenset(
    {"power_vs_soc", "soc_vs_time", "range_vs_time_real", "range_vs_time_rated"}
)


@require_GET
def DCChargeGraph(request, hashedVin, chart, desiredperiod):
    """PNG for DC charge charts: power_vs_soc | soc_vs_time | range_vs_time_*."""
    denied = _unknown_hashed_vin_response(request, hashedVin, kind="raw")
    if denied:
        return denied
    chart_key = (chart or "").strip().lower()
    if chart_key not in DC_CHARGE_CHARTS:
        return HttpResponseNotFound("Unknown DC chart " + (chart or ""))

    try:
        weeks = int(desiredperiod)
    except (TypeError, ValueError):
        weeks = 520
    if weeks < 0:
        weeks = 0

    from matesla.BatteryDegradation import pack_kwh_for_vehicle
    from matesla.units import get_distance_unit, km_to_display, miles_to_display, unit_labels

    outlier_mode = _parse_dc_outlier_mode(request.GET.get("filter"))
    envelope_mode = _parse_dc_envelope_mode(request.GET.get("envelope"))
    size = graph_size_from_request(request)
    unit = get_distance_unit(request)
    dist_label = unit_labels(unit)["distance"]

    cache_key = _graph_png_cache_key(
        hashedVin,
        f"dc_v7_{chart_key}_{outlier_mode}_{envelope_mode}",
        weeks,
        size,
        kind="dc_charge",
        unit=unit,
    )
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None:
        return _png_response_from_bytes(hit, size, cache_status="HIT")

    analysis = _load_dc_analysis(hashedVin, weeks, outlier_mode)
    if chart_key == "power_vs_soc":
        title = _("DC charge power vs battery SoC")
        response = GenerateDcPowerVsSocGraph(
            analysis["power_curve"],
            title,
            envelope_mode=envelope_mode,
            size=size,
        )
    elif chart_key == "soc_vs_time":
        title = _("DC charge: SoC vs time by start SoC")
        response = GenerateDcSocVsTimeGraph(
            analysis["time_curves"],
            title,
            size=size,
        )
    else:
        # range_vs_time_real | range_vs_time_rated
        full_rated_mi = _full_rated_miles_for_hashed_vin(hashedVin)
        if chart_key == "range_vs_time_rated":
            full_display = miles_to_display(full_rated_mi, unit)
            title = _("DC charge: rated range vs time")
            empty = _("Rated full range unavailable for this vehicle")
        else:
            seasonal = _seasonal_drive_kwh_per_100km(hashedVin)
            real_kwh = seasonal["kwh_per_100km"] if seasonal else None
            epa_kwh = _epa_kwh_per_100km_for_hashed_vin(hashedVin)
            pack = pack_kwh_for_vehicle(hashed_vin=hashedVin)
            full_real_km_val = full_real_range_km(
                full_rated_miles=full_rated_mi,
                real_kwh_per_100km=real_kwh,
                epa_kwh_per_100km=epa_kwh,
                pack_kwh=pack,
            )
            full_display = km_to_display(full_real_km_val, unit)
            title = _("DC charge: driving range vs time")
            empty = _("Not enough seasonal driving data for real range")

        if full_display is None or full_display <= 0 or not analysis["time_curves"]:
            range_curves = {}
        else:
            range_curves = scale_soc_time_curves_to_range(
                analysis["time_curves"], full_display
            )
        response = GenerateDcRangeVsTimeGraph(
            range_curves,
            title,
            distance_label=dist_label,
            size=size,
            empty_message=empty,
        )
    return _cache_graph_png(cache_key, response, size)


@require_GET
def PollDetails(request, hashedVin):
    """
    Adaptive Fleet polling diagnostics: current interval, habit trust, and
    typical-week idle grid. Complements the fleet poll cost graph (counts only).
    """
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied

    from matesla.poll_diagnostics import (
        build_poll_diagnostic_report,
        resolve_vehicle_for_hashed_vin,
        resolve_vin_for_hashed_vin,
        weekday_short_label,
    )
    from matesla.poll_habits import NIGHT_HOURS

    vehicle = resolve_vehicle_for_hashed_vin(hashedVin)
    vin = resolve_vin_for_hashed_vin(hashedVin)
    report = build_poll_diagnostic_report(
        vehicle=vehicle,
        vin=vin,
        hashed_vin=hashedVin,
        force_recompute=False,
        forecast_days=0,
    )

    # Reshape week grid for the template: rows = hours 0–23, columns = Mon–Sun.
    grid_by_hour = []
    cells_by_key = {
        (cell.isoweekday, cell.hour): cell for cell in report.week_grid
    }
    for hour in range(24):
        row_cells = []
        for isoweekday in range(1, 8):
            row_cells.append(cells_by_key.get((isoweekday, hour)))
        grid_by_hour.append({"hour": hour, "cells": row_cells})

    weekday_headers = [
        {"isoweekday": day, "label": weekday_short_label(day)}
        for day in range(1, 8)
    ]

    context = _vehicle_chrome_context(request, hashedVin)
    context.update(
        {
            "report": report,
            "habits": report.habits,
            "current": report.current,
            "grid_by_hour": grid_by_hour,
            "weekday_headers": weekday_headers,
            "night_hours": set(NIGHT_HOURS),
            "notes": report.notes,
        }
    )
    return render(request, "personalstats/poll_details.html", context)
