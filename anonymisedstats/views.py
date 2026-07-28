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

# in a general way, I eject cars not seen for maxdaysinthepast days
# for period test: https://stackoverflow.com/questions/1984047/django-filter-older-than-days
# cars should be refreshed at least once a day, ok, they can be unreachable
# (no network), but anyway it should be short
maxdaysinthepast = 15


def GeneratePngFromGraph(fig, size="full"):
    # activate this when you want performance analysis
    # return HttpResponse("<html><body>todo activate this only for performance test of graphs</body></html>")
    return render_png(fig, size=size)


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
    # Figure (not pyplot) for web-server safety:
    # https://matplotlib.org/stable/users/explain/figure/backends.html#matplotlib-in-a-web-application-server
    fig, cfg = make_figure(size, bar=True)
    ax = fig.subplots(nrows=1, ncols=1, sharey=True)
    if names is not None and values is not None:
        ax.bar(
            names,
            values,
            color=BAR_FACE,
            edgecolor=SCATTER_EDGE,
            linewidth=0.4,
            alpha=0.92,
        )
        for label in ax.get_xticklabels():
            label.set_rotation(25)
            label.set_ha("right")
    finish_figure(fig, ax, title, cfg)
    return GeneratePngFromGraph(fig, size=size)


def GetNamesAndValuesFromGroupByTotalResult(results, desiredfield):
    names = list()
    values = list()
    for entry in results:
        if entry[desiredfield] is None:
            names.append(str(_("No Value")))
        else:
            if type(entry[desiredfield]) == type(True):
                if entry[desiredfield] is True:
                    names.append(str(_("True")))
                else:
                    names.append(str(_("False")))
            else:
                name = str(entry[desiredfield])
                # if large (ie firmware), keep first word
                if len(name) > 5:
                    name = name.split()[0]
                names.append(name)
        values.append(entry['total'])
    return names, values


def FirmwareUpdates(request):
    # query 10 most recent versions to not have an unreadable graph
    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaFirmwareHistory.objects.filter(
        vin__in=TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).values('vin')).filter(
        IsArchive=False).values(
        'Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, 'Version')
    return GenerateBarGraph(names, values, _('Most recent Firmware updates'), size=size)


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
    # Check that it is one field from the TeslaCarInfo
    validFields = TeslaCarInfo.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield)

    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).filter(car_type=CarModel).values(
        desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, GetTitleForField(desiredfield), size=size)


def StatsOnCarAllModelsGraph(request, desiredfield):
    # Check that it is one field from the TeslaCarInfo
    validFields = TeslaCarInfo.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield)

    size = graph_size_from_request(request)
    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).values(desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, GetTitleForField(desiredfield), size=size)


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
    """Prefer usable_battery_level, else battery_level."""
    if not isinstance(entry, dict):
        entry = entry.__dict__
    u = entry.get("usable_battery_level")
    if u is not None:
        try:
            return float(u)
        except (TypeError, ValueError):
            return None
    b = entry.get("battery_level")
    if b is None:
        return None
    try:
        return float(b)
    except (TypeError, ValueError):
        return None


def high_soc_scatter_q(min_soc: float = DEGRADATION_SCATTER_MIN_SOC):
    """ORM filter: high SoC and not actively charging."""
    from django.db.models import Q

    soc_q = Q(usable_battery_level__gte=min_soc) | (
        Q(usable_battery_level__isnull=True) & Q(battery_level__gte=min_soc)
    )
    # While Charging, battery_range / SoC coupling is unstable → thick vertical noise.
    return soc_q & ~Q(charging_state="Charging")


def degradation_scatter_queryset(qs):
    """Prefer strict high-SoC filter; fall back if too few points."""
    strict = qs.filter(high_soc_scatter_q())
    # exists()/[:N] avoids full count on huge tables
    if strict[: DEGRADATION_SCATTER_MIN_POINTS].count() >= DEGRADATION_SCATTER_MIN_POINTS:
        return strict
    return qs.filter(high_soc_scatter_q(min_soc=DEGRADATION_SCATTER_FALLBACK_SOC))


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def aggregate_scatter_daily_median(xvalues, yvalues, dates):
    """
    One point per civil day (median X, median Y).

    TeslaFi-style histories have many snapshots/day; the vertical cloud is mostly
    same-day BMS jitter, not real degradation change.
    """
    if not dates or len(dates) != len(xvalues) or len(xvalues) == 0:
        return xvalues, yvalues
    from collections import defaultdict

    buckets = defaultdict(lambda: ([], []))
    for d, x, y in zip(dates, xvalues, yvalues):
        if d is None or x is None or y is None:
            continue
        day = d.date() if hasattr(d, "date") else d
        try:
            buckets[day][0].append(float(x))
            buckets[day][1].append(float(y))
        except (TypeError, ValueError):
            continue
    if not buckets:
        return xvalues, yvalues
    out_x, out_y = [], []
    for day in sorted(buckets.keys()):
        xs, ys = buckets[day]
        mx, my = _median(xs), _median(ys)
        if mx is None or my is None:
            continue
        out_x.append(mx)
        out_y.append(my)
    return out_x, out_y


