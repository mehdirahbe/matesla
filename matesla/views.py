import re
import traceback

from django.contrib import messages
from django.contrib.auth import get_user, get_user_model, login
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.template import loader
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from matesla.units import (
    KM_TO_MILES,
    format_distance,
    format_epa_range,
    format_number,
    format_speed_from_mph,
    get_distance_unit,
    miles_to_display,
    normalize_unit,
    redirect_url_for_unit,
    set_distance_unit_cookie,
)

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
    key_pair_exists,
    public_key_url,
    check_public_key_reachable,
    register_partner_account,
)
from .forms import TeslaAppSettingsForm, TeslaPartnerDomainForm
from .models.TeslaAppSettings import TeslaAppSettings
from .models.TeslaOAuthPending import TeslaOAuthPending
from .models.TeslaToken import TeslaToken, TeslaVehicle
from .models.VinHash import HashTheVin


@require_http_methods(["GET", "POST"])
@never_cache
def view_set_distance_unit(request):
    """
    Persist km/mi preference in a cookie and redirect back.

    GET ?unit=km|mi&next=/path  (also accepts POST unit=)

    Redirect URL is cache-busted (`_du=mi`) so the browser cannot serve an HTML
    page still rendered in the previous unit.
    """
    raw = request.POST.get("unit") or request.GET.get("unit")
    unit = normalize_unit(raw)
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    # Relative paths are fine; block open redirects to other hosts.
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = "/"
    target = redirect_url_for_unit(next_url, unit)
    response = redirect(target)
    # Never let intermediaries or the browser keep the redirect/page pair stale.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    set_distance_unit_cookie(response, unit, secure=request.is_secure())
    return response


@csrf_exempt
@never_cache
@require_http_methods(["GET", "POST"])
def view_internal_capture(request):
    """
    Run Tesla capture inside the web process (same SQLite connection).

    Prefer this from cron instead of `manage.py TakeTeslaCarDataSnapshot`, which
    starts a second Python process and can lock SQLite while gunicorn serves.

    If a capture is already running (multi-threaded gunicorn + cron overlap),
    returns immediately with skipped_already_running=true (HTTP 200).

    No auth: intended for localhost-only installs (not exposed to the internet).
    """
    from matesla.capture import capture_all_online_vehicles

    stats = capture_all_online_vehicles()
    return JsonResponse(stats)




def view_teslacss(request):
    return HttpResponse(
        loader.get_template('matesla/tesla.css').render({}, request))


@never_cache
def home(request):
    """
    Site landing: day map for the active vehicle.

    Uses only local DB (session / primary vehicle / VIN hash) — never Fleet
    vehicle_data. Live status remains at matesla/status when the user opts in.
    """
    user = _acting_user_or_login(request)
    if user is None:
        return redirect("login")

    vehicle = resolve_active_vehicle(user, request)
    if vehicle is None or not (vehicle.vin or "").strip():
        if not TeslaToken.objects.filter(user_id=user.id).exists():
            # Local setup: send to Tesla account. Read-only hosts block this URL;
            # NoTeslaVehicules stays reachable for guests.
            from mysite.writable_access import is_writable_request

            if is_writable_request(request):
                return redirect("AddTeslaAccount")
        return redirect("NoTeslaVehicules")

    hashed = HashTheVin(vehicle.vin)
    if not hashed:
        return redirect("NoTeslaVehicules")
    return redirect("PersoDayMap", hashedVin=hashed)


# Vehicle is sleeping — matesla never wakes (no billable wake_up).
@never_cache
def asleep(request):
    # Dead-end page removed: offline mode is handled by the vehicle hub (status).
    return redirect("tesla_status")


@never_cache
def view_TeslaServerError(request):
    return singleAction(request, lambda request, user: HttpResponse(
        loader.get_template('matesla/TeslaServerError.html').render({}, request)), True)



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

# Fleet vehicle_config.exterior_color → paint option (names are more reliable than option_codes)
_EXTERIOR_NAME_TO_CODE = {
    "solidblack": "PBSB",
    "black": "PBSB",
    "obsidianblack": "PMBL",
    "pearlwhite": "PPSW",
    "pearlwhitemulticoat": "PPSW",
    "white": "PPSW",
    "deepblue": "PPSB",
    "deepbluemetallic": "PPSB",
    "blue": "PPSB",
    "steelgrey": "PMNG",
    "midnightsilver": "PMNG",
    "midnightsilvermetallic": "PMNG",
    "grey": "PMNG",
    "gray": "PMNG",
    "silver": "PMSS",
    "silvermetallic": "PMSS",
    "redmulticoat": "PPMR",
    "red": "PPMR",
}


