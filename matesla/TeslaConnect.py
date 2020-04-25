import datetime

import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from geopy.geocoders import Nominatim

from matesla.models import TeslaToken, TeslaState, TeslaFirmwareHistory


class TeslaServerException(Exception):
    pass


class TeslaAuthenticationException(Exception):
    pass


# tesla did refuse our token, yhis occurs when password has been changed by user
class TeslaUnauthorisedException(Exception):
    pass


class TeslaCommandException(Exception):
    pass


class TeslaNoUserException(Exception):
    pass


class TeslaNoVehiculeException(Exception):
    pass


class TeslaIsAsleepException(Exception):
    pass


# tesla client id and secret which are everywhere on internet
client_id = '81527cff06843c8634fdc09e8ac0abefb46ac849f38fe1e431c2ef2106796384'
client_secret = 'c7257eb71a564034f9419ee651c7d0e5f7aa6bfbd18bafb5c5c033b093bb2fa3'


# Either return a valid TeslaToken of None if login did fail
def GetTokenFromLoginPW(teslalogin, teslapw):
    token_url = "https://owner-api.teslamotors.com/oauth/token"
    data = {'grant_type': 'password', 'client_id': client_id, 'client_secret': client_secret,
            'email': teslalogin, 'password': teslapw}
    access_token_response = requests.post(token_url, data=data, verify=True, allow_redirects=False)
    # Check if connection did fail
    if access_token_response is None or access_token_response.status_code != 200:
        return None
    tokens = json.loads(access_token_response.text)
    teslatoken = TeslaToken()
    teslatoken.access_token = tokens["access_token"]
    teslatoken.expires_in = int(tokens["expires_in"])
    teslatoken.created_at = int(tokens["created_at"])
    teslatoken.refresh_token = tokens["refresh_token"]
    teslatoken.vehicle_id = None
    return teslatoken


# Either return a valid TeslaToken of None if login did fail
def GetTokenFromRefreshToken(refreshtoken):
    token_url = "https://owner-api.teslamotors.com/oauth/token"
    data = {'grant_type': 'refresh_token', 'client_id': client_id, 'client_secret': client_secret,
            'refresh_token': refreshtoken}
    access_token_response = requests.post(token_url, data=data, verify=True, allow_redirects=False)
    # Check if connection did fail
    if access_token_response is None or access_token_response.status_code != 200:
        return None
    tokens = json.loads(access_token_response.text)
    teslatoken = TeslaToken()
    teslatoken.access_token = tokens["access_token"]
    teslatoken.expires_in = int(tokens["expires_in"])
    teslatoken.created_at = int(tokens["created_at"])
    teslatoken.refresh_token = tokens["refresh_token"]
    teslatoken.vehicle_id = None
    return teslatoken


# Return the list of velicles or None if token is not valid
def GetVehicles(access_token):
    api_call_headers = {'Authorization': 'Bearer ' + access_token}
    api_call_response = requests.get("https://owner-api.teslamotors.com/api/1/vehicles", headers=api_call_headers,
                                     verify=True)
    if api_call_response is None or api_call_response.status_code != 200:
        return None
    vehicles = json.loads(api_call_response.text)
    return vehicles


# return the first vehicle or None
def GetVehicle(vehicles):
    if vehicles is None or len(vehicles['response']) == 0:
        return None
    return vehicles['response'][0]['id']


# return the tesla token object, having token and vehicule id (if a vehicle is associated to account)
def Connect(user):
    try:
        teslatoken = TeslaToken.objects.get(user_id=user.id)
    except ObjectDoesNotExist:
        raise TeslaNoUserException()
    # Do we have a token not expired (renew at mid life)?
    if datetime.datetime.fromtimestamp(teslatoken.created_at + teslatoken.expires_in / 2) >= datetime.datetime.now():
        # token is still valid
        if teslatoken.vehicle_id is None:
            raise TeslaNoVehiculeException()
        return teslatoken
    # Use renewal to generate a new token
    newteslatoken = GetTokenFromRefreshToken(teslatoken.refresh_token)
    teslatoken.delete()
    if newteslatoken is None:
        raise TeslaUnauthorisedException()  # refresh did fail
    # save refreshed token
    newteslatoken.user_id = user
    newteslatoken.vehicle_id = GetVehicle(GetVehicles(newteslatoken.access_token))
    newteslatoken.save()
    # and return it as above
    if newteslatoken.vehicle_id is None:
        raise TeslaNoVehiculeException()
    return newteslatoken


# return true if tesla is awake. False if still sleeping
def WaitForWakeUp(teslaatoken):
    if teslaatoken is None:
        raise AuthenticationError
    # wait until awake, can take some time
    passes = 0
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    while True:
        api_call_response = requests.post(
            "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/wake_up",
            headers=api_call_headers, verify=True)
        if api_call_response is None or api_call_response.status_code != 200:
            raise TeslaServerException()
        vehicle_state = json.loads(api_call_response.text)
        if vehicle_state["response"]["state"] == "online":
            break
        # datetime.time.sleep(1)  # Wait for 1 seconds
        return False
        passes = passes + 1
        if passes == 3:
            return False
    return True


# Save data history
def SaveDataHistory(teslaState):
    context = teslaState.vehicle_state["response"]
    vehicle_config = context["vehicle_config"]
    vehicle_state = context["vehicle_state"]
    # Firmware updates
    toSave = TeslaFirmwareHistory()
    toSave.SaveIfDontExistsYet(teslaState.vin, vehicle_state["car_version"], vehicle_config["car_type"])


# returns params as TeslaState
def ParamsConnectedTesla(user):
    teslaatoken = Connect(user)
    ret = TeslaState
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    api_call_response = requests.get(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/vehicle_data",
        headers=api_call_headers, verify=True)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaServerException()
    vehicle_state = json.loads(api_call_response.text)
    ret.vehicle_state = vehicle_state
    context = vehicle_state["response"]
    ret.vin = context["vin"]
    ret.name = context["display_name"]
    ret.isOnline = context["state"] == "online"
    if ret.isOnline == False:
        return ret
    chargestate = context["charge_state"]
    ret.batteryrange = chargestate["battery_range"] * 1.609344
    # Estimate battery degradation
    ret.batterydegradation = (1. - ((1. * ret.batteryrange) / (chargestate["battery_level"] * 1.) * 100.) / 500.) * 100.
    drive_state = context["drive_state"]
    longitude = str(drive_state["longitude"])
    latitude = str(drive_state["latitude"])
    vehicle_state = context["vehicle_state"]
    ret.OdometerInKm = vehicle_state['odometer'] * 1.609344
    try:
        geolocator = Nominatim(user_agent="mon app tesla")
        location = geolocator.reverse(latitude + "," + longitude)
        ret.location = location.address
    except Exception:
        ret.location = "Unknown"
    # Save info for data history
    SaveDataHistory(ret)
    return ret


def SetChargeLevel(desiredchargelevel, user):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    data = {'percent': str(desiredchargelevel)}
    api_call_response = requests.post(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/set_charge_limit",
        headers=api_call_headers, verify=True, data=data)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()


# rem execute a command, see https://www.teslaapi.io/vehicles/commands for list
def executeCommand(user, command, setOn=None):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    if setOn is None:
        api_call_response = requests.post(
            "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/" + command,
            headers=api_call_headers, verify=True)
    else:
        data = {'on': str(setOn)}
        api_call_response = requests.post(
            "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/" + command,
            headers=api_call_headers, verify=True, data=data)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()
