import traceback

import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from .BatteryDegradation import ComputeBatteryDegradation
from .models.AddressFromLatLong import AddressFromLatLong, GetAddressFromLatLong
from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaToken import TeslaToken, TeslaVehicle
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo
from .models.TeslaAppSettings import TeslaAppSettings
from matesla.TeslaState import TeslaState
from matesla.GetProxyToUse import GetProxyToUse
from matesla.TeslaOAuth import ensure_fresh_access_token, TeslaOAuthError

SESSION_ACTIVE_VEHICLE_KEY = "active_tesla_vehicle_api_id"


def fleet_api_base() -> str:
    app = TeslaAppSettings.get_solo()
    if app and app.api_base:
        return app.api_base.rstrip("/")
    return "https://fleet-api.prd.eu.vn.cloud.tesla.com"


def api_url(path: str) -> str:
    """Build absolute Fleet API URL. path starts with /api/..."""
    if not path.startswith("/"):
        path = "/" + path
    return fleet_api_base() + path


class TeslaServerException(Exception):
    pass


class TeslaAuthenticationException(Exception):
    pass


# tesla did refuse our token, yhis occurs when password has been changed by user
class TeslaUnauthorisedException(Exception):
    pass



class TeslaNoUserException(Exception):
    pass


class TeslaNoVehiculeException(Exception):
    pass


class TeslaIsAsleepException(Exception):
    pass


class TeslaFleetLimitException(Exception):
    """
    Fleet account temporarily disabled for usage/billing limits
    (e.g. HTTP 403 account disabled: EXCEEDED_LIMIT).
    Not the same as the car being asleep.
    """

    def __init__(self, message=None, status_code=None, body=None):
        super().__init__(
            message
            or "Tesla Fleet API free credit / usage limit exceeded"
        )
        self.status_code = status_code
        self.body = body