def returnColorFronContext(context):
    """Pick compositor paint code for the status car image.

    Prefer Fleet exterior_color names (e.g. SolidBlack → PBSB). option_codes are
    often missing or wrong on Fleet API; only used as a fallback.
    """
    exterior = context.get("exterior_color") or ""
    key = exterior.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if key in _EXTERIOR_NAME_TO_CODE:
        return _EXTERIOR_NAME_TO_CODE[key]

    # Nested vehicle_config if not flattened yet
    vehicle_config = context.get("vehicle_config") or {}
    if isinstance(vehicle_config, dict):
        exterior = vehicle_config.get("exterior_color") or ""
        key = exterior.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
        if key in _EXTERIOR_NAME_TO_CODE:
            return _EXTERIOR_NAME_TO_CODE[key]

    option_codes = context.get("option_codes") or ""
    if option_codes:
        for code in option_codes.replace("$", "").split(","):
            code = code.strip().upper()
            if code in ValidColorCodes:
                return code
    return "PPSW"


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
            "api_id": vehicle.api_id,
            "vin": vehicle.vin,
            "display_name": vehicle.display_name,
            "label": vehicle.label,
            "state": vehicle.state,
            "is_primary": vehicle.is_primary,
        }
        for vehicle in vehicles
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

    unit = get_distance_unit(request)
    # Tesla API distances are miles; TeslaConnect may already convert to km.
    # Prefer raw miles when available so unit preference is applied once.
    range_miles = getattr(params, "batteryrange_miles", None)
    if range_miles is None and params.batteryrange:
        # Legacy: batteryrange was always km after TeslaConnect conversion
        range_miles = float(params.batteryrange) * KM_TO_MILES
    odo_miles = getattr(params, "odometer_miles", None)
    if odo_miles is None and params.OdometerInKm:
        odo_miles = float(params.OdometerInKm) * KM_TO_MILES

    context["batteryrange"] = format_number(miles_to_display(range_miles, unit), 0) or "0"
    context["batteryrange_display"] = format_distance(
        range_miles, unit, decimals=0, with_unit=True
    )
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
    context["EPARange_display"] = format_epa_range(params.EPARangeMiles, unit, decimals=0)
    vin = context.get("vin") or ""
    context["hashedVin"] = HashTheVin(vin) if vin else ""
    from matesla.VinAnalysis import GetVinDecoderUrl

    context["vin_decoder_url"] = GetVinDecoderUrl(vin)
    context["location"] = params.location or ""
    context["OdometerInKm"] = format_number(miles_to_display(odo_miles, unit), 0) or "0"
    context["Odometer_display"] = format_distance(
        odo_miles, unit, decimals=0, with_unit=True
    )
    # Tesla speed is mph
    speed_raw = context.get("speed")
    try:
        speed_mph = float(speed_raw) if speed_raw is not None else None
    except (TypeError, ValueError):
        speed_mph = None
    context["speed_display"] = format_speed_from_mph(
        speed_mph, unit, decimals=0, with_unit=True
    )

    try:
        context["colorcode"] = returnColorFronContext(context)
    except Exception:
        # SolidBlack is more common than white as a safe fallback for missing data
        context["colorcode"] = "PBSB"

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


def _vehicle_list_context(user, request):
    """Shared multi-vehicle selector context (JSON-serializable vehicle dicts)."""
    vehicles = list_user_vehicles(user)
    active = resolve_active_vehicle(user, request)
    return {
        "user_vehicles": [
            {
                "api_id": vehicle.api_id,
                "vin": vehicle.vin,
                "display_name": vehicle.display_name,
                "label": vehicle.label,
                "state": vehicle.state,
                "is_primary": vehicle.is_primary,
            }
            for vehicle in vehicles
        ],
        "active_vehicle_api_id": active.api_id if active else None,
        "active_vehicle_label": active.label if active else None,
        "active_vehicle": active,
    }


def Preparestatus(request, user):
    """Online hub: live controls + status."""
    context = PreparestatusDictionary(request, user)
    context["hub_mode"] = "live"
    template = loader.get_template("matesla/vehicle_hub.html")
    return HttpResponse(template.render(context, request))


