import traceback

from django.contrib import messages
from django.contrib.auth import get_user, get_user_model, login
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.template import loader
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from matesla.TeslaConnect import *  # noqa: F403
# SESSION_ACTIVE_VEHICLE_KEY, list_user_vehicles, resolve_active_vehicle, set_active_vehicle via *
from matesla.TeslaOAuth import (
    TeslaOAuthError,
    apply_token_response,
    build_authorize_url,
    exchange_code_for_tokens,
    new_oauth_state,
    sync_vehicles_from_api,
)
from matesla.TeslaPartner import (
    TeslaPartnerError,
    ensure_key_pair,
    public_key_pem_text,
    public_key_url,
    check_public_key_reachable,
    register_partner_account,
)
from .forms import (
    DesiredChargeLevelForm,
    DesiredTemperatureForm,
    RemoteStartDriveForm,
    TeslaAppSettingsForm,
)
from .models.TeslaAppSettings import TeslaAppSettings
from .models.TeslaOAuthPending import TeslaOAuthPending
from .models.TeslaToken import TeslaToken, TeslaVehicle
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
            SetChargeLevel(form.cleaned_data["DesiredChargeLevel"], user, request=request)
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
            SetDriverTempCelcius(form.cleaned_data["DesiredTemperature"], user, request=request)
            return redirect("tesla_status")
    # if a GET (or any other method) we'll create a blank form
    else:
        form = DesiredTemperatureForm(initial={'DesiredTemperature': '20'})
    return render(request, 'matesla/getdesiredtemperature.html', {'form': form})


def view_teslacss(request):
    return HttpResponse(
        loader.get_template('matesla/tesla.css').render({}, request))


# Vehicle is sleeping — matesla never wakes (no billable wake_up).
@never_cache
def asleep(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect("login")
    active = resolve_active_vehicle(user, request)
    return render(
        request,
        "matesla/asleep.html",
        {"active_vehicle_label": active.label if active else None},
    )


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
    exterior = context.get("exterior_color") or ""
    if exterior == "PearlWhite":
        return "PPSW"
    if exterior == "DeepBlue":
        return "PPSB"
    if exterior in ("SteelGrey", "MidnightSilver"):
        return "PMNG"
    if exterior == "RedMulticoat":
        return "PPMR"
    # get color code from option codes (often null on Fleet API)
    colorcode = "PPSW"
    option_codes = context.get("option_codes") or ""
    if option_codes:
        for code in option_codes.split(","):
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
    if not software_update:
        return False
    return software_update.get("download_perc") == 100 and software_update.get("status") == "available"


def DoesHaveUpdateScheduled(software_update):
    if not software_update:
        return False
    return software_update.get("download_perc") == 100 and software_update.get("status") == "scheduled"


def DoesHaveUpdateInstalling(software_update):
    if not software_update:
        return False
    return software_update.get("download_perc") == 100 and software_update.get("status") == "installing"


# Prepare all entries used in the status page, given a request and logged user id
def PreparestatusDictionary(request, user):
    params = ParamsConnectedTesla(user, request)
    context = dict(params.vehicle_state.get("response") or {})
    vehicles = list_user_vehicles(user)
    active = resolve_active_vehicle(user, request)
    # Dicts (not model instances) so statusJson can JSON-serialize the context
    context["user_vehicles"] = [
        {
            "api_id": v.api_id,
            "vin": v.vin,
            "display_name": v.display_name,
            "label": v.label,
            "state": v.state,
            "is_primary": v.is_primary,
        }
        for v in vehicles
    ]
    context["active_vehicle_api_id"] = active.api_id if active else None
    context["active_vehicle_label"] = active.label if active else None
    context["display_name"] = params.name or context.get("display_name") or "Tesla"

    # Flatten nested Fleet states (may be empty dicts)
    for nested_key in (
        "charge_state",
        "climate_state",
        "drive_state",
        "vehicle_config",
        "vehicle_state",
    ):
        nested = context.get(nested_key) or {}
        if isinstance(nested, dict):
            context.update(nested)

    context["batteryrange"] = "{:.0f}".format(params.batteryrange or 0)
    context["batterydegradation"] = (
        "{:.1f}".format(params.batterydegradation)
        if params.batterydegradation is not None
        else None
    )
    context["NumberCycles"] = (
        "{:.1f}".format(params.NumberCycles) if params.NumberCycles is not None else None
    )
    context["EPARangeMiles"] = (
        "{:.0f}".format(params.EPARangeMiles) if params.EPARangeMiles is not None else None
    )
    vin = context.get("vin") or ""
    context["hashedVin"] = HashTheVin(vin) if vin else ""
    context["location"] = params.location or ""
    context["OdometerInKm"] = "{:.0f}".format(params.OdometerInKm or 0)

    try:
        context["colorcode"] = returnColorFronContext(context)
    except Exception:
        context["colorcode"] = "PPSW"

    lat = context.get("latitude")
    lon = context.get("longitude")
    if lat is not None and lon is not None:
        context["linktogooglemaps"] = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{float(lat):.6f},{float(lon):.6f}"
        )
    else:
        context["linktogooglemaps"] = ""

    software_update = context.get("software_update") or {}
    context["hasUpdateReady"] = DoesHaveUpdateReady(software_update) if software_update else False
    context["hasUpdateScheduled"] = (
        DoesHaveUpdateScheduled(software_update) if software_update else False
    )
    context["hasUpdateInstalling"] = (
        DoesHaveUpdateInstalling(software_update) if software_update else False
    )
    if context["hasUpdateReady"] or context["hasUpdateScheduled"] or context["hasUpdateInstalling"]:
        context["UpdateVersion"] = software_update.get("version")
    if context["hasUpdateScheduled"]:
        warn_ms = software_update.get("warning_time_remaining_ms") or 0
        context["UpdateVersionTimeSeconds"] = str(warn_ms / 1000)
    if context["hasUpdateInstalling"]:
        context["UpdateVersionInstallPerc"] = str(software_update.get("install_perc") or 0)

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
    return singleAction(request, lambda request, user: executeCommand(user, 'honk_horn', request=request))


