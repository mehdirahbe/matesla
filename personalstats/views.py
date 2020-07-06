import io

import django
from django.db.models import Max, Min, Avg
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.template import loader
from django_tables2 import SingleTableView
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import (DateFormatter)
from matplotlib.figure import Figure

from anonymisedstats.views import PrepareCSVFromQuery, GetXandYFromBatteryDegradResult, GenerateScatterGraph, \
    GeneratePngFromGraph
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.VinHash import IsValidHash
from django.utils.translation import ugettext_lazy as _

# Create your views here.

# Return a dictionary with titles for fields
from personalstats.tables import TeslaFirmwareHistoryTable


def GetTitleForFieldDico():
    dico = {
        'outside_temp': _('Outside temperature (°C)'),
        'driver_temp_setting': _('Driver temperature (°C)'),
        'inside_temp': _('Inside temperature (°C)'),
        'passenger_temp_setting': _('Passenger temperature (°C)'),
        'odometer': _('Odometer (miles)'),
        'speed': _('Speed'),
        'latitude': _('Latitude'),
        'longitude': _('Longitude'),
        'power': _('Power'),
        'battery_level': _('Battery level (%)'),
        'battery_range': _('Battery range (miles)'),
        'charge_limit_soc': _('Battery charge limit (%)'),
        'charge_rate': _('Charge rate'),
        'charger_actual_current': _('Charger actual current (A)'),
        'charger_phases': _('Charger phases'),
        'charger_power': _('Charger power (kW)'),
        'charger_voltage': _('Charger voltage (V)'),
        'est_battery_range': _('Estimated battery range (miles)'),
        'usable_battery_level': _('Usable battery level (%)'),
        'battery_degradation': _('Battery degradation (%)'),
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


def GenerateDateGraph(datesList, maxvalues, minvalues, avgvalues, title):
    # From https://matplotlib.org/3.1.1/api/_as_gen/matplotlib.pyplot.plot_date.html
    fig = Figure(figsize=[12, 5])

    # format date acording to the language of the user
    language = django.utils.translation.get_language()
    if language is not None and language == 'fr':
        formatter = DateFormatter('%d/%m/%y')
    else:
        formatter = DateFormatter('%m/%d/%y')

    ax = fig.subplots()
    if datesList is not None and minvalues is not None:
        # See for line styles: https://matplotlib.org/api/_as_gen/matplotlib.pyplot.plot.html#matplotlib.pyplot.plot
        # and for possible args https://matplotlib.org/api/_as_gen/matplotlib.pyplot.plot_date.html#matplotlib.pyplot.plot_date
        ax.plot_date(datesList, minvalues, linestyle='-', label=_('Minimum'))
        ax.plot_date(datesList, avgvalues, linestyle='-', label=_('Average'))
        ax.plot_date(datesList, maxvalues, linestyle='-', label=_('Maximum'))
        ax.legend()  # https://matplotlib.org/api/_as_gen/matplotlib.axes.Axes.legend.html#matplotlib.axes.Axes.legend
        ax.xaxis.set_major_formatter(formatter)
        ax.ticklabel_format(axis='y', useOffset=False, style='plain')
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
    # Check that it is one field from the TeslaCarDataSnapshot
    validFields = TeslaCarDataSnapshot.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield), False
    return None, True


# allow to disable cache when improving graphs and you want a constant reload
# @never_cache
def StatsOnCarGraph(request, hashedVin, desiredfield, numberofdays):
    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response
    count = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count()
    if count == 0:
        return GenerateDateGraph(None, None, None, None, desiredfield)

    results = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin) \
        .values('DateOnlyDay') \
        .annotate(max_val=Max(desiredfield)).annotate(min_val=Min(desiredfield)). \
        annotate(avg_val=Avg(desiredfield)).order_by('DateOnlyDay')
    dates, maxvalues, minvalues, avgvalues = GetDatesAndValuesFromGroupByDateResult(results)
    return GenerateDateGraph(dates, maxvalues, minvalues, avgvalues, GetTitleForField(desiredfield))


# allow to disable cache when improving HTML and you want a constant reload
# @never_cache
def Stats(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    template = loader.get_template('personalstats/carstats.html')
    context = {}
    context["hashedVin"] = hashedVin
    context.update(GetTitleForFieldDico())
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


def BatteryDegradationGraph(request, hashedVin, desiredfield):
    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response

    count = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count()
    if count == 0:
        return GenerateDateGraph(None, None, None, None, 'battery_degradation')

    # see in anonymous stats for random samples
    results = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).order_by('randomNr')[:500]
    xvalues, yxvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    return GenerateScatterGraph(xvalues, yxvalues, GetTitleForField(desiredfield))


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
