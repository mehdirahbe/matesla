import io

import numpy as np
from django.db.models import Count, Max
from django.template import loader
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from django.http import HttpResponse, HttpResponseNotFound
from django.contrib.auth import get_user
from django.db import connection
import csv
from django.utils.translation import ugettext_lazy as _
from datetime import timedelta
from django.utils import timezone

# Create your views here.
from django.views.decorators.cache import never_cache

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.TeslaCarInfo import TeslaCarInfo

# in a general way, I eject cars not seen for maxdaysinthepast days
# for period test: https://stackoverflow.com/questions/1984047/django-filter-older-than-days
# cars should be refreshed at least once a day, ok, they can be unreachable
# (no network), but anyway it should be short
maxdaysinthepast = 15

# return content as png of a bar graph with names (X), values (Y) with title
from mysite.settings import DATABASES


def GeneratePngFromGraph(fig):
    # activate this when you want performance analysis
    # return HttpResponse("<html><body>todo activate this only for performance test of graphs</body></html>")

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


def GenerateBarGraph(names, values, title):
    # figsize is size in hundred of pixels
    # See https://matplotlib.org/3.2.1/faq/howto_faq.html#how-to-use-matplotlib-in-a-web-application-server
    # as pyplot in webserver will generate leaks
    # result: errors 500 in heroku official, grr
    # as default dpi is 100, 9, 3 means 900*300 pixels
    # and we need more width than 9 for firmware as label are large
    fig = Figure(figsize=[12, 3])
    ax = fig.subplots(nrows=1, ncols=1, sharey=True)
    if names is not None and values is not None:
        ax.bar(names, values)
    fig.suptitle(title)
    return GeneratePngFromGraph(fig)


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
    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaFirmwareHistory.objects.filter(
        vin__in=TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).values('vin')).filter(
        IsArchive=False).values(
        'Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, 'Version')
    return GenerateBarGraph(names, values, _('Most recent Firmware updates'))


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

    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).filter(car_type=CarModel).values(
        desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, GetTitleForField(desiredfield))


def StatsOnCarAllModelsGraph(request, desiredfield):
    # Check that it is one field from the TeslaCarInfo
    validFields = TeslaCarInfo.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield)

    time_threshold = timezone.now() - timedelta(days=maxdaysinthepast)
    results = TeslaCarInfo.objects.filter(LastSeenDate__gte=time_threshold).values(desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, GetTitleForField(desiredfield))


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


# I want 2 decimals, and scientific notation...when it has a meaning, because
# 10 exponant 0 is not very helpfull
def FormatDouble2Decimals(d):
    return "{:.2e}".format(d).replace("e+00", "")


def GenerateScatterGraph(xvalues, yvalues, title):
    # From https://matplotlib.org/3.2.1/api/_as_gen/matplotlib.pyplot.scatter.html
    fig = Figure(figsize=[12, 5])

    ax = fig.subplots()
    if xvalues is not None and yvalues is not None:
        ax.scatter(xvalues, yvalues)
        # do regression polynomial, see https://stackoverflow.com/questions/19068862/how-to-overplot-a-line-on-a-scatter-plot-in-python
        # and https://docs.scipy.org/doc/numpy/reference/generated/numpy.polyfit.html
        # and https://riptutorial.com/numpy/example/27442/using-np-polyfit
        # add a second order polynomial
        # returns params of the polynomial
        p2 = np.polyfit(xvalues, yvalues, 2)  # Last argument is degree of polynomial
        f2 = np.poly1d(p2)  # So we can call f(x)
        # draw it, after removing dups (for perf) and sorting it (to have continuous line)
        sortedx = list(dict.fromkeys(xvalues))
        sortedx.sort()
        ax.plot(sortedx, f2(sortedx), '-',
                label=FormatDouble2Decimals(p2[0]) + "x2+" + FormatDouble2Decimals(
                    p2[1]) + "x+" + FormatDouble2Decimals(p2[2]))
        # now add a linear fit, and user can see which match data the best
        p1 = np.polyfit(xvalues, yvalues, 1)
        f1 = np.poly1d(p1)
        # draw it (dups removed above)
        ax.plot(sortedx, f1(sortedx), '-',
                label=FormatDouble2Decimals(p1[0]) + "x+" + FormatDouble2Decimals(p1[1]))
        ax.legend()
    fig.suptitle(title)
    return GeneratePngFromGraph(fig)


def BatteryDegradationGraph(request, desiredfield):
    # Similar (and reuse) wht is done for equivalent graph in personal graph,
    # but here we mix all car data

    # Check that it is one field from the TeslaCarDataSnapshot
    validFields = TeslaCarDataSnapshot.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield), False

    count = TeslaCarDataSnapshot.objects.count()
    if count == 0:
        return GenerateBarGraph(None, None, GetTitleForField(desiredfield))

    # to have a random sample
    # see https://stackoverflow.com/questions/31801826/random-sample-on-django-querysets-how-will-sampling-on-querysets-affect-perform
    # but as data grows, it it faster to have a real random number in data
    # we are now at 6000 rows and it is 3 times faster, as using ?-->random()
    # needs to read all the table and sort it by random.
    # While using indexed randomNr just take the first rows of the index (yes, it uses the index).
    results = TeslaCarDataSnapshot.objects.all().order_by('randomNr')[:500]
    xvalues, yxvalues = GetXandYFromBatteryDegradResult(results, desiredfield)
    return GenerateScatterGraph(xvalues, yxvalues, GetTitleForField(desiredfield))
