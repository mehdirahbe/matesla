from django.urls import path

from . import views

urlpatterns = [
    path('', views.status, name='tesla_status'),
    path('matesla/statusJson', views.statusJson, name='teslastatusJson'),
    path('matesla/asleep', views.asleep, name='teslaasleep'),
    path('matesla/AddTeslaAccount', views.view_AddTeslaAccount, name='AddTeslaAccount'),
    path('matesla/oauth/start', views.view_tesla_oauth_start, name='tesla_oauth_start'),
    path('matesla/select_vehicle', views.view_select_vehicle, name='select_vehicle'),
    path('matesla/TeslaServerError', views.view_TeslaServerError, name='TeslaServerError'),
    path('matesla/NoTeslaVehicules', views.view_NoTeslaVehicules, name='NoTeslaVehicules'),
    path('matesla/ConnectionError', views.view_ConnectionError, name='ConnectionError'),
]
