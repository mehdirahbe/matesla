import csv
from datetime import timedelta

import numpy as np
from django.contrib.auth import get_user
from django.db import connection
from django.db.models import Count, Max
from django.http import HttpResponse, HttpResponseNotFound
from django.template import loader
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache

from matesla.graphstyle import (
    BAR_FACE,
    FIT_LINEAR,
    SCATTER_EDGE,
    SCATTER_FACE,
    finish_figure,
    graph_size_from_request,
    make_figure,
    render_png,
    style_legend,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from mysite.settings import DATABASES

# Fleet-mix graphs only include cars seen recently enough. Cars not refreshed
# for longer than this are treated as left the active mix (may be offline briefly,
# but multi-week silence is enough to drop them from option/firmware charts).
MAX_DAYS_IN_THE_PAST = 15
# Back-compat alias for any import still using the 2020 name
maxdaysinthepast = MAX_DAYS_IN_THE_PAST


def GeneratePngFromGraph(figure, size="full"):
    """Encode a matplotlib figure as an HTTP PNG response."""
    return render_png(figure, size=size)


# Return a dictionary with titles for fields
def GetTitleForFieldDico():
    dico = {
        'Date': _('Car addition Date'),
        'car_type': _('Car type'),
        'charge_port_type': _('Charge port'),
        'exterior_color': _('Exterior color'),
        'has_air_suspension': _('Has air suspension'),
        'has_ludicrous_mode': _('Has ludicrous mode'),
        'motorized_charge_port': _('Is charge port motorized'),
        'rear_seat_heaters': _('Has rear seat heaters'),
        'rhd': _('Right hand drive'),
        'roof_color': _('Roof color'),
        'wheel_type': _('Wheel'),
        'sentry_mode_available': _('Is sentry mode available'),
        'smart_summon_available': _('Is FSD enabled'),
        'eu_vehicle': _('European union car'),
        'EPARange': _('EPA Range (miles)'),
        'isDualMotor': _('Is Dual Motor'),
        'modelYear': _('Car year'),
        'outside_temp': _('Outside temperature (°C)'),
        'odometer': _('Odometer (miles)'),
        'battery_level': _('Battery level (%)'),
        'charge_limit_soc': _('Battery charge limit (%)'),
        'NumberCycles': _('Estim. number of battery cycles'),
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


def GenerateBarGraph(names, values, title, size="full"):
    """
    Simple categorical bar chart (firmware versions, option frequencies, …).

    Uses Figure (not pyplot) so multi-threaded web workers stay safe:
    https://matplotlib.org/stable/users/explain/figure/backends.html
    """
    figure, style_config = make_figure(size, bar=True)
    axes = figure.subplots(nrows=1, ncols=1, sharey=True)
    if names is not None and values is not None:
        axes.bar(
            names,
            values,
            color=BAR_FACE,
            edgecolor=SCATTER_EDGE,
            linewidth=0.4,
            alpha=0.92,
        )
        for tick_label in axes.get_xticklabels():
            tick_label.set_rotation(25)
            tick_label.set_ha("right")
    finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)


def GetNamesAndValuesFromGroupByTotalResult(results, desired_field):
    """Turn ORM group-by rows into parallel name/value lists for bar charts."""
    names = []
    values = []
    for entry in results:
        field_value = entry[desired_field]
        if field_value is None:
            names.append(str(_("No Value")))
        elif isinstance(field_value, bool):
            names.append(str(_("True") if field_value else _("False")))
        else:
            label = str(field_value)
            # Firmware strings are long; keep the first token for axis readability
            if len(label) > 5:
                label = label.split()[0]
            names.append(label)
        values.append(entry["total"])
    return names, values


def FirmwareUpdates(request):
    """Bar chart of the 10 most recent firmware versions still seen in the fleet."""
    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=MAX_DAYS_IN_THE_PAST)
    results = (
        TeslaFirmwareHistory.objects.filter(
            vin__in=TeslaCarInfo.objects.filter(
                LastSeenDate__gte=time_threshold
            ).values("vin")
        )
        .filter(IsArchive=False)
        .values("Version")
        .annotate(MostRecent=Max("Date"))
        .annotate(total=Count("Version"))
        .order_by("-MostRecent")[:10]
    )
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, "Version")
    return GenerateBarGraph(
        names, values, _("Most recent Firmware updates"), size=size
    )


