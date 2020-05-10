import io

from django.db.models import Count, Max
from django.template import loader
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from django.http import HttpResponse
from django.contrib.auth import get_user
from django.db import connection
import csv

# Create your views here.
from django.views.decorators.cache import never_cache

from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.TeslaCarInfo import TeslaCarInfo


# return content as png of a bar graph with names (X), values (Y) with title
from mysite.settings import DATABASES


def GenerateBarGraph(names, values, title):
    # figsize is size in hundred of pixels
    # See https://matplotlib.org/3.2.1/faq/howto_faq.html#how-to-use-matplotlib-in-a-web-application-server
    # as pyplot in webserver will generate leaks
    # result: errors 500 in heroku official, grr
    # as defaukt dpi is 100, 9, 3 means 900*300 pixels
    # and we need more width than 9 for firmware as label are large
    fig = Figure(figsize=[12, 3])
    ax = fig.subplots(nrows=1, ncols=1, sharey=True)
    ax.bar(names, values)
    fig.suptitle(title)
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


def GetNamesAndValuesFromGroupByTotalResult(results, desiredfield):
    names = list()
    values = list()
    for entry in results:
        if entry[desiredfield] is None:
            names.append("No Value")
        else:
            if type(entry[desiredfield]) == type(True):
                if entry[desiredfield] is True:
                    names.append("True")
                else:
                    names.append("False")
            else:
                if type(entry[desiredfield]) == type(1):
                    if entry[desiredfield] == 1:
                        names.append("True")
                    else:
                        names.append("False")
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
    results = TeslaFirmwareHistory.objects.filter(IsArchive=False).values('Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, 'Version')
    return GenerateBarGraph(names, values, 'Most recent Firmware updates')


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
    results = TeslaCarInfo.objects.filter(car_type=CarModel).values(desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, desiredfield)


def StatsOnCarAllModelsGraph(request, desiredfield):
    results = TeslaCarInfo.objects.values(desiredfield).annotate(
        total=Count(desiredfield)).order_by(desiredfield)[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, desiredfield)
    return GenerateBarGraph(names, values, desiredfield)


@never_cache
def StatsChoicePage(request):
    return HttpResponse(loader.get_template('anonymisedstats/carstats.html').render({}, request))


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
