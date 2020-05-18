import io

import django
from django.db.models import Max, Min, Avg
from django.db.models.functions import TruncDate
from django.http import HttpResponse, HttpResponseNotFound
from django.template import loader
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import (DateFormatter)
from matplotlib.figure import Figure
from numpy.polynomial.polynomial import polyfit

from anonymisedstats.views import PrepareCSVFromQuery
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.VinHash import IsValidHash
from django.utils.translation import ugettext_lazy as _


# Create your views here.

# Return a dictionary with titles for fields
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


def GeneratePngFromGraph(fig):
    # https://stackoverflow.com/questions/49542459/error-in-django-when-using-matplotlib-examples
    buf = io.BytesIO()
    canvas = FigureCanvasAgg(fig)
    canvas.print_png(buf)
    response = HttpResponse(buf.getvalue(), content_type='image/png')
    # if required clear the figure for reuse
    fig.clear()
    # I recommend to add Content-Length for Django
    response['Content-Length'] = str(len(response.content))
    return response


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
        dates.append(entry['date'])
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
        .values(date=TruncDate('Date')) \
        .annotate(max_val=Max(desiredfield)).annotate(min_val=Min(desiredfield)). \
        annotate(avg_val=Avg(desiredfield)).order_by('date')
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


def GetXandYFromBatteryDegradResult(results, xfield):
    xvalues = list()
    yvalues = list()
    for entry in results:
        entry = entry.__dict__
        if entry['battery_degradation'] is None:
            continue
        if entry[xfield] is None:
            continue

        xvalues.append(entry[xfield])
        yvalues.append(entry['battery_degradation'])
    return xvalues, yvalues


def GenerateScatterGraph(xvalues, yvalues, title):
    # From https://matplotlib.org/3.2.1/api/_as_gen/matplotlib.pyplot.scatter.html
    fig = Figure(figsize=[12, 5])

    ax = fig.subplots()
    if xvalues is not None and yvalues is not None:
        ax.scatter(xvalues, yvalues)
        # do regression polynomial, see https://stackoverflow.com/questions/19068862/how-to-overplot-a-line-on-a-scatter-plot-in-python
        # and https://docs.scipy.org/doc/numpy/reference/generated/numpy.polyfit.html
        #a, b, c = polyfit(xvalues, yvalues, 2)
        a, b = polyfit(xvalues, yvalues, 1)
        regressy = []
        xvalues.sort()
        for x in xvalues:
            #y = c * x * x + b * x + a
            y = b * x + a
            regressy.append(y)
        ax.plot(xvalues, regressy, '-')
    fig.suptitle(title)
    return GeneratePngFromGraph(fig)


def BatteryDegradationGraph(request, hashedVin, desiredfield):
    response, isValid = SecurityChecks(hashedVin, desiredfield)
    if isValid is False:
        return response

    count = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count()
    if count == 0:
        return GenerateDateGraph(None, None, None, None, 'battery_degradation')

    results = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin)
    xvalues, yxvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    return GenerateScatterGraph(xvalues, yxvalues, GetTitleForField(desiredfield))
