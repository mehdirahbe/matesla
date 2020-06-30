from django.urls import path

from . import views

'''From https://docs.djangoproject.com/en/3.0/topics/http/urls/
A request to /articles/2005/03/ would match the third entry in the list. Django
 would call the function views.month_archive(request, year=2005, month=3).
'''

urlpatterns = [
path('Stats/<str:hashedVin>', views.Stats, name='PersoStats'),
path('BatteryDegradationGraph/<str:hashedVin>/<str:desiredfield>', views.BatteryDegradationGraph, name='PersoStatsBatteryDegradationGraph'),
path('StatsOnCarGraph/<str:hashedVin>/<str:desiredfield>/<int:numberofdays>', views.StatsOnCarGraph, name='StatsOnCarGraph'),
path('AllMyDataAsCSV/<str:hashedVin>', views.view_AllMyDataAsCSV, name='AllMyDataAsCSV'),
path('FirmwareHistory/<str:hashedVin>', views.FirmwareHistory, name='PersoStatsFirmwareHistory'),
path('FirmwareHistoryCSV/<str:hashedVin>', views.FirmwareHistoryCSV, name='PersoStatsFirmwareHistoryCSV'),
]
