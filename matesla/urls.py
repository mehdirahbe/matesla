from django.urls import path

from . import views

urlpatterns = [
    path('', views.status, name='tesla_status'),
    path('matesla/statusJson', views.statusJson, name='teslastatusJson'),
    path('matesla/asleep', views.asleep, name='teslaasleep'),
    path('matesla/getdesiredchargelevel', views.getdesiredchargelevel, name='getdesiredchargelevel'),
    path('matesla/getdesiredtemperature', views.getdesiredtemperature, name='getdesiredtemperature'),
    path('matesla/flash_lights', views.Viewflash_lights, name='flash_lights'),
    path('matesla/honk_horn', views.Viewhonk_horn, name='honk_horn'),
    path('matesla/start_climate', views.Viewstart_climate, name='start_climate'),
    path('matesla/stop_climate', views.Viewstop_climate, name='stop_climate'),
    path('matesla/unlock_car', views.Viewunlock_car, name='unlock_car'),
    path('matesla/lock_car', views.Viewlock_car, name='lock_car'),
    path('matesla/AddTeslaAccount', views.view_AddTeslaAccount, name='AddTeslaAccount'),
    path('matesla/TeslaServerError', views.view_TeslaServerError, name='TeslaServerError'),
    path('matesla/TeslaServerCmdFail', views.view_TeslaServerCmdFail, name='TeslaServerCmdFail'),
    path('matesla/NoTeslaVehicules', views.view_NoTeslaVehicules, name='NoTeslaVehicules'),
    path('matesla/ConnectionError', views.view_ConnectionError, name='ConnectionError'),
    path('matesla/sentry_start', views.view_sentry_start, name='sentry_start'),
    path('matesla/sentry_stop', views.view_sentry_stop, name='sentry_stop'),
    path('matesla/valet_start', views.view_valet_start, name='valet_start'),
    path('matesla/valet_stop', views.view_valet_stop, name='valet_stop'),
    path('matesla/chargeport_open', views.view_chargeport_open, name='chargeport_open'),
    path('matesla/chargeport_close', views.view_chargeport_close, name='chargeport_close'),
    path('matesla/charge_start', views.view_charge_start, name='charge_start'),
    path('matesla/charge_stop', views.view_charge_stop, name='charge_stop'),
    path('matesla/remote_start_drive', views.view_remote_start_drive, name='remote_start_drive'),
]
