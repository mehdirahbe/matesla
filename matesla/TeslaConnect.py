import datetime

import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from geopy.geocoders import Nominatim

from matesla.models import TeslaState, TeslaAccount, TeslaToken
from matesla.passwordencryption import getSaltForKey, decrypt

class TeslaServerException(Exception):
    pass

class TeslaAuthenticationException(Exception):
    pass

#tesla did refuse our token, yhis occurs when password has been changed by user
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

# return the tesla token object, having token and vehicule id (if a vehicle is associated to account)
def Connect(user):
    try:
        teslatoken = TeslaToken.objects.get(user_id=user.id)
    except ObjectDoesNotExist:
        teslatoken = None
    # Do we have a valid filled token not expired?
    if teslatoken is None or len(teslatoken.access_token) == 0 or datetime.datetime.fromtimestamp(
            teslatoken.created_at + teslatoken.expires_in) < datetime.datetime.now():
        if teslatoken is None:
            teslatoken = TeslaToken()
            teslatoken.user_id = user
        # refresh the token with user+pw
        try:
            teslaaccount = TeslaAccount.objects.get(user_id=user.id)
        except ObjectDoesNotExist:
            raise TeslaNoUserException()
        teslalogin = teslaaccount.TeslaUser
        saltlogin = getSaltForKey(teslalogin)
        teslapw = decrypt(teslaaccount.TeslaPassword, saltlogin)
        # tesla client id and secret which are everywhere on internet
        client_id = '81527cff06843c8634fdc09e8ac0abefb46ac849f38fe1e431c2ef2106796384'
        client_secret = 'c7257eb71a564034f9419ee651c7d0e5f7aa6bfbd18bafb5c5c033b093bb2fa3'
        # do the Oauth 2 request
        token_url = "https://owner-api.teslamotors.com/oauth/token"
        data = {'grant_type': 'password', 'client_id': client_id, 'client_secret': client_secret,
                'email': teslalogin, 'password': teslapw}
        access_token_response = requests.post(token_url, data=data, verify=True, allow_redirects=False)
        # Check if connection did fail
        if access_token_response is None or access_token_response.status_code != 200:
            raise TeslaAuthenticationException()
        tokens = json.loads(access_token_response.text)
        teslatoken.access_token = tokens["access_token"]
        teslatoken.expires_in = int(tokens["expires_in"])
        teslatoken.created_at = int(tokens["created_at"])
        teslatoken.refresh_token = tokens["refresh_token"]
        # now that we have a token, we can ask the vehicles
        api_call_headers = {'Authorization': 'Bearer ' + teslatoken.access_token}
        api_call_response = requests.get("https://owner-api.teslamotors.com/api/1/vehicles", headers=api_call_headers,
                                         verify=True)
        if api_call_response is None or api_call_response.status_code != 200:
            raise TeslaServerException()
        vehicles = json.loads(api_call_response.text)
        if len(vehicles['response']) == 0:
            raise TeslaNoVehiculeException()
        teslatoken.vehicle_id = vehicles['response'][0]['id']
        teslatoken.save()
    return teslatoken


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
        #datetime.time.sleep(1)  # Wait for 1 seconds
        return False
        passes = passes + 1
        if passes == 3:
            return False
    return True


# returns params as TeslaState
def ParamsConnectedTesla(user):
    teslaatoken = Connect(user)
    ret = TeslaState
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    api_call_response = requests.get(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/vehicle_data",
        headers=api_call_headers, verify=True)
    if api_call_response is not None and api_call_response.status_code==408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code==401:
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
    chargestate=context["charge_state"]
    ret.batteryrange = chargestate["battery_range"] * 1.609344
    # Estimate battery degradation
    ret.batterydegradation = (1. - ((1. * ret.batteryrange) / (chargestate["battery_level"] * 1.) * 100.) / 500.) * 100.
    drive_state=context["drive_state"]
    longitude = str(drive_state["longitude"])
    latitude = str(drive_state["latitude"])
    vehicle_state=context["vehicle_state"]
    ret.OdometerInKm=vehicle_state['odometer']* 1.609344
    try:
        geolocator = Nominatim(user_agent="mon app tesla")
        location = geolocator.reverse(latitude + "," + longitude)
        ret.location = location.address
    except Exception:
        ret.location="Unknown"
    return ret


def SetChargeLevel(desiredchargelevel, user):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    data = {'percent': str(desiredchargelevel)}
    api_call_response = requests.post(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/set_charge_limit",
        headers=api_call_headers, verify=True,data=data)
    if api_call_response is not None and api_call_response.status_code==408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code==401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()


# rem execute a command, see https://www.teslaapi.io/vehicles/commands for list
def executeCommand(user, command,setOn=None):
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
            headers=api_call_headers, verify=True,data=data)
    if api_call_response is not None and api_call_response.status_code==408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code==401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()
