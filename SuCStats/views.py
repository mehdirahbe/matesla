from django.http import HttpResponse
from django.db import connection
from django.template import loader

from anonymisedstats.views import PrepareCSVFromQuery

# Query to have monitored SuCs by observations frequency
queryToHaveAllSuCs = 'select matesla_allsuperchargers.name,count(*) Num_observations,' \
                     'max(matesla_superchargeruse.total_stalls) Total_stalls,' \
                     'min(matesla_superchargeruse.available_stalls) Min_available_stalls,' \
                     'max(matesla_superchargeruse.available_stalls) Max_available_stalls,' \
                     'matesla_allsuperchargers.latitude,matesla_allsuperchargers.longitude from matesla_superchargeruse ' \
                     'join matesla_allsuperchargers on matesla_allsuperchargers.id=matesla_superchargeruse.superchargerfkey_id ' \
                     'group by matesla_allsuperchargers.name,matesla_allsuperchargers.latitude,matesla_allsuperchargers.longitude ' \
                     'order by 2 desc'


def MonitoredSuCs(request):
    # show query results in table
    # https://stackoverflow.com/questions/58902342/show-in-a-table-html-template-the-result-of-a-sql-query-using-django
    # https://forum.djangoproject.com/t/display-table-from-sql-query-in-html/3162
    with connection.cursor() as cursor:
        cursor.execute(queryToHaveAllSuCs)
        context = context = {"rows": cursor.fetchall()}
        return HttpResponse(loader.get_template('SuCStats/MonitoredSuCs.html').render(context, request))


def MonitoredSuCsAsCSV(request):
    return PrepareCSVFromQuery(queryToHaveAllSuCs)


# local test URL: 127.0.0.1:8000/fr/SuCStats/MonitoredSuCsRawDataAsCSV
def MonitoredSuCsRawDataAsCSV(request):
    return PrepareCSVFromQuery('SELECT min(available_stalls) min_available_stalls,max(available_stalls) '
                               'max_available_stalls,max(total_stalls) total_stalls,"Date"::timestamp::date date,'
                               'superchargerfkey_id FROM public.matesla_superchargeruse group by '
                               '"Date"::timestamp::date,superchargerfkey_id')


# local test URL: 127.0.0.1:8000/fr/SuCStats/MonitoredSuCsListAsCSV
def MonitoredSuCsListAsCSV(request):
    return PrepareCSVFromQuery('SELECT id, name, type, latitude, longitude, "Date"	FROM '
                               'public.matesla_allsuperchargers;')


# local test URL: 127.0.0.1:8000/fr/SuCStats/MonitoredSuCsRawDataWithNamesAsCSV
def MonitoredSuCsRawDataWithNamesAsCSV(request):
    return PrepareCSVFromQuery('SELECT min(matesla_superchargeruse.available_stalls) min_available_stalls, '
                               'max(matesla_superchargeruse.available_stalls) max_available_stalls,'
                               'max(matesla_superchargeruse.total_stalls) total_stalls, '
                               'matesla_superchargeruse."Date"::timestamp::date date,matesla_allsuperchargers.name '
                               'FROM matesla_superchargeruse join matesla_allsuperchargers on '
                               'matesla_superchargeruse.superchargerfkey_id=matesla_allsuperchargers.id group by '
                               'matesla_superchargeruse."Date"::timestamp::date,matesla_allsuperchargers.name')