def FirmwareUpdatesAsCSV(request):
    # Same query as FirmwareUpdates but no top as here we can return all rows
    query = str(TeslaFirmwareHistory.objects.filter(IsArchive=False).values('Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent').query)
    # grr sql lite want 0 for false and generated query use false-->adapt
    if DATABASES['default']['ENGINE'].find('sqlite') >= 0:
        query = query.replace('False', '0')

    return PrepareCSVFromQuery(query)


def StatsOnCarByModelGraph(request, desiredfield, CarModel):
    """
    Option frequency histogram for one car_type (e.g. wheel options on Model 3).

    URL kwargs keep their historical names (desiredfield, CarModel) so reverse()
    and path converters stay stable.
    """
    desired_field = desiredfield
    car_model = CarModel
    valid_fields = TeslaCarInfo.__dict__
    if desired_field is None or desired_field not in valid_fields:
        return HttpResponseNotFound(
            "Graph for this field doesn't exists " + str(desired_field)
        )

    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=MAX_DAYS_IN_THE_PAST)
    results = (
        TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold)
        .filter(car_type=car_model)
        .values(desired_field)
        .annotate(total=Count(desired_field))
        .order_by(desired_field)[:10]
    )
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desired_field)
    return GenerateBarGraph(
        names, values, GetTitleForField(desired_field), size=size
    )


def StatsOnCarAllModelsGraph(request, desiredfield):
    """Option frequency histogram across all recently seen cars."""
    desired_field = desiredfield
    valid_fields = TeslaCarInfo.__dict__
    if desired_field is None or desired_field not in valid_fields:
        return HttpResponseNotFound(
            "Graph for this field doesn't exists " + str(desired_field)
        )

    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=MAX_DAYS_IN_THE_PAST)
    results = (
        TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold)
        .values(desired_field)
        .annotate(total=Count(desired_field))
        .order_by(desired_field)[:10]
    )
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desired_field)
    return GenerateBarGraph(
        names, values, GetTitleForField(desired_field), size=size
    )


@never_cache
def StatsChoicePage(request):
    context = {}
    context.update(GetTitleForFieldDico())

    return HttpResponse(loader.get_template('anonymisedstats/carstats.html').render(context, request))


def PrepareCSVFromQuery(query):
    # from https://docs.djangoproject.com/en/3.0/topics/db/sql/ for sql
    # from https://docs.djangoproject.com/en/3.0/howto/outputting-csv/ for csv

    # prepare csv response (browser should know that)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="AllRawCarInfos.csv"'
    writer = csv.writer(response)

    # connect to SQL
    with connection.cursor() as cursor:
        results = cursor.execute(query)

        # generate heading
        title = []
        for col in cursor.description:
            title.append(col[0])  # index 0 seems to be name
        writer.writerow(title)

        # then values
        for row in cursor.fetchall():
            values = []
            for field in row:
                values.append(str(field))
            writer.writerow(values)
        return response


# view for admin in order to download all car info
def GetAllRawCarInfos(request):
    user = get_user(request)
    if not user.is_authenticated or not user.is_superuser:
        return HttpResponse('Accessing all raw car infos is only for admins')
    return PrepareCSVFromQuery('select * from matesla_teslacarinfo;')


# Scatter fits are much noisier at low SoC (range extrapolation).
# Prefer ≥ 80 % (Tesla’s recommended daily charge limit); active charging excluded.
DEGRADATION_SCATTER_MIN_SOC = 80.0
# If a car rarely reaches 80 %, fall back so the graph is not empty.
DEGRADATION_SCATTER_FALLBACK_SOC = 75.0
DEGRADATION_SCATTER_MIN_POINTS = 15


