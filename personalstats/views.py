import django
from django.db.models import Max, Min, Avg, F, FloatField, Case, When
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.template import loader
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
from datetime import timedelta, datetime

# Create your views here.

# Return a dictionary with titles for fields
from personalstats.tables import TeslaFirmwareHistoryTable

# Graph keys that are not real DB columns (computed in the view)
COMPUTED_GRAPH_FIELDS = frozenset({"range_at_100", "range_at_100_odometer"})


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

# allow to disable cache when improving HTML and you want a constant reload
# @never_cache
def Stats(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    template = loader.get_template('personalstats/carstats.html')
    context = {"hashedVin": hashedVin}
    context.update(GetTitleForFieldDico())
    # Multi-vehicle selector when user is logged in
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
    return HttpResponse(template.render(context, request))


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
