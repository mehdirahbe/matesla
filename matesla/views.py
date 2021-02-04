import traceback

from django.contrib.auth import get_user
# Create your views here.
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.template import loader
from django.views.decorators.cache import never_cache
from matesla.TeslaConnect import *
from .forms import DesiredChargeLevelForm, AddTeslaAccountForm, DesiredTemperatureForm, RemoteStartDriveForm
from .models.TeslaToken import TeslaToken
from .models.VinHash import HashTheVin


@never_cache
def getdesiredchargelevel(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    # if this is a POST request we need to process the form data
    if request.method == 'POST':
        # create a form instance and populate it with data from the request:
        form = DesiredChargeLevelForm(request.POST)
        # check whether it's valid:
        if form.is_valid():
            # process the data in form.cleaned_data as required
            # redirect to a new URL:
            SetChargeLevel(form.cleaned_data["DesiredChargeLevel"], user)
            return redirect("tesla_status")
    # if a GET (or any other method) we'll create a blank form
    else:
        form = DesiredChargeLevelForm(initial={'DesiredChargeLevel': '90'})
    return render(request, 'matesla/getdesiredchargelevel.html', {'form': form})


@never_cache
def getdesiredtemperature(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    # if this is a POST request we need to process the form data
    if request.method == 'POST':
        # create a form instance and populate it with data from the request:
        form = DesiredTemperatureForm(request.POST)
        # check whether it's valid:
        if form.is_valid():
            # process the data in form.cleaned_data as required
            # redirect to a new URL:
            SetDriverTempCelcius(form.cleaned_data["DesiredTemperature"], user)
            return redirect("tesla_status")
    # if a GET (or any other method) we'll create a blank form
    else:
        form = DesiredTemperatureForm(initial={'DesiredTemperature': '20'})
    return render(request, 'matesla/getdesiredtemperature.html', {'form': form})


def view_teslacss(request):
    return HttpResponse(
        loader.get_template('matesla/tesla.css').render({}, request))


# This view signal the car is sleeping
@never_cache
def asleep(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/asleep.html').render({}, request)), True)


@never_cache
def view_TeslaServerError(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/TeslaServerError.html').render({}, request)), True)


@never_cache
def view_TeslaServerCmdFail(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/TeslaServerCmdFail.html').render({}, request)), True)


@never_cache
def view_NoTeslaVehicules(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/NoTeslaVehicules.html').render({}, request)), True)


@never_cache
def view_ConnectionError(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/ConnectionError.html').render({}, request)), True)


ValidColorCodes = {
    "PBSB": "Solid Black",
    "PPMR": "Red Multi-Coat",
    "PMNG": "Midnight Silver Metallic",
    "PPSB": "Deep Blue Metallic",
    "PPSW": "Pearl White Multi-Coat",
    "PMSS": "Silver Metallic",
    "PMBL": "Obsidian Black",
}


def returnColorFronContext(context):
    # if we know the color, use it (here is for David car) as codes
    # can't really be trusted (David car is black according to codes)
    if context["exterior_color"] == "PearlWhite":
        return "PPSW";
    if context["exterior_color"] == "DeepBlue":
        return "PPSB";
    if context["exterior_color"] == "SteelGrey" or context["exterior_color"] == "MidnightSilver":
        return "PMNG";
    if context["exterior_color"] == "RedMulticoat":
        return "PPMR";
    # get color code from codes
    colorcode = "PPMR"  # default value
    option_codeslist = context["option_codes"].split(',')
    for code in option_codeslist:
        if code in ValidColorCodes:
            colorcode = code
            break
    return colorcode


# Return true if a firmware update is downloadeand ready to install.
# arg is value return by the car and should contain something like this
# download_perc-->100
# status-->available
# version-->2020.36.11
def DoesHaveUpdateReady(software_update):
    if software_update["download_perc"] == 100 and software_update["status"] == "available":
        return True
    return False

def DoesHaveUpdateScheduled(software_update):
    if software_update["download_perc"] == 100 and software_update["status"] == "scheduled":
        return True
    return False

def DoesHaveUpdateInstalling(software_update):
    if software_update["download_perc"] == 100 and software_update["status"] == "installing":
        return True
    return False


# Prepare all entries used in the status page, given a request and logged user id
def PreparestatusDictionary(request, user):
    params = ParamsConnectedTesla(user)
    context = params.vehicle_state["response"]
    context.update(context["charge_state"])
    context.update(context["climate_state"])
    context.update(context["drive_state"])
    context.update(context["vehicle_config"])
    context.update(context["vehicle_state"])
    context["batteryrange"] = '{:.0f}'.format(params.batteryrange)
    if params.batterydegradation is not None:
        context["batterydegradation"] = '{:.1f}'.format(params.batterydegradation)
    else:
        context["batterydegradation"] = None
    if params.NumberCycles is None:
        context["NumberCycles"] = None
    else:
        context["NumberCycles"] = '{:.1f}'.format(params.NumberCycles)
    if params.EPARangeMiles is None:
        context["EPARangeMiles"] = None
    else:
        context["EPARangeMiles"] = '{:.0f}'.format(params.EPARangeMiles)
    context["hashedVin"] = HashTheVin(context["vin"])
    context["location"] = params.location
    context["OdometerInKm"] = '{:.0f}'.format(params.OdometerInKm)
    context["colorcode"] = returnColorFronContext(context)
    # link to go in google maps, tesla provide 6 decimals
    context["linktogooglemaps"] = "https://www.google.com/maps/search/?api=1&query=" + \
                                  '{:.6f}'.format(context["latitude"]) + ',' + \
                                  '{:.6f}'.format(context["longitude"])

    # Will allow to know if a firmware update can be installed, if yes, propose option
    context["hasUpdateReady"] = DoesHaveUpdateReady(context["software_update"])
    if context["hasUpdateReady"]:
        context["UpdateVersion"] = context["software_update"]["version"]
    context["hasUpdateScheduled"] = DoesHaveUpdateScheduled(context["software_update"])
    if context["hasUpdateScheduled"]:
        context["UpdateVersion"] = context["software_update"]["version"]
        context["UpdateVersionTimeSeconds"] = str(context["software_update"]["warning_time_remaining_ms"]/1000)
    context["hasUpdateInstalling"] = DoesHaveUpdateInstalling(context["software_update"])
    if context["hasUpdateInstalling"]:
        context["UpdateVersion"] = context["software_update"]["version"]
        context["UpdateVersionInstallPerc"] = str(context["software_update"]["install_perc"])


    # allow to check if we are on deployment test server, or localhost
    WebServerName = str(request.get_host())
    context["IsLocalHost"] = WebServerName == "127.0.0.1:8000"
    context["Isafternoonscrubland"] = WebServerName == "afternoon-scrubland-61531.herokuapp.com"
    return context


# Prepare the status page, given a request and logged user id
def Preparestatus(request, user):
    context = PreparestatusDictionary(request, user)
    template = loader.get_template('matesla/carstatus.html')
    return HttpResponse(template.render(context, request))


# Get the status page data as Json, given a request and logged user id
def PreparestatusJson(request, user):
    context = PreparestatusDictionary(request, user)
    # See https://simpleisbetterthancomplex.com/tutorial/2016/07/27/how-to-return-json-encoded-response.html
    return JsonResponse(context)


# The status view
@never_cache
def status(request):
    return singleAction(request, lambda request, user: Preparestatus(request, user), True)


# The status json data
@never_cache
def statusJson(request):
    return singleActionJson(request, lambda request, user: PreparestatusJson(request, user))


'''Check login, and if fine call func. Return its output.
On tesla login error, return json error detail.
Never redirect.'''


def singleActionJson(request, func):
    user = get_user(request)
    if not user.is_authenticated:
        return JsonResponse({'error': 'not logged'})
    try:
        ret = func(request, user)
        return ret
    except TeslaIsAsleepException:
        # if asleep
        WaitForWakeUp(Connect(user))
        return JsonResponse({'error': 'TeslaIsAsleepException'})
    except TeslaNoUserException:
        return JsonResponse({'error': 'TeslaNoUserException'})
    except TeslaUnauthorisedException:
        return JsonResponse({'error': 'TeslaUnauthorisedException'})
    except TeslaAuthenticationException:
        return JsonResponse({'error': 'TeslaAuthenticationException'})
    except TeslaServerException:
        return JsonResponse({'error': 'TeslaServerException'})
    except TeslaCommandException:
        return JsonResponse({'error': 'TeslaCommandException'})
    except TeslaNoVehiculeException:
        return JsonResponse({'error': 'TeslaNoVehiculeException'})
    except requests.exceptions.ConnectionError:
        return JsonResponse({'error': 'ConnectionError'})
    except Exception as ex:
        traceback.print_exc()
        return JsonResponse({'error': type(ex).__name__})
    return JsonResponse({'error': 'How did we arrive here?'})


'''Check login, and if fine call func.  Then go to status page.
On tesla login error, go to tesla credentials page.'''


def singleAction(request, func, shouldReturnFunc=False):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    try:
        ret = func(request, user)
        if shouldReturnFunc == True:
            return ret
    except TeslaIsAsleepException:
        # if asleep
        WaitForWakeUp(Connect(user))
        return redirect('teslaasleep')
    except TeslaNoUserException:
        return redirect('AddTeslaAccount')
    except TeslaUnauthorisedException:
        return redirect('AddTeslaAccount')
    except TeslaAuthenticationException:
        return redirect('AddTeslaAccount')
    except TeslaServerException:
        return redirect('TeslaServerError')
    except TeslaCommandException:
        return redirect('TeslaServerCmdFail')
    except TeslaNoVehiculeException:
        return redirect('NoTeslaVehicules')
    except requests.exceptions.ConnectionError:
        return redirect('ConnectionError')
    except Exception as ex:
        traceback.print_exc()
        return HttpResponse(type(ex).__name__)
    return redirect("tesla_status")


# View which honk (dont call during the night!) and then display status page
@never_cache
def Viewhonk_horn(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'honk_horn'))


# View which flash lights and then display status page
@never_cache
def Viewflash_lights(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'flash_lights'))


# View which start car warmup and then display status page
@never_cache
def Viewstart_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_start'))


# View which stop car warmup and then display status page
@never_cache
def Viewstop_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_stop'))


# View which stop car warmup and then display status page
@never_cache
def Viewunlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_unlock'))


# View which stop car warmup and then display status page
@never_cache
def Viewlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_lock'))


# View which allow to add/edit tesla credentials
@never_cache
def view_AddTeslaAccount(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    if request.method == 'POST':
        tesla_account_form = AddTeslaAccountForm(request.POST)
        if tesla_account_form.is_valid():
            # remove any previous token
            try:
                TeslaToken.objects.get(user_id=user.id).delete()
            except ObjectDoesNotExist:
                pass
            # and save
            tesla_account_form.SaveModdel(user)
            return redirect("tesla_status")
    # display form
    return render(request, 'matesla/AddTeslaAccount.html',
                  {'tesla_account_form': AddTeslaAccountForm()})


# Start sentry
@never_cache
def view_sentry_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', True))


# Stop sentry
@never_cache
def view_sentry_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', False))


# Start valet mode
@never_cache
def view_valet_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', True))


# Stop valet mode
@never_cache
def view_valet_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', False))


# Open charge port
@never_cache
def view_chargeport_open(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_open'))


# Close charge port
@never_cache
def view_chargeport_close(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_close'))


# Start charge
@never_cache
def view_charge_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_start'))


# Stop charge
@never_cache
def view_charge_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_stop'))


# Start install of software update, with a 2 minutes timeout, as the car propose
@never_cache
def view_install_software_update(request):
    return singleAction(request,
                        lambda request, user: executeCommand(user, 'schedule_software_update', None, 'offset_sec', 120))


# Activate remote drive, show a dialog asking PW
@never_cache
def view_remote_start_drive(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    # if this is a POST request we need to process the form data
    if request.method == 'POST':
        # create a form instance and populate it with data from the request:
        form = RemoteStartDriveForm(request.POST)
        # check whether it's valid:
        if form.is_valid():
            # process the data in form.cleaned_data as required
            # redirect to a new URL:
            ActivateRemoteStartDrive(form.cleaned_data["TeslaPassword"], user)
            return redirect("tesla_status")
    # if a GET (or any other method) we'll create a blank form
    else:
        form = RemoteStartDriveForm(initial={'TeslaPassword': ''})
    return render(request, 'matesla/getRemote_start_drivePassword.html', {'form': form})
