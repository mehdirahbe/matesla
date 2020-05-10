from django.urls import path

from . import views

'''From https://docs.djangoproject.com/en/3.0/topics/http/urls/
A request to /articles/2005/03/ would match the third entry in the list. Django
 would call the function views.month_archive(request, year=2005, month=3).
'''

urlpatterns = [
path('firmwareupdates', views.FirmwareUpdates, name='AnonymisedFirmwareUpdates'),
path('FirmwareUpdatesAsCSV', views.FirmwareUpdatesAsCSV, name='AnonymisedFirmwareUpdatesAsCSV'),
path('StatsOnCarGraph/<str:desiredfield>', views.StatsOnCarAllModelsGraph, name='AnonymisedStatsOnCarByModel'),
path('StatsOnCarGraph/<str:desiredfield>/<str:CarModel>', views.StatsOnCarByModelGraph, name='AnonymisedStatsOnCarByModel'),
path('AnonymisedStatsChoicePage', views.StatsChoicePage, name='AnonymisedStatsChoicePage'),
path('GetAllRawCarInfos', views.GetAllRawCarInfos, name='AnonymisedStatsGetAllRawCarInfos'),
]