def _soc_for_scatter(entry) -> float | None:
    """Prefer usable_battery_level, else battery_level, as float percent."""
    if not isinstance(entry, dict):
        entry = entry.__dict__
    usable = entry.get("usable_battery_level")
    if usable is not None:
        try:
            return float(usable)
        except (TypeError, ValueError):
            return None
    battery_level = entry.get("battery_level")
    if battery_level is None:
        return None
    try:
        return float(battery_level)
    except (TypeError, ValueError):
        return None


def high_soc_scatter_q(minimum_soc: float = DEGRADATION_SCATTER_MIN_SOC):
    """
    ORM filter: high SoC and not actively charging.

    Charging rows couple range and SoC poorly and create vertical noise clouds.
    """
    from django.db.models import Q

    high_soc = Q(usable_battery_level__gte=minimum_soc) | (
        Q(usable_battery_level__isnull=True) & Q(battery_level__gte=minimum_soc)
    )
    return high_soc & ~Q(charging_state="Charging")


def degradation_scatter_queryset(queryset):
    """Prefer strict high-SoC filter; fall back if too few points for a trend."""
    strict = queryset.filter(high_soc_scatter_q())
    # [:N].count() avoids a full table COUNT on huge histories
    if (
        strict[: DEGRADATION_SCATTER_MIN_POINTS].count()
        >= DEGRADATION_SCATTER_MIN_POINTS
    ):
        return strict
    return queryset.filter(
        high_soc_scatter_q(minimum_soc=DEGRADATION_SCATTER_FALLBACK_SOC)
    )


def _median(values):
    """Median of a non-empty numeric list (None if empty)."""
    sorted_values = sorted(values)
    count = len(sorted_values)
    if count == 0:
        return None
    middle = count // 2
    if count % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def aggregate_scatter_daily_median(x_values, y_values, sample_dates):
    """
    Collapse many same-day snapshots into one median (X, Y) point per civil day.

    TeslaFi-style histories poll often; the vertical cloud is mostly same-day
    BMS jitter, not real degradation change. Daily median makes the trend readable.
    """
    if (
        not sample_dates
        or len(sample_dates) != len(x_values)
        or len(x_values) == 0
    ):
        return x_values, y_values
    from collections import defaultdict

    # day -> (list of X, list of Y)
    per_day = defaultdict(lambda: ([], []))
    for sample_date, x_value, y_value in zip(sample_dates, x_values, y_values):
        if sample_date is None or x_value is None or y_value is None:
            continue
        civil_day = (
            sample_date.date() if hasattr(sample_date, "date") else sample_date
        )
        try:
            per_day[civil_day][0].append(float(x_value))
            per_day[civil_day][1].append(float(y_value))
        except (TypeError, ValueError):
            continue
    if not per_day:
        return x_values, y_values
    median_x_values, median_y_values = [], []
    for civil_day in sorted(per_day.keys()):
        day_x_values, day_y_values = per_day[civil_day]
        median_x = _median(day_x_values)
        median_y = _median(day_y_values)
        if median_x is None or median_y is None:
            continue
        median_x_values.append(median_x)
        median_y_values.append(median_y)
    return median_x_values, median_y_values


def GetXandYFromBatteryDegradResult(
    results, x_field, *, daily_median=True, minimum_soc=None
):
    """
    Extract scatter series from ORM rows: X = x_field, Y = battery_degradation.

    Filters high SoC and non-charging; optionally collapses to daily medians.
    """
    # Prefer caller/ORM filter; default floor is fallback so soft queryset is not re-killed
    if minimum_soc is None:
        minimum_soc = DEGRADATION_SCATTER_FALLBACK_SOC
    x_values = []
    y_values = []
    sample_dates = []
    for entry in results:
        raw = entry if isinstance(entry, dict) else entry.__dict__
        if raw.get("battery_degradation") is None:
            continue
        if raw.get(x_field) is None:
            continue
        state_of_charge = _soc_for_scatter(raw)
        if state_of_charge is None or state_of_charge < minimum_soc:
            continue
        # Skip active charging even if the caller passed unfiltered rows
        if raw.get("charging_state") == "Charging":
            continue
        x_values.append(raw[x_field])
        y_values.append(raw["battery_degradation"])
        sample_dates.append(raw.get("Date"))
    if daily_median:
        return aggregate_scatter_daily_median(x_values, y_values, sample_dates)
    return x_values, y_values