class TeslaFleetApiError(Exception):
    """Fleet API call failed; carries status + body for UI."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def is_fleet_limit_response(status_code, body: str | None) -> bool:
    """True when Fleet returns account disabled / EXCEEDED_LIMIT."""
    if status_code not in (402, 403, 429):
        return False
    text = (body or "").lower()
    return (
        "exceeded_limit" in text
        or "account disabled" in text
        or "payment" in text
        or "usage limit" in text
        or "credit" in text
    )


# Return the list of vehicles; raise TeslaFleetApiError with details on failure
def GetVehicles(access_token):
    api_call_headers = {"Authorization": "Bearer " + access_token}
    api_call_response = requests.get(
        api_url("/api/1/vehicles"),
        proxies=GetProxyToUse(),
        headers=api_call_headers,
        verify=True,
        timeout=60,
    )
    if api_call_response is None:
        raise TeslaFleetApiError("Pas de réponse de l'API Fleet (vehicles).")
    if api_call_response.status_code != 200:
        body = api_call_response.text[:1200]
        hint = ""
        if api_call_response.status_code == 412 and "registered" in body.lower():
            hint = (
                " → L'application MyRobotCar n'est pas encore « partner registered » "
                "dans la région EU. Il faut un domaine HTTPS public + clé publique "
                "(étape 1.b sur la page de connexion)."
            )
        raise TeslaFleetApiError(
            f"GET /vehicles → HTTP {api_call_response.status_code}: {body}{hint}",
            status_code=api_call_response.status_code,
            body=body,
        )
    return json.loads(api_call_response.text)


def resolve_active_vehicle(user, request=None) -> TeslaVehicle | None:
    """
    Pick the vehicle to control/display:
    1) session selection (if still valid for this user)
    2) is_primary flag
    3) first vehicle by ordering
    """
    queryset = TeslaVehicle.objects.filter(user=user)
    if not queryset.exists():
        return None

    if request is not None:
        selected_id = request.session.get(SESSION_ACTIVE_VEHICLE_KEY)
        if selected_id:
            vehicle = queryset.filter(api_id=str(selected_id)).first()
            if vehicle:
                return vehicle

    primary = queryset.filter(is_primary=True).first()
    if primary:
        return primary
    return queryset.first()


def set_active_vehicle(
    request, user, api_id: str, *, persist_primary: bool = True
) -> TeslaVehicle | None:
    """
    Select active vehicle for this browser session.

    persist_primary=False: only session (Tailscale guests) — do not rewrite
    is_primary used by capture / household default.
    """
    vehicle = TeslaVehicle.objects.filter(user=user, api_id=str(api_id)).first()
    if not vehicle:
        return None
    request.session[SESSION_ACTIVE_VEHICLE_KEY] = vehicle.api_id
    if persist_primary:
        # Also mark as primary so management commands / no-session paths use it
        TeslaVehicle.objects.filter(user=user, is_primary=True).update(is_primary=False)
        vehicle.is_primary = True
        vehicle.save(update_fields=["is_primary"])
    return vehicle


def list_user_vehicles(user):
    return list(TeslaVehicle.objects.filter(user=user))


# return token + active vehicle attached as token.vehicle / token.vehicle_id
def Connect(user, request=None) -> TeslaToken:
    try:
        teslatoken = TeslaToken.objects.get(user_id=user.id)
    except ObjectDoesNotExist:
        raise TeslaNoUserException()
    try:
        teslatoken = ensure_fresh_access_token(teslatoken)
    except TeslaOAuthError:
        raise TeslaUnauthorisedException()

    vehicle = resolve_active_vehicle(user, request)
    if vehicle is None:
        raise TeslaNoVehiculeException()

    # Convenience attributes used by the rest of the app
    teslatoken.vehicle = vehicle
    teslatoken.vehicle_id = vehicle.api_id
    teslatoken.vehicle_vin = vehicle.vin
    teslatoken.display_name = vehicle.display_name
    return teslatoken


# ---------------------------------------------------------------------------
# Billing policy (no credit card / free tier)
# ---------------------------------------------------------------------------
# Fleet API is pay-per-use. This personal install has no payment method, so we
# stay within the monthly free credit and never call expensive endpoints:
#   - wake_up …………… NEVER (use the official Tesla app instead)
#   - vehicle_data … skip only when list state is explicitly "asleep"
#     (API "offline" is unreliable vs the Tesla app — still try vehicle_data)
#   - commands ……… only on explicit user action (status page buttons)
# Auth/token endpoints are not billed.
# See: https://developer.tesla.com/docs/fleet-api/billing-and-limits
# ---------------------------------------------------------------------------


def fleet_http_error_reason(status_code, body: str | None, *, what: str = "API") -> str:
    """Short human-readable reason for a failed Fleet HTTP response (FR, for ops logs)."""
    text = (body or "").strip()
    low = text.lower()
    if is_fleet_limit_response(status_code, text):
        return (
            f"{what}: limite d’usage / crédit Fleet dépassé "
            f"(HTTP {status_code}, EXCEEDED_LIMIT ou compte désactivé)"
        )
    if status_code == 401:
        return f"{what}: non autorisé (HTTP 401) — token expiré ou révoqué"
    if status_code == 403:
        return f"{what}: accès refusé (HTTP 403) — {text[:180] or 'forbidden'}"
    if status_code == 404:
        return f"{what}: introuvable (HTTP 404)"
    if status_code == 408:
        return f"{what}: timeout véhicule (HTTP 408) — souvent endormi / hors ligne"
    if status_code == 429:
        return f"{what}: trop de requêtes (HTTP 429 rate limit)"
    if status_code is not None and status_code >= 500:
        return f"{what}: erreur serveur Tesla (HTTP {status_code})"
    if status_code is not None:
        snippet = text[:180].replace("\n", " ") if text else ""
        extra = f" — {snippet}" if snippet else ""
        return f"{what}: échec HTTP {status_code}{extra}"
    return f"{what}: pas de réponse HTTP"


def get_vehicles_list_payload(access_token) -> dict | None:
    """
    GET /api/1/vehicles — cheap-ish list with connectivity state per car.

    Raises TeslaFleetLimitException when the account is disabled for usage limits.
    Returns None on other non-200 / network failures (caller may still try vehicle_data).
    """
    payload, _reason = fetch_vehicles_list(access_token)
    return payload


def fetch_vehicles_list(access_token) -> tuple[dict | None, str | None]:
    """
    GET /api/1/vehicles with diagnostics.

    Returns (payload, None) on success.
    Raises TeslaFleetLimitException on usage/billing disable.
    Returns (None, reason_fr) on other failures.
    """
    try:
        resp = requests.get(
            api_url("/api/1/vehicles"),
            proxies=GetProxyToUse(),
            headers={"Authorization": "Bearer " + access_token},
            verify=True,
            timeout=60,
        )
    except requests.exceptions.Timeout:
        return None, "Liste véhicules: timeout réseau vers Fleet API"
    except requests.exceptions.ConnectionError as exc:
        return None, f"Liste véhicules: erreur de connexion ({exc.__class__.__name__})"
    except requests.exceptions.RequestException as exc:
        return None, f"Liste véhicules: erreur réseau ({exc})"

    if resp is None:
        return None, "Liste véhicules: pas de réponse"
    if resp.status_code != 200:
        if is_fleet_limit_response(resp.status_code, resp.text):
            raise TeslaFleetLimitException(
                status_code=resp.status_code, body=resp.text[:500]
            )
        return None, fleet_http_error_reason(
            resp.status_code, resp.text, what="Liste véhicules"
        )
    try:
        return json.loads(resp.text), None
    except json.JSONDecodeError:
        return None, "Liste véhicules: JSON invalide dans la réponse Tesla"


def refresh_vehicle_states_from_list(user, list_payload: dict | None) -> None:
    """Update TeslaVehicle.state from a /vehicles list response."""
    if not list_payload:
        return
    for vehicle_entry in list_payload.get("response") or []:
        api_id = str(vehicle_entry.get("id") or "")
        if not api_id:
            continue
        TeslaVehicle.objects.filter(user=user, api_id=api_id).update(
            state=vehicle_entry.get("state") or "",
            display_name=vehicle_entry.get("display_name") or "",
            vin=vehicle_entry.get("vin") or "",
        )


def get_vehicle_connectivity(access_token, vehicle_id, list_payload=None) -> str | None:
    """
    Connectivity state for one vehicle from GET /vehicles.
    Returns state string (online / asleep / offline / …) or None.
    """
    payload = list_payload if list_payload is not None else get_vehicles_list_payload(access_token)
    if not payload:
        return None
    for vehicle_entry in payload.get("response") or []:
        if str(vehicle_entry.get("id")) == str(vehicle_id) or str(
            vehicle_entry.get("vehicle_id")
        ) == str(vehicle_id):
            return vehicle_entry.get("state")
    return None


# Save data history
def SaveDataHistory(teslaState):
    try:
        context = (teslaState.vehicle_state or {}).get("response") or {}
        vehicle_config = context.get("vehicle_config") or {}
        vehicle_state = context.get("vehicle_state") or {}
        charge_state = context.get("charge_state") or {}
        if not teslaState.vin:
            return
        # Firmware updates
        car_version = vehicle_state.get("car_version")
        car_type = vehicle_config.get("car_type")
        if car_version and car_type:
            toSave = TeslaFirmwareHistory()
            toSave.SaveIfDontExistsYet(teslaState.vin, car_version, car_type)
        # Car infos
        toSave = TeslaCarInfo()
        toSave = toSave.SaveIfDontExistsYet(teslaState.vin, context)
        # if we don't have epa range yet, this will force its recomputation
        if toSave and toSave.EPARange is None:
            battery_range_miles = charge_state.get("battery_range")
            usable_battery_level = charge_state.get("usable_battery_level")
            odometer_miles = vehicle_state.get("odometer")
            if (
                battery_range_miles is not None
                and usable_battery_level is not None
                and odometer_miles is not None
            ):
                ComputeBatteryDegradation(
                    battery_range_miles,
                    usable_battery_level,
                    teslaState.vin,
                    odometer_miles,
                )
        # Car variable infos
        toSave = TeslaCarDataSnapshot()
        toSave.SaveIfDontExistsYet(teslaState.vin, context)
    # during firmware update, some fields will be null-->don't crash, just ignore save of invalid data
    except (IntegrityError, KeyError, TypeError, AttributeError):
        traceback.print_exc()
        return


# Fleet vehicle_data: without this query, GPS is often omitted and display_name may be null.
VEHICLE_DATA_ENDPOINTS = (
    "charge_state;climate_state;drive_state;location_data;"
    "vehicle_config;vehicle_state;gui_settings"
)


# returns params as TeslaState
def ParamsConnectedTesla(user, request=None):
    teslaatoken = Connect(user, request)

    ret = TeslaState()
    api_call_headers = {"Authorization": "Bearer " + teslaatoken.access_token}

    # List first: refresh states. Only hard-skip vehicle_data when explicitly "asleep".
    # "offline" from Fleet is often wrong vs the Tesla app — still try vehicle_data.
    # Do not treat list failure as "asleep" (that hid EXCEEDED_LIMIT / billing errors).
    list_payload = get_vehicles_list_payload(teslaatoken.access_token)
    try:
        refresh_vehicle_states_from_list(user, list_payload)
    except Exception:
        traceback.print_exc()

    state = get_vehicle_connectivity(
        teslaatoken.access_token, teslaatoken.vehicle_id, list_payload=list_payload
    )
    if state == "asleep":
        try:
            if getattr(teslaatoken, "vehicle", None):
                TeslaVehicle.objects.filter(pk=teslaatoken.vehicle.pk).update(state=state)
        except Exception:
            pass
        raise TeslaIsAsleepException

    api_call_response = requests.get(
        api_url(f"/api/1/vehicles/{teslaatoken.vehicle_id}/vehicle_data"),
        params={"endpoints": VEHICLE_DATA_ENDPOINTS},
        proxies=GetProxyToUse(),
        headers=api_call_headers,
        verify=True,
        timeout=60,
    )
    body_text = api_call_response.text if api_call_response is not None else ""
    if api_call_response is not None and is_fleet_limit_response(
        api_call_response.status_code, body_text
    ):
        raise TeslaFleetLimitException(
            status_code=api_call_response.status_code, body=body_text[:500]
        )
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        # Only map known vehicle-unavailable cases to the offline hub.
        # list state None (list call failed) must not look like "asleep".
        if state in ("offline", "asleep") and (
            api_call_response is None or api_call_response.status_code >= 400
        ):
            raise TeslaIsAsleepException
        raise TeslaServerException()

    payload = json.loads(api_call_response.text)
    ret.vehicle_state = payload
    context = payload.get("response") or {}
    if not context:
        raise TeslaServerException()

    # Fleet often omits display_name on vehicle_data — fall back to our DB cache
    ret.vin = context.get("vin") or getattr(teslaatoken, "vehicle_vin", None) or ""
    ret.name = (
        context.get("display_name")
        or getattr(teslaatoken, "display_name", None)
        or (teslaatoken.vehicle.display_name if getattr(teslaatoken, "vehicle", None) else None)
        or ret.vin
        or "Tesla"
    )
    # Ensure template always has a name key
    context["display_name"] = ret.name
    context["vin"] = ret.vin

    # Successful vehicle_data means we have live data. Fleet sometimes omits
    # top-level state; do not drop telemetry when the field is missing.
    top_state = context.get("state")
    ret.isOnline = top_state in (None, "", "online")
    if not ret.isOnline:
        return ret

    charge_state = context.get("charge_state") or {}
    vehicle_state = context.get("vehicle_state") or {}
    drive_state = context.get("drive_state") or {}

    battery_range = charge_state.get("battery_range")
    if battery_range is not None:
        ret.batteryrange = battery_range * 1.609344
    else:
        ret.batteryrange = 0.0

    odometer = vehicle_state.get("odometer")
    usable_battery_level = charge_state.get("usable_battery_level")
    if (
        battery_range is not None
        and usable_battery_level is not None
        and odometer is not None
        and ret.vin
    ):
        ret.batterydegradation, ret.NumberCycles, ret.EPARangeMiles = (
            ComputeBatteryDegradation(
                battery_range, usable_battery_level, ret.vin, odometer
            )
        )
    else:
        ret.batterydegradation, ret.NumberCycles, ret.EPARangeMiles = None, None, None

    if odometer is not None:
        ret.OdometerInKm = odometer * 1.609344
    else:
        ret.OdometerInKm = 0.0

    lat = drive_state.get("latitude")
    lon = drive_state.get("longitude")
    if lat is not None and lon is not None:
        ret.location = GetAddressFromLatLong(lat, lon)
        context["latitude"] = lat
        context["longitude"] = lon
    else:
        ret.location = ""
        context["latitude"] = None
        context["longitude"] = None

    # Keep nested dicts present for the status page merge
    context.setdefault("charge_state", charge_state)
    context.setdefault("climate_state", context.get("climate_state") or {})
    context.setdefault("drive_state", drive_state)
    context.setdefault("vehicle_config", context.get("vehicle_config") or {})
    context.setdefault("vehicle_state", vehicle_state)
    payload["response"] = context

    try:
        SaveDataHistory(ret)
    except Exception:
        traceback.print_exc()
    return ret
