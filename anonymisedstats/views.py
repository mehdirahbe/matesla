from tempfile import mktemp

from django.db.models import Count, Max
from django.http import HttpResponse
from django.template import loader
import matplotlib.pyplot as plt
import os
from django.contrib.auth import get_user

# Create your views here.
from django.views.decorators.cache import never_cache

from matesla.models import TeslaFirmwareHistory, TeslaCarInfo


# return content as png of a bar graph with names (X), values (Y) with title
def GenerateBarGraph(names, values, title):
    #figsize is size in hundred of pixels
    fig, axs = plt.subplots(nrows=1, ncols=1, figsize=(9, 3), sharey=True)
    axs.bar(names, values)
    fig.suptitle(title)
    tempfilename = mktemp()
    plt.savefig(tempfilename)
    plt.close()
    f = open(tempfilename + '.png', "rb")
    content = f.read()
    f.close()
    os.remove(tempfilename + '.png')
    # return HttpResponse(queyr)
    return HttpResponse(content, content_type="image/png")


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
                    names.append(entry[desiredfield])
        values.append(entry['total'])
    return names, values


def FirmwareUpdates(request):
    # query 10 most popular versions
    # queyr=TeslaFirmwareHistory.objects.values('Version').annotate(total=Count('Version')).order_by('-total')[:10].query
    # query 10 most recent versions
    queyr = TeslaFirmwareHistory.objects.filter(IsArchive=False).values('Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10].query
    results = TeslaFirmwareHistory.objects.filter(IsArchive=False).values('Version').annotate(
        MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10]
    names, values = GetNamesAndValuesFromGroupByTotalResult(results, 'Version')
    return GenerateBarGraph(names, values, 'Most recent Firmware updates')


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


# view for admin in order to download all car info
def GetAllRawCarInfos(request):
    user = get_user(request)
    if not user.is_authenticated or not user.is_superuser:
        return HttpResponse('Accessing all raw car infos is only for admins')
    countRows = TeslaCarInfo.objects.count()
    if countRows == 0:
        return HttpResponse("")
    results = TeslaCarInfo.objects.values()
    retAsTabDelimitted = ""

    # generate heading
    entry = results[0]
    for fields in entry:
        retAsTabDelimitted += fields
        retAsTabDelimitted += "\t"
    retAsTabDelimitted += "\n"

    # then values
    for entry in results:
        for field in entry.values():
            retAsTabDelimitted += str(field)
            retAsTabDelimitted += "\t"
        retAsTabDelimitted += "\n"
    return HttpResponse(retAsTabDelimitted, content_type="text/plain")