# View which flash lights and then display status page
@never_cache
def Viewflash_lights(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'flash_lights', request=request))


# View which start car warmup and then display status page
@never_cache
def Viewstart_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_start', request=request))


# View which stop car warmup and then display status page
@never_cache
def Viewstop_climate(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'auto_conditioning_stop', request=request))


# View which stop car warmup and then display status page
@never_cache
def Viewunlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_unlock', request=request))


# View which stop car warmup and then display status page
@never_cache
def Viewlock_car(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'door_lock', request=request))


# Connect Tesla account via Fleet API OAuth (tokens stored in DB)
@never_cache
@require_http_methods(["GET", "POST"])
def view_AddTeslaAccount(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect("login")

    app_settings = TeslaAppSettings.get_solo()
    token = TeslaToken.objects.filter(user_id=user.id).first()
    vehicles = list_user_vehicles(user)
    active = resolve_active_vehicle(user, request)

    if request.method == "POST" and request.POST.get("action") == "save_app":
        form = TeslaAppSettingsForm(request.POST, instance=app_settings)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Identifiants développeur enregistrés.",
            )
            return redirect("AddTeslaAccount")
        app_form = form
    elif request.method == "POST" and request.POST.get("action") == "generate_keys":
        priv, pub = ensure_key_pair()
        messages.success(
            request,
            f"Clés générées : {pub} (à publier) et {priv} (secret, local).",
        )
        return redirect("AddTeslaAccount")
    elif request.method == "POST" and request.POST.get("action") == "check_public_key":
        domain = (request.POST.get("partner_domain") or "").strip()
        if app_settings and not domain:
            domain = app_settings.partner_domain
        if not domain:
            messages.error(request, "Indique un domaine partner d'abord.")
        else:
            ok, msg = check_public_key_reachable(domain)
            (messages.success if ok else messages.error)(request, msg)
        return redirect("AddTeslaAccount")
    elif request.method == "POST" and request.POST.get("action") == "register_partner":
        domain = (request.POST.get("partner_domain") or "").strip()
        if app_settings and not domain:
            domain = app_settings.partner_domain
        try:
            if app_settings and domain and domain != app_settings.partner_domain:
                app_settings.partner_domain = domain
                app_settings.save(update_fields=["partner_domain", "updated_at"])
            result = register_partner_account(domain)
            messages.success(
                request,
                f"Partner register OK pour « {domain} ». "
                f"Tu peux maintenant reconnecter / resync les véhicules. Détail: {result}",
            )
        except TeslaPartnerError as exc:
            messages.error(request, str(exc))
        return redirect("AddTeslaAccount")
    elif request.method == "POST" and request.POST.get("action") == "resync_vehicles":
        if not token:
            messages.error(request, "Pas de token Tesla — connecte-toi d'abord.")
            return redirect("AddTeslaAccount")
        try:
            from matesla.TeslaOAuth import ensure_fresh_access_token

            token = ensure_fresh_access_token(token)
            vehicles_payload = GetVehicles(token.access_token)
            vehicles = sync_vehicles_from_api(user, vehicles_payload)
            if vehicles:
                primary = next((v for v in vehicles if v.is_primary), vehicles[0])
                set_active_vehicle(request, user, primary.api_id)
                messages.success(
                    request,
                    f"{len(vehicles)} véhicule(s) synchronisé(s). Actif : {primary.label}.",
                )
                return redirect("tesla_status")
            messages.warning(request, "Aucun véhicule sur le compte Tesla.")
        except TeslaFleetApiError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            traceback.print_exc()
            messages.error(request, f"Resync échoué : {exc}")
        return redirect("AddTeslaAccount")
    elif request.method == "POST" and request.POST.get("action") == "disconnect":
        TeslaToken.objects.filter(user_id=user.id).delete()
        TeslaVehicle.objects.filter(user=user).delete()
        request.session.pop(SESSION_ACTIVE_VEHICLE_KEY, None)
        messages.info(request, "Compte Tesla déconnecté (tokens et véhicules supprimés).")
        return redirect("AddTeslaAccount")
    elif request.method == "POST" and request.POST.get("action") == "select_vehicle":
        api_id = request.POST.get("vehicle_api_id")
        if set_active_vehicle(request, user, api_id):
            messages.success(request, "Véhicule actif mis à jour.")
        else:
            messages.error(request, "Véhicule inconnu.")
        return redirect("AddTeslaAccount")
    else:
        app_form = TeslaAppSettingsForm(instance=app_settings) if app_settings else TeslaAppSettingsForm()

    pub_pem = None
    pub_path = None
    try:
        ensure_key_pair()
        pub_pem = public_key_pem_text()
        pub_path = str(
            __import__("matesla.TeslaPartner", fromlist=["PUBLIC_KEY_PATH"]).PUBLIC_KEY_PATH
        )
    except Exception:
        pass

    domain = (app_settings.partner_domain if app_settings else "") or ""
    return render(
        request,
        "matesla/AddTeslaAccount.html",
        {
            "app_form": app_form,
            "app_configured": TeslaAppSettings.is_configured(),
            "tesla_token": token,
            "vehicles": vehicles,
            "active_vehicle": active,
            "partner_registered": bool(app_settings and app_settings.partner_registered),
            "partner_domain": domain,
            "public_key_pem": pub_pem,
            "public_key_path": pub_path,
            "public_key_url": public_key_url(domain) if domain else None,
            "oauth_error": request.session.pop("tesla_oauth_error", None),
        },
    )


