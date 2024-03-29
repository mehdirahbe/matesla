from anonymisedstats.views import PrepareCSVFromQuery
from django.shortcuts import redirect
from django.utils.translation import ugettext_lazy as _

def MonitoredSuCs(request):
    # google data studio french url is https://datastudio.google.com/reporting/3cc14dae-6ea2-4f4b-869a-afc2c1e00489
    # and english url https://datastudio.google.com/reporting/119103e1-6912-4b1c-890c-f02a6083fc8c
    return redirect(_('https://datastudio.google.com/reporting/119103e1-6912-4b1c-890c-f02a6083fc8c'))

# local test URL: 127.0.0.1:8000/fr/SuCStats/MonitoredSuCsRawDataWithNamesAsCSV
def MonitoredSuCsRawDataWithNamesAsCSV(request):
    return PrepareCSVFromQuery('SELECT min(matesla_superchargeruse.available_stalls) min_available_stalls, '
                               'max(matesla_superchargeruse.available_stalls) max_available_stalls,'
                               'max(matesla_superchargeruse.total_stalls) total_stalls, '
                               'to_char(matesla_superchargeruse."Date", \'YYYYMMDDHH24\') date,matesla_allsuperchargers.name, '
                               'matesla_allsuperchargers.latitude,matesla_allsuperchargers.longitude,count(*) CountSample '
                               'FROM matesla_superchargeruse join matesla_allsuperchargers on '
                               'matesla_superchargeruse.superchargerfkey_id=matesla_allsuperchargers.id group by '
                               'to_char(matesla_superchargeruse."Date", \'YYYYMMDDHH24\'),matesla_allsuperchargers.name,'
                               'matesla_allsuperchargers.latitude,matesla_allsuperchargers.longitude')