def PrepareOfflineHub(request, user, *, hub_reason="asleep"):
    """
    Offline hub: same vehicle selector, personal stats body (no live API).

    hub_reason:
      - asleep: car resting / unreachable (default)
      - fleet_limit: Fleet free credit / usage limit (not the car)
    """
    from personalstats.views import GetTitleForFieldDico

    sel = _vehicle_list_context(user, request)
    active = sel["active_vehicle"]
    if active is None:
        raise TeslaNoVehiculeException()

    vin = active.vin or ""
    hashed = HashTheVin(vin) if vin else ""
    from personalstats.views import resolve_stats_period

    context = {
        **sel,
        "hub_mode": "offline",
        "hub_reason": hub_reason,
        "hashedVin": hashed,
        "display_name": active.display_name or active.label,
        "stats_period": resolve_stats_period(request),
    }
    context.update(GetTitleForFieldDico())
    template = loader.get_template("matesla/vehicle_hub.html")
    return HttpResponse(template.render(context, request))


def PreparestatusJson(request, user):
    context = PreparestatusDictionary(request, user)
    return JsonResponse(context)


# The status view — hub: live controls if online, personal stats if asleep / limited.
# Offline reasons are handled inside the callback so singleAction still owns login errors.
@never_cache
def status(request):
    def _hub(request, user):
        try:
            return Preparestatus(request, user)
        except TeslaIsAsleepException:
            return PrepareOfflineHub(request, user, hub_reason="asleep")
        except TeslaFleetLimitException:
            return PrepareOfflineHub(request, user, hub_reason="fleet_limit")

    return singleAction(request, _hub, True)


# The status json data
@never_cache
def statusJson(request):
    return singleActionJson(request, lambda request, user: PreparestatusJson(request, user))


'''Check login, and if fine call func. Return its output.
On tesla login error, return json error detail.
Never redirect.'''


def _acting_user_or_login(request):
    """
    Logged-in user, or household owner on Tailscale read-only.
    Local anonymous → None (caller redirects to login).
    """
    from mysite.writable_access import resolve_acting_user

    return resolve_acting_user(request)


def singleActionJson(request, func):
    user = _acting_user_or_login(request)
    if user is None:
        return JsonResponse({'error': 'not logged'})
    try:
        ret = func(request, user)
        return ret
    except TeslaIsAsleepException:
        return JsonResponse({'error': 'TeslaIsAsleepException'})
    except TeslaFleetLimitException:
        return JsonResponse({'error': 'TeslaFleetLimitException'})
    except TeslaNoUserException:
        return JsonResponse({'error': 'TeslaNoUserException'})
    except TeslaUnauthorisedException:
        return JsonResponse({'error': 'TeslaUnauthorisedException'})
    except TeslaAuthenticationException:
        return JsonResponse({'error': 'TeslaAuthenticationException'})
    except TeslaServerException:
        return JsonResponse({'error': 'TeslaServerException'})
    except TeslaNoVehiculeException:
        return JsonResponse({'error': 'TeslaNoVehiculeException'})
    except requests.exceptions.ConnectionError:
        return JsonResponse({'error': 'ConnectionError'})
    except Exception as exc:
        traceback.print_exc()
        return JsonResponse({'error': type(exc).__name__})
    return JsonResponse({'error': 'How did we arrive here?'})


'''Check login (or household owner on read-only remote), then call func.
On tesla login error, go to tesla credentials page (local only).'''