@never_cache
@require_GET
def view_tesla_oauth_start(request):
    user = get_user(request)
    if not user.is_authenticated:
        return redirect("login")
    if not TeslaAppSettings.is_configured():
        messages.error(request, "Configure d'abord le Client ID et le Client Secret.")
        return redirect("AddTeslaAccount")

    TeslaOAuthPending.purge_expired()
    state = new_oauth_state()
    # Persist state→user in DB: the Django session often dies on the way back
    # from auth.tesla.com (external redirect), which previously sent users to /login.
    TeslaOAuthPending.objects.create(state=state, user=user)
    request.session["tesla_oauth_state"] = state
    request.session["tesla_oauth_user_id"] = user.id
    request.session.modified = True
    request.session.save()

    try:
        return redirect(build_authorize_url(state))
    except TeslaOAuthError as exc:
        TeslaOAuthPending.objects.filter(state=state).delete()
        messages.error(request, str(exc))
        return redirect("AddTeslaAccount")


@never_cache
@require_GET
def view_tesla_oauth_callback(request):
    """
    Registered on Tesla as: http://localhost:8001/oauth/callback
    Must stay outside i18n URL prefixes.
    """
    error = request.GET.get("error")
    if error:
        request.session["tesla_oauth_error"] = (
            f"Tesla a refusé l'autorisation: {error} "
            f"({request.GET.get('error_description', '')})"
        )
        return redirect("AddTeslaAccount")

    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        request.session["tesla_oauth_error"] = (
            "Callback OAuth invalide (code ou state manquant). Réessaie."
        )
        return redirect("AddTeslaAccount")

    # Prefer DB state (survives lost session cookie); fall back to session.
    pending = (
        TeslaOAuthPending.objects.select_related("user")
        .filter(state=state)
        .first()
    )
    session_state = request.session.pop("tesla_oauth_state", None)
    session_user_id = request.session.pop("tesla_oauth_user_id", None)

    if pending is None and (not session_state or state != session_state):
        request.session["tesla_oauth_error"] = (
            "Callback OAuth invalide (state inconnu ou expiré). "
            "Reconnecte-toi à matesla puis relance « Se connecter avec Tesla »."
        )
        return redirect("login")

    if pending is not None:
        user = pending.user
        pending.delete()
    else:
        User = get_user_model()
        try:
            user = User.objects.get(pk=session_user_id)
        except User.DoesNotExist:
            request.session["tesla_oauth_error"] = "Utilisateur OAuth introuvable."
            return redirect("login")

    # Re-establish Django session after the Tesla hop (often required).
    if not request.user.is_authenticated or request.user.pk != user.pk:
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    try:
        token_payload = exchange_code_for_tokens(code)
        apply_token_response(user, token_payload)
        try:
            vehicles_payload = GetVehicles(token_payload["access_token"])
        except TeslaFleetApiError as exc:
            messages.error(request, str(exc))
            return redirect("AddTeslaAccount")
        vehicles = sync_vehicles_from_api(user, vehicles_payload)
        if not vehicles:
            messages.warning(
                request,
                "Connecté à Tesla, mais aucun véhicule trouvé sur le compte.",
            )
            return redirect("NoTeslaVehicules")
        primary = next((v for v in vehicles if v.is_primary), vehicles[0])
        set_active_vehicle(request, user, primary.api_id)
        names = ", ".join(v.label for v in vehicles)
        messages.success(
            request,
            f"Tesla connecté — {len(vehicles)} véhicule(s) : {names}. "
            f"Actif : {primary.label}.",
        )
        return redirect("tesla_status")
    except TeslaOAuthError as exc:
        messages.error(request, str(exc))
        return redirect("AddTeslaAccount")
    except Exception as exc:
        traceback.print_exc()
        messages.error(request, f"Erreur inattendue: {exc}")
        return redirect("AddTeslaAccount")


