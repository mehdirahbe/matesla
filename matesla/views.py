from django.contrib.auth import get_user
# Create your views here.
from django.http import HttpResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.template import loader
from django.core.exceptions import ObjectDoesNotExist

from matesla.TeslaConnect import *
from .forms import DesiredChargeLevelForm, AddTeslaAccountForm

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


def view_teslacss(request):
    return HttpResponse(
        loader.get_template('matesla/tesla.css').render({}, request))


# This view signal the car is sleeping
def asleep(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/asleep.html').render({}, request)), True)


def view_TeslaServerError(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/TeslaServerError.html').render({}, request)), True)


def view_TeslaServerCmdFail(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/TeslaServerCmdFail.html').render({}, request)), True)


def view_NoTeslaVehicules(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/NoTeslaVehicules.html').render({}, request)), True)


def view_ConnectionError(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/ConnectionError.html').render({}, request)), True)


def returnColorFronContext(context):
    #if we know the color, use it (here is for David car) as codes
    # can't really be trusted (David car is black according to codes)
    if context["exterior_color"]=="PearlWhite":
        return "PPSW";
    # get color code from codes
    colordico = {
        "PBSB": "Solid Black",
        "PPMR": "Red Multi-Coat",
        "PMNG": "Midnight Silver Metallic",
        "PPSB": "Deep Blue Metallic",
        "PPSW": "Pearl White Multi-Coat",
        "PMSS": "Silver Metallic",
        "PMBL": "Obsidian Black"
    }
    colorcode = "PPMR"  # default value
    option_codeslist = context["option_codes"].split(',')
    for code in option_codeslist:
        if code in colordico:
            colorcode = code
            break
    return colorcode


# Prepare the status page, given a request and logged user id
def Preparestatus(request, user):
    params = ParamsConnectedTesla(user)
    context = params.vehicle_state["response"]
    context.update(context["charge_state"])
    context.update(context["climate_state"])
    context.update(context["drive_state"])
    context.update(context["vehicle_state"])
    context["batteryrange"] = '{:.0f}'.format(params.batteryrange)
    context["batterydegradation"] = '{:.1f}'.format(params.batterydegradation)
    context["location"] = params.location
    context["OdometerInKm"] = '{:.0f}'.format(params.OdometerInKm)
    template = loader.get_template('matesla/carstatus.html')
    context["colorcode"] = returnColorFronContext(context)
    # link to go in google maps, tesla provide 6 decimals
    context["linktogooglemaps"] = "https://www.google.com/maps/search/?api=1&query=" + \
                                  '{:.6f}'.format(context["latitude"]) + ',' + \
                                  '{:.6f}'.format(context["longitude"])

    return HttpResponse(template.render(context, request))


# The status view
def status(request):
    return singleAction(request, lambda request, user: Preparestatus(request, user), True)


# The user info view (debug purpose)
def UserInfo(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect('login')
    teslaaccount = TeslaAccount.objects.get(user_id=user.id)
    teslalogin = teslaaccount.TeslaUser
    # to debug pw encryption only, never activate in prod
    '''saltlogin = getSaltForKey(teslalogin)
    teslapw = decrypt(teslaaccount.TeslaPassword, saltlogin)'''
    teslapw = 'crypté'
    return HttpResponse(str(user.id) + "</BR>" + str(user.pk) + "</BR>" + str(teslalogin) + "</BR>" + str(teslapw))


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
        return HttpResponse(type(ex).__name__)
    return redirect("tesla_status")


# View which honk (dont call during the night!) and then display status page
def Viewhonk_horn(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'honk_horn'))


# View which flash lights and then display status page
def Viewflash_lights(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'flash_lights'))


# View which start car warmup and then display status page
def Viewstart_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_start'))


# View which stop car warmup and then display status page
def Viewstop_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_stop'))


# View which stop car warmup and then display status page
def Viewunlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_unlock'))


# View which stop car warmup and then display status page
def Viewlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_lock'))


# View which allow to add/edit tesla credentials
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
            #and save
            tesla_account_form.SaveModdel(user)
            return redirect("tesla_status")
    #display form
    return render(request, 'matesla/AddTeslaAccount.html',
                      {'tesla_account_form': AddTeslaAccountForm()})

# Start sentry
def view_sentry_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', True))


# Stop sentry
def view_sentry_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', False))


# Start valet mode
def view_valet_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', True))


# Stop valet mode
def view_valet_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', False))


# Open charge port
def view_chargeport_open(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_open'))


# Close charge port
def view_chargeport_close(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_close'))


# Start charge
def view_charge_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_start'))


# Stop charge
def view_charge_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_stop'))
