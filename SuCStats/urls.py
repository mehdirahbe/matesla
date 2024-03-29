from django.urls import path

from . import views

'''From https://docs.djangoproject.com/en/3.0/topics/http/urls/
A request to /articles/2005/03/ would match the third entry in the list. Django
 would call the function views.month_archive(request, year=2005, month=3).
'''

urlpatterns = [
path('MonitoredSuCs', views.MonitoredSuCs, name='SuCStatsMonitoredSuCs'),
path('MonitoredSuCsRawDataWithNamesAsCSV', views.MonitoredSuCsRawDataWithNamesAsCSV, name='SuCStatsMonitoredSuCsRawDataWithNamesAsCSV'),
]