def singleAction(request, func, shouldReturnFunc=False):
    user = _acting_user_or_login(request)
    if user is None:
        return redirect('login')
    try:
        ret = func(request, user)
        if shouldReturnFunc == True:
            return ret
    except TeslaIsAsleepException:
        # Hub shows offline/stats mode — never trap the user on a dead-end page
        return redirect('tesla_status')
    except TeslaFleetLimitException:
        # Same hub as offline, with a fleet-limit banner (status view handles it)
        return redirect('tesla_status')
    except TeslaNoUserException:
        return redirect('AddTeslaAccount')
    except TeslaUnauthorisedException:
        return redirect('AddTeslaAccount')
    except TeslaAuthenticationException:
        return redirect('AddTeslaAccount')
    except TeslaServerException:
        return redirect('TeslaServerError')
    except TeslaNoVehiculeException:
        return redirect('NoTeslaVehicules')
    except requests.exceptions.ConnectionError:
        return redirect('ConnectionError')
    except Exception as exc:
        traceback.print_exc()
        return HttpResponse(type(exc).__name__)
    return redirect("tesla_status")



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
    app_form = None
    partner_domain_form = None
    action = request.POST.get("action") if request.method == "POST" else None

    if action == "save_app":
        form = TeslaAppSettingsForm(request.POST, instance=app_settings)
        if form.is_valid():
            was_configured = TeslaAppSettings.is_configured()
            before = None
            if app_settings:
                before = (
                    app_settings.client_id,
                    app_settings.client_secret,
                    app_settings.redirect_uri,
                    app_settings.api_base,
                )
            obj = form.save()
            after = (obj.client_id, obj.client_secret, obj.redirect_uri, obj.api_base)
            if was_configured and before == after:
                messages.info(
                    request,
                    _("Application settings unchanged (safe to save again anytime)."),
                )
            elif was_configured:
                messages.success(
                    request,
                    _(
                        "Developer credentials updated. "
                        "If you changed Client ID or secret, use Re-authorize."
                    ),
                )
            else:
                messages.success(request, _("Developer credentials saved."))
            return redirect("AddTeslaAccount")
        app_form = form
    elif action == "save_partner_domain":
        # Only update partner_domain — never touch credentials on this action.
        domain_form = TeslaPartnerDomainForm(request.POST)
        if not app_settings:
            messages.error(
                request,
                _("Save Client ID and secret in step 1 before setting a partner domain."),
            )
            return redirect("AddTeslaAccount")
        if domain_form.is_valid():
            domain = domain_form.cleaned_data["partner_domain"]
            previous = (app_settings.partner_domain or "").strip().lower()
            if domain == previous:
                messages.info(
                    request,
                    _("Partner domain unchanged: %(domain)s") % {"domain": domain or "—"},
                )
                return redirect("AddTeslaAccount")
            app_settings.partner_domain = domain
            update_fields = ["partner_domain", "updated_at"]
            # Registration is per domain — changing it invalidates the flag.
            if app_settings.partner_registered and domain != previous:
                app_settings.partner_registered = False
                update_fields.append("partner_registered")
                app_settings.save(update_fields=update_fields)
                if domain:
                    messages.warning(
                        request,
                        _(
                            "Partner domain changed to %(domain)s. "
                            "Previous registration no longer applies — "
                            "verify the public key, then register again."
                        )
                        % {"domain": domain},
                    )
                else:
                    messages.info(request, _("Partner domain cleared."))
                return redirect("AddTeslaAccount")
            app_settings.save(update_fields=update_fields)
            if domain:
                messages.success(
                    request,
                    _("Partner domain saved: %(domain)s") % {"domain": domain},
                )
            else:
                messages.info(request, _("Partner domain cleared."))
            return redirect("AddTeslaAccount")
        partner_domain_form = domain_form
    elif action == "generate_keys":
        # Safe: never overwrites an existing pair (would break published HTTPS key).
        priv, pub, created = ensure_key_pair()
        if created:
            messages.success(
                request,
                _(
                    "EC key pair created. Publish the public key to your partner domain "
                    "(file: %(pub)s). Private key stays local: %(priv)s."
                )
                % {"pub": pub, "priv": priv},
            )
        else:
            messages.info(
                request,
                _(
                    "EC key pair already exists — left unchanged. "
                    "Regenerating would invalidate the public key on HTTPS; "
                    "delete the files under tesla_keys/ only if you really mean to."
                ),
            )
        return redirect("AddTeslaAccount")
    elif action == "check_public_key":
        domain = (request.POST.get("partner_domain") or "").strip()
        if app_settings and not domain:
            domain = app_settings.partner_domain
        if not domain:
            messages.error(request, _("Set a partner domain first."))
        else:
            ok, msg = check_public_key_reachable(domain)
            (messages.success if ok else messages.error)(request, msg)
        return redirect("AddTeslaAccount")
    elif action == "register_partner":
        domain = (request.POST.get("partner_domain") or "").strip()
        if app_settings and not domain:
            domain = app_settings.partner_domain
        already = bool(
            app_settings
            and app_settings.partner_registered
            and (app_settings.partner_domain or "").strip().lower()
            == (domain or "").strip().lower()
        )
        try:
            if app_settings and domain and domain != app_settings.partner_domain:
                app_settings.partner_domain = domain
                app_settings.partner_registered = False
                app_settings.save(
                    update_fields=["partner_domain", "partner_registered", "updated_at"]
                )
            result = register_partner_account(domain)
            if already:
                messages.success(
                    request,
                    _(
                        "Partner registration reconfirmed for « %(domain)s ». "
                        "Safe to repeat. Detail: %(result)s"
                    )
                    % {"domain": domain, "result": result},
                )
            else:
                messages.success(
                    request,
                    _(
                        "Partner register OK for « %(domain)s ». "
                        "You can authorize Tesla / resync vehicles now. Detail: %(result)s"
                    )
                    % {"domain": domain, "result": result},
                )
        except TeslaPartnerError as exc:
            messages.error(request, str(exc))
        return redirect("AddTeslaAccount")
    elif action == "resync_vehicles":
        if not token:
            messages.error(request, _("No Tesla token — sign in first."))
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
                    _("%(count)s vehicle(s) synchronized. Active: %(label)s.")
                    % {"count": len(vehicles), "label": primary.label},
                )
                return redirect("tesla_status")
            messages.warning(request, _("No vehicles on the Tesla account."))
        except TeslaFleetApiError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            traceback.print_exc()
            messages.error(request, _("Resync failed: %(err)s") % {"err": exc})
        return redirect("AddTeslaAccount")
    elif action == "disconnect":
        TeslaToken.objects.filter(user_id=user.id).delete()
        TeslaVehicle.objects.filter(user=user).delete()
        request.session.pop(SESSION_ACTIVE_VEHICLE_KEY, None)
        messages.info(
            request,
            _("Tesla account disconnected (tokens and vehicles removed)."),
        )
        return redirect("AddTeslaAccount")

    if app_form is None:
        app_form = (
            TeslaAppSettingsForm(instance=app_settings)
            if app_settings
            else TeslaAppSettingsForm()
        )
    if partner_domain_form is None:
        # Prefill with the canonical public-key URL when a domain is known,
        # so the field is clearly editable as a full URL (not only a host).
        initial_partner = ""
        if app_settings and app_settings.partner_domain:
            initial_partner = public_key_url(app_settings.partner_domain)
        partner_domain_form = TeslaPartnerDomainForm(
            initial={"partner_domain": initial_partner}
        )

    # Create key pair on first visit only if missing — never rotates existing keys.
    try:
        ensure_key_pair()
    except Exception:
        pass

    domain = (app_settings.partner_domain if app_settings else "") or ""
    if partner_domain_form.is_bound and partner_domain_form.errors:
        domain = partner_domain_form.data.get("partner_domain") or domain

    redirect_uri = ""
    if app_settings and app_settings.redirect_uri:
        redirect_uri = app_settings.redirect_uri
    else:
        redirect_uri = (
            app_form["redirect_uri"].value()
            or app_form.fields["redirect_uri"].initial
            or ""
        )

    return render(
        request,
        "matesla/AddTeslaAccount.html",
        {
            "app_form": app_form,
            "partner_domain_form": partner_domain_form,
            "app_configured": TeslaAppSettings.is_configured(),
            "tesla_token": token,
            "vehicles": vehicles,
            "active_vehicle": active,
            "partner_registered": bool(app_settings and app_settings.partner_registered),
            "partner_domain": domain,
            "keys_ready": key_pair_exists(),
            "public_key_url": public_key_url(domain) if domain else None,
            "redirect_uri": redirect_uri,
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
        messages.error(request, _("Configure the Client ID and Client Secret first."))
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
        request.session["tesla_oauth_error"] = _(
            "Tesla denied authorization: %(error)s (%(desc)s)"
        ) % {
            "error": error,
            "desc": request.GET.get("error_description", ""),
        }
        return redirect("AddTeslaAccount")

    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        request.session["tesla_oauth_error"] = _(
            "Invalid OAuth callback (missing code or state). Please try again."
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
        request.session["tesla_oauth_error"] = _(
            "Invalid OAuth callback (unknown or expired state). "
            "Sign in to MaTesla again, then use “Sign in with Tesla”."
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
            request.session["tesla_oauth_error"] = _("OAuth user not found.")
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
                _("Connected to Tesla, but no vehicles found on the account."),
            )
            return redirect("NoTeslaVehicules")
        primary = next((v for v in vehicles if v.is_primary), vehicles[0])
        set_active_vehicle(request, user, primary.api_id)
        names = ", ".join(v.label for v in vehicles)
        messages.success(
            request,
            _("Tesla connected — %(count)s vehicle(s): %(names)s. Active: %(label)s.")
            % {
                "count": len(vehicles),
                "names": names,
                "label": primary.label,
            },
        )
        return redirect("tesla_status")
    except TeslaOAuthError as exc:
        messages.error(request, str(exc))
        return redirect("AddTeslaAccount")
    except Exception as exc:
        traceback.print_exc()
        messages.error(request, _("Unexpected error: %(err)s") % {"err": exc})
        return redirect("AddTeslaAccount")


@never_cache
@require_http_methods(["POST"])
def view_select_vehicle(request):
    """Switch active vehicle; stay on the same kind of page when possible.

    Personal pages (day map, stats, firmware) are keyed by hashedVin — after a
    switch we re-route to the same view for the newly selected vehicle.
    """
    from mysite.writable_access import is_writable_request, resolve_acting_user

    user = resolve_acting_user(request)
    if user is None:
        return redirect("login")
    api_id = request.POST.get("vehicle_api_id")
    # Guests on Tailscale: session only — do not change household is_primary
    vehicle = set_active_vehicle(
        request, user, api_id, persist_primary=is_writable_request(request)
    )
    if not vehicle:
        messages.error(request, _("Unknown vehicle."))
        return redirect("home")

    next_kind = (request.POST.get("next") or "").strip().lower()
    # Whitelist only — never open-redirect on user-supplied URLs
    if next_kind in (
        "daymap",
        "stats",
        "firmware",
        "drives",
        "dccharge",
        "polldetails",
    ) and vehicle.vin:
        from matesla.models.VinHash import HashTheVin

        hashed = HashTheVin(vehicle.vin)
        if hashed:
            if next_kind == "daymap":
                day = (request.POST.get("day") or "").strip()
                # Accept only ISO dates YYYY-MM-DD from the day map form
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
                    return redirect("PersoDayMapDay", hashedVin=hashed, day=day)
                return redirect("PersoDayMap", hashedVin=hashed)
            if next_kind == "stats":
                from personalstats.views import (
                    STATS_PERIOD_SESSION_KEY,
                    parse_stats_period,
                )
                from django.urls import reverse

                # Keep graph period (weeks) across vehicle switches.
                period = parse_stats_period(
                    request.POST.get("period"),
                    default=parse_stats_period(
                        request.session.get(STATS_PERIOD_SESSION_KEY)
                    ),
                )
                request.session[STATS_PERIOD_SESSION_KEY] = period
                url = reverse("PersoStats", kwargs={"hashedVin": hashed})
                return redirect(f"{url}?period={period}")
            if next_kind == "drives":
                from personalstats.views import (
                    STATS_PERIOD_SESSION_KEY,
                    parse_stats_period,
                    DRIVES_SORT_SPECS,
                    DRIVES_SORT_DEFAULT,
                )
                from django.urls import reverse

                period = parse_stats_period(
                    request.POST.get("period"),
                    default=parse_stats_period(
                        request.session.get(STATS_PERIOD_SESSION_KEY)
                    ),
                )
                request.session[STATS_PERIOD_SESSION_KEY] = period
                sort = (request.POST.get("sort") or DRIVES_SORT_DEFAULT).strip().lower()
                if sort not in DRIVES_SORT_SPECS:
                    sort = DRIVES_SORT_DEFAULT
                url = reverse("PersoDrives", kwargs={"hashedVin": hashed})
                return redirect(f"{url}?period={period}&sort={sort}")
            if next_kind == "firmware":
                return redirect("PersoStatsFirmwareHistory", hashedVin=hashed)
            if next_kind == "dccharge":
                from personalstats.views import (
                    STATS_PERIOD_SESSION_KEY,
                    parse_stats_period,
                )
                from django.urls import reverse

                period = parse_stats_period(
                    request.POST.get("period"),
                    default=parse_stats_period(
                        request.session.get(STATS_PERIOD_SESSION_KEY)
                    ),
                )
                request.session[STATS_PERIOD_SESSION_KEY] = period
                filt = (request.POST.get("filter") or "robust").strip().lower()
                if filt not in ("robust", "all"):
                    filt = "robust"
                envelope = (request.POST.get("envelope") or "p10_p90").strip().lower()
                if envelope not in ("p10_p90", "min_max"):
                    envelope = "p10_p90"
                url = reverse("PersoDCCharge", kwargs={"hashedVin": hashed})
                return redirect(
                    f"{url}?period={period}&filter={filt}&envelope={envelope}"
                )
            if next_kind == "polldetails":
                return redirect("PersoPollDetails", hashedVin=hashed)

    return redirect("tesla_status")


