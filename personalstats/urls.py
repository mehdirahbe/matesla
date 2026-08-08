from django.urls import path

from . import views
from .views import FirmwareHistoryView

'''From https://docs.djangoproject.com/en/3.0/topics/http/urls/
A request to /articles/2005/03/ would match the third entry in the list. Django
 would call the function views.month_archive(request, year=2005, month=3).
'''

urlpatterns = [
path('Stats/<str:hashedVin>', views.Stats, name='PersoStats'),
# Day path / map: type a calendar day (or use prev/next arrows)
path('DayMap/<str:hashedVin>', views.DayMap, name='PersoDayMap'),
path('DayMap/<str:hashedVin>/<str:day>', views.DayMap, name='PersoDayMapDay'),
# Single fast-charge stop curve PNGs from day-map DC rows
# chart: power_vs_time | power_vs_soc (separate files for save/print)
path(
    'DayChargeSessionGraph/<str:hashedVin>/<str:day>/<int:start_ts>/<str:chart>',
    views.DayChargeSessionGraph,
    name='PersoDayChargeSessionGraph',
),
# Multi-criteria trip leaderboard (≥ 20 km): longest, elevation, cardinals, temp
path('Drives/<str:hashedVin>', views.Drives, name='PersoDrives'),
# DC fast-charge profiles (power vs SoC, SoC vs time)
path('DCCharge/<str:hashedVin>', views.DCCharge, name='PersoDCCharge'),
path(
    'DCChargeGraph/<str:hashedVin>/<str:chart>/<int:desiredperiod>',
    views.DCChargeGraph,
    name='PersoDCChargeGraph',
),
# Lifetime map JSON (path + KPIs) for the personal stats card
path('LifetimeMapData/<str:hashedVin>', views.LifetimeMapData, name='PersoLifetimeMapData'),
# Async reverse-geocode (cache miss → Nominatim, rate-limited)
path('ResolveAddress', views.ResolveAddress, name='PersoResolveAddress'),
# Async Supercharger match for day-map DC stops (cached directory)
path('MatchSupercharger', views.MatchSupercharger, name='PersoMatchSupercharger'),
# desiredperiod is weeks (0 = all history); keep in sync with #DesiredPeriod in the UI
path(
    'BatteryDegradationGraph/<str:hashedVin>/<str:desiredfield>/<int:desiredperiod>',
    views.BatteryDegradationGraph,
    name='PersoStatsBatteryDegradationGraph',
),
path('StatsOnCarGraph/<str:hashedVin>/<str:desiredfield>/<int:desiredperiod>', views.StatsOnCarGraph, name='StatsOnCarGraph'),
path('AllMyDataAsCSV/<str:hashedVin>', views.view_AllMyDataAsCSV, name='AllMyDataAsCSV'),
path('FirmwareHistory/<str:hashedVin>', views.FirmwareHistory, name='PersoStatsFirmwareHistory'),
path('FirmwareHistoryCSV/<str:hashedVin>', views.FirmwareHistoryCSV, name='PersoStatsFirmwareHistoryCSV'),
path('FirmwareHistory', FirmwareHistoryView.as_view(), name='PersoStatsFirmwareHistory'),
]