@never_cache
@require_http_methods(["POST"])
def view_select_vehicle(request):
    """Switch active vehicle (status page dropdown)."""
    user = get_user(request)
    if not user.is_authenticated:
        return redirect("login")
    api_id = request.POST.get("vehicle_api_id")
    vehicle = set_active_vehicle(request, user, api_id)
    if vehicle:
        messages.success(request, f"Véhicule actif : {vehicle.label}")
    else:
        messages.error(request, "Véhicule inconnu.")
    return redirect("tesla_status")


# Start sentry
@never_cache
def view_sentry_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', True, request=request))


# Stop sentry
@never_cache
def view_sentry_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_sentry_mode', False, request=request))


# Start valet mode
@never_cache
def view_valet_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', True, request=request))


# Stop valet mode
@never_cache
def view_valet_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'set_valet_mode', False, request=request))


# Open charge port
@never_cache
def view_chargeport_open(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_open', request=request))


# Close charge port
@never_cache
def view_chargeport_close(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_port_door_close', request=request))


# Start charge
@never_cache
def view_charge_start(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_start', request=request))


# Stop charge
@never_cache
def view_charge_stop(request):
    return singleAction(request, lambda request, user: executeCommand(user, 'charge_stop', request=request))


# Start install of software update, with a 2 minutes timeout, as the car propose
@never_cache
def view_install_software_update(request):
    return singleAction(request,
                        lambda request, user: executeCommand(user, 'schedule_software_update', None, 'offset_sec', 120, request=request))


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
            ActivateRemoteStartDrive(form.cleaned_data["TeslaPassword"], user, request=request)
            return redirect("tesla_status")
    # if a GET (or any other method) we'll create a blank form
    else:
        form = RemoteStartDriveForm(initial={'TeslaPassword': ''})
    return render(request, 'matesla/getRemote_start_drivePassword.html', {'form': form})