def GetXandYFromBatteryDegradResult(results, xfield, *, daily_median=True, min_soc=None):
    # Prefer caller/ORM filter; default floor is fallback so soft queryset is not re-killed.
    if min_soc is None:
        min_soc = DEGRADATION_SCATTER_FALLBACK_SOC
    xvalues = list()
    yvalues = list()
    dates = list()
    for entry in results:
        raw = entry if isinstance(entry, dict) else entry.__dict__
        if raw.get("battery_degradation") is None:
            continue
        if raw.get(xfield) is None:
            continue
        soc = _soc_for_scatter(raw)
        if soc is None or soc < min_soc:
            continue
        # Skip active charging even if caller passed unfiltered rows
        cs = raw.get("charging_state")
        if cs == "Charging":
            continue
        xvalues.append(raw[xfield])
        yvalues.append(raw["battery_degradation"])
        dates.append(raw.get("Date"))
    if daily_median:
        return aggregate_scatter_daily_median(xvalues, yvalues, dates)
    return xvalues, yvalues


# I want 2 decimals, and scientific notation...when it has a meaning, because
# 10 exponant 0 is not very helpfull
def FormatDouble2Decimals(d):
    return "{:.2e}".format(d).replace("e+00", "")


def GenerateScatterGraph(xvalues, yvalues, title, size="full"):
    # From https://matplotlib.org/3.2.1/api/_as_gen/matplotlib.pyplot.scatter.html
    fig, cfg = make_figure(size)
    ax = fig.subplots()
    if xvalues is not None and yvalues is not None and len(xvalues) > 0:
        ax.scatter(
            xvalues,
            yvalues,
            s=cfg["scatter_size"],
            c=SCATTER_FACE,
            alpha=0.45,
            edgecolors=SCATTER_EDGE,
            linewidths=0.25,
            zorder=2,
        )
        # Linear trend only (quadratic fits misled on long histories).
        if len(xvalues) >= 2:
            sortedx = list(dict.fromkeys(xvalues))
            sortedx.sort()
            xv = np.asarray(xvalues, dtype=float)
            yv = np.asarray(yvalues, dtype=float)
            p1 = np.polyfit(xv, yv, 1)
            f1 = np.poly1d(p1)
            y_hat = f1(xv)
            ss_res = float(np.sum((yv - y_hat) ** 2))
            ss_tot = float(np.sum((yv - np.mean(yv)) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
            # Slope as percentage points of degradation per 10_000 miles
            slope_per_10k = float(p1[0]) * 10000.0
            r2_txt = f"{r2:.2f}" if r2 == r2 else "—"  # NaN check
            # Legend: quality (R²) + practical slope; equation stays secondary
            label = (
                f"R²={r2_txt}  ·  {slope_per_10k:+.2f} pt/10k mi\n"
                f"{FormatDouble2Decimals(p1[0])}x+{FormatDouble2Decimals(p1[1])}"
            )
            ax.plot(
                sortedx,
                f1(sortedx),
                "-",
                color=FIT_LINEAR,
                linewidth=cfg["linewidth"],
                label=label,
                zorder=3,
            )
            style_legend(ax, cfg)
    finish_figure(fig, ax, title, cfg)
    return GeneratePngFromGraph(fig, size=size)


def BatteryDegradationGraph(request, desiredfield):
    # Similar (and reuse) wht is done for equivalent graph in personal graph,
    # but here we mix all car data

    # Check that it is one field from the TeslaCarDataSnapshot
    validFields = TeslaCarDataSnapshot.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield), False

    size = graph_size_from_request(request)
    count = TeslaCarDataSnapshot.objects.count()
    if count == 0:
        return GenerateBarGraph(None, None, GetTitleForField(desiredfield), size=size)

    # to have a random sample
    # see https://stackoverflow.com/questions/31801826/random-sample-on-django-querysets-how-will-sampling-on-querysets-affect-perform
    # but as data grows, it it faster to have a real random number in data
    # we are now at 6000 rows and it is 3 times faster, as using ?-->random()
    # needs to read all the table and sort it by random.
    # While using indexed randomNr just take the first rows of the index (yes, it uses the index).
    results = degradation_scatter_queryset(TeslaCarDataSnapshot.objects.all()).order_by(
        "randomNr"
    )[:800]
    # Fleet mix: no daily median (many cars/days); high-SoC filter is enough
    xvalues, yxvalues = GetXandYFromBatteryDegradResult(
        results, desiredfield, daily_median=False
    )
    return GenerateScatterGraph(xvalues, yxvalues, GetTitleForField(desiredfield), size=size)
