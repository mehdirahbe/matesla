from tempfile import mktemp

from django.db.models import Count, Max
from django.http import HttpResponse
from django.shortcuts import render
import matplotlib.pyplot as plt
import os

# Create your views here.
from matesla.models import TeslaFirmwareHistory


def FirmwareUpdates(request):
    # query 10 most popular versions
    # queyr=TeslaFirmwareHistory.objects.values('Version').annotate(total=Count('Version')).order_by('-total')[:10].query
    # query 10 most recent versions
    queyr = TeslaFirmwareHistory.objects.values('Version').annotate(MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10].query
    results = TeslaFirmwareHistory.objects.values('Version').annotate(MostRecent=Max('Date')).annotate(
        total=Count('Version')).order_by('-MostRecent')[:10]
    names = list()
    values = list()
    for entry in results:
        names.append(entry['Version'])
        values.append(entry['total'])
    fig, axs = plt.subplots(nrows=1, ncols=1, figsize=(9, 3), sharey=True)
    axs.bar(names, values)
    fig.suptitle('Most recent Firmware updates')
    tempfilename = mktemp()
    plt.savefig(tempfilename)
    plt.close()
    f = open(tempfilename + '.png', "rb")
    content = f.read()
    f.close()
    os.remove(tempfilename + '.png')
    # return HttpResponse(queyr)
    return HttpResponse(content, content_type="image/png")
