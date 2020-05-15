import io

from django.db.models import Max, Min, Avg
from django.db.models.functions import TruncDate
from django.http import HttpResponse, HttpResponseNotFound
from django.template import loader
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import (DateFormatter)
from matplotlib.figure import Figure

from anonymisedstats.views import PrepareCSVFromQuery
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.VinHash import IsValidHash


# Create your views here.

def GenerateDateGraph(datesList, maxvalues, minvalues, avgvalues, title):
    # From https://matplotlib.org/3.1.1/api/_as_gen/matplotlib.pyplot.plot_date.html
    fig = Figure(figsize=[12, 5])

    formatter = DateFormatter('%m/%d/%y')

    ax = fig.subplots()
    if datesList is not None and minvalues is not None:
        ax.plot_date(datesList, minvalues)
        ax.plot_date(datesList, avgvalues)
        ax.plot_date(datesList, maxvalues)
        ax.xaxis.set_major_formatter(formatter)
        ax.ticklabel_format(axis='y', useOffset=False, style='plain')
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


#allow to disable cache when improving graphs and you want a constant reload
#@never_cache
def StatsOnCarGraph(request, hashedVin, desiredfield, numberofdays):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    # Check that it is one field from the TeslaCarDataSnapshot
    validFields = TeslaCarDataSnapshot.__dict__
    if desiredfield is None or desiredfield not in validFields:
        # means invalid desiredfield field was passed
        return HttpResponseNotFound("Graph for this field doesn't exists " + desiredfield)

    count = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin).count()
    if count == 0:
        return GenerateDateGraph(None, None, None, None, desiredfield)

    results = TeslaCarDataSnapshot.objects.filter(hashedVin=hashedVin) \
        .values(date=TruncDate('Date')) \
        .annotate(max_val=Max(desiredfield)).annotate(min_val=Min(desiredfield)).annotate(avg_val=Avg(desiredfield))
    dates, maxvalues, minvalues, avgvalues = GetDatesAndValuesFromGroupByDateResult(results)
    return GenerateDateGraph(dates, maxvalues, minvalues, avgvalues, desiredfield)


#allow to disable cache when improving HTML and you want a constant reload
#@never_cache
def Stats(request, hashedVin):
    if not IsValidHash(hashedVin):
        # means invalid hashedVin field was passed
        return HttpResponseNotFound("This hashed vin is not valid " + hashedVin)
    template = loader.get_template('personalstats/carstats.html')
    context = {}
    context["hashedVin"] = hashedVin
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