def FormatDouble2Decimals(value):
    """Two-decimal scientific notation, stripping useless e+00 exponents."""
    return "{:.2e}".format(value).replace("e+00", "")


def GenerateScatterGraph(x_values, y_values, title, size="full"):
    """
    Degradation scatter with a linear trend line (R² + slope per 10k miles).

    Quadratic fits misled on long histories, so we only show a linear polyfit.
    """
    figure, style_config = make_figure(size)
    axes = figure.subplots()
    if x_values is not None and y_values is not None and len(x_values) > 0:
        axes.scatter(
            x_values,
            y_values,
            s=style_config["scatter_size"],
            c=SCATTER_FACE,
            alpha=0.45,
            edgecolors=SCATTER_EDGE,
            linewidths=0.25,
            zorder=2,
        )
        if len(x_values) >= 2:
            sorted_unique_x = list(dict.fromkeys(x_values))
            sorted_unique_x.sort()
            x_array = np.asarray(x_values, dtype=float)
            y_array = np.asarray(y_values, dtype=float)
            linear_coefficients = np.polyfit(x_array, y_array, 1)
            linear_polynomial = np.poly1d(linear_coefficients)
            y_predicted = linear_polynomial(x_array)
            residual_sum_squares = float(np.sum((y_array - y_predicted) ** 2))
            total_sum_squares = float(
                np.sum((y_array - np.mean(y_array)) ** 2)
            )
            r_squared = (
                1.0 - residual_sum_squares / total_sum_squares
                if total_sum_squares > 0
                else float("nan")
            )
            # Slope as percentage points of degradation per 10_000 miles
            slope_per_10k_miles = float(linear_coefficients[0]) * 10000.0
            r_squared_text = (
                f"{r_squared:.2f}" if r_squared == r_squared else "—"
            )
            trend_label = (
                f"R²={r_squared_text}  ·  {slope_per_10k_miles:+.2f} pt/10k mi\n"
                f"{FormatDouble2Decimals(linear_coefficients[0])}x+"
                f"{FormatDouble2Decimals(linear_coefficients[1])}"
            )
            axes.plot(
                sorted_unique_x,
                linear_polynomial(sorted_unique_x),
                "-",
                color=FIT_LINEAR,
                linewidth=style_config["linewidth"],
                label=trend_label,
                zorder=3,
            )
            style_legend(axes, style_config)
    finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)


def BatteryDegradationGraph(request, desiredfield):
    """
    Fleet-mix degradation scatter (all cars), X = desiredfield.

    URL kwarg name `desiredfield` is kept for stable reverse()/path matching.
    Samples via indexed randomNr instead of ORDER BY RANDOM() so large tables
    stay fast (first rows of the randomNr index).
    """
    desired_field = desiredfield
    valid_fields = TeslaCarDataSnapshot.__dict__
    if desired_field is None or desired_field not in valid_fields:
        return (
            HttpResponseNotFound(
                "Graph for this field doesn't exists " + str(desired_field)
            ),
            False,
        )

    size = graph_size_from_request(request)
    if TeslaCarDataSnapshot.objects.count() == 0:
        return GenerateBarGraph(
            None, None, GetTitleForField(desired_field), size=size
        )

    results = degradation_scatter_queryset(
        TeslaCarDataSnapshot.objects.all()
    ).order_by("randomNr")[:800]
    # Fleet mix: no daily median (many cars/days); high-SoC filter is enough
    x_values, y_values = GetXandYFromBatteryDegradResult(
        results, desired_field, daily_median=False
    )
    return GenerateScatterGraph(
        x_values, y_values, GetTitleForField(desired_field), size=size
    )
