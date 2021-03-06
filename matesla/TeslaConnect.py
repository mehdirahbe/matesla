import datetime
import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from .BatteryDegradation import ComputeBatteryDegradation
from .models.AddressFromLatLong import AddressFromLatLong, GetAddressFromLatLong
from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaToken import TeslaToken
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo
from matesla.TeslaState import TeslaState
import os


# proxy to use since someone in Tesla (grr) did block all cloud (ie amazon aws)
# requests the 10/9/2020. If prod, define HTTPS_PROXY. I found that
# proxies from https://proxy-seller.com works fine
# HTTPS_PROXY must be something like: http://user on proxy:pw on proxy@IP address:port
def GetProxyToUse():
    try:
        proxyStr = os.environ['HTTPS_PROXY']
    except KeyError:
        return None
    if proxyStr is None:
        return None
    proxiesDict = {"https": proxyStr}
    return proxiesDict


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
    access_token_response = requests.post(token_url, proxies=GetProxyToUse(), data=data, verify=True,
                                          allow_redirects=False)
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
    access_token_response = requests.post(token_url, proxies=GetProxyToUse(), data=data, verify=True,
                                          allow_redirects=False)
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
    api_call_response = requests.get("https://owner-api.teslamotors.com/api/1/vehicles",
                                     proxies=GetProxyToUse(),
                                     headers=api_call_headers,
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
    # if we don't have way to compute expiry date or refresh token, continue we what we have
    # And if tesla site eject us, we will arivve to tesla account page
    if teslatoken.created_at is None or teslatoken.refresh_token is None:
        if teslatoken.vehicle_id is None:
            raise TeslaNoVehiculeException()
        return teslatoken
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


def SendWakeUpCommand(access_token, vehicle_id):
    api_call_headers = {'Authorization': 'Bearer ' + access_token}
    api_call_response = requests.post(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(vehicle_id) + "/wake_up",
        proxies=GetProxyToUse(), headers=api_call_headers, verify=True)
    return api_call_response


# return true if tesla is awake. False if still sleeping
def WaitForWakeUp(teslaatoken: TeslaToken):
    if teslaatoken is None:
        raise TeslaAuthenticationException
    # wait until awake, can take some time
    passes = 0
    while True:
        api_call_response = SendWakeUpCommand(teslaatoken.access_token, teslaatoken.vehicle_id)
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
    try:
        context = teslaState.vehicle_state["response"]
        vehicle_config = context["vehicle_config"]
        vehicle_state = context["vehicle_state"]
        # Firmware updates
        toSave = TeslaFirmwareHistory()
        toSave.SaveIfDontExistsYet(teslaState.vin, vehicle_state["car_version"], vehicle_config["car_type"])
        # Car infos
        toSave = TeslaCarInfo()
        toSave = toSave.SaveIfDontExistsYet(teslaState.vin, context)
        # if we don't have epa range yet, this will force its recomputation
        if toSave.EPARange is None:
            chargestate = context["charge_state"]
            vehicle_state = context["vehicle_state"]
            ComputeBatteryDegradation(chargestate["battery_range"], chargestate["usable_battery_level"],
                                      teslaState.vin, vehicle_state['odometer'])
        # Car variable infos
        toSave = TeslaCarDataSnapshot()
        toSave.SaveIfDontExistsYet(teslaState.vin, context)
    # during firmware update, some fields will be null-->don't crash, just ignore save of invalid data
    except IntegrityError:
        return

# returns params as TeslaState
def ParamsConnectedTesla(user):
    teslaatoken = Connect(user)
    ret = TeslaState
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    api_call_response = requests.get(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/vehicle_data",
        proxies=GetProxyToUse(), headers=api_call_headers, verify=True)
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
    vehicle_state = context["vehicle_state"]
    ret.batterydegradation, ret.NumberCycles, ret.EPARangeMiles = ComputeBatteryDegradation(
        chargestate["battery_range"],
        chargestate["usable_battery_level"],
        ret.vin,
        vehicle_state['odometer'])
    drive_state = context["drive_state"]
    longitude = str(drive_state["longitude"])
    latitude = str(drive_state["latitude"])
    ret.OdometerInKm = vehicle_state['odometer'] * 1.609344
    ret.location = GetAddressFromLatLong(drive_state["latitude"], drive_state["longitude"])
    # Save info for data history
    SaveDataHistory(ret)
    return ret


def SetTeslaParamater(data, user, commandToCall):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    api_call_response = requests.post(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/" + commandToCall,
        proxies=GetProxyToUse(), headers=api_call_headers, verify=True, data=data)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()


def SetChargeLevel(desiredchargelevel, user):
    data = {'percent': str(desiredchargelevel)}
    SetTeslaParamater(data, user, 'set_charge_limit')


def SetDriverTempCelcius(desiredtemperature, user):
    data = {'driver_temp': str(desiredtemperature)}
    SetTeslaParamater(data, user, 'set_temps')


# See https://tesla-api.timdorr.com/vehicle/commands/remotestart
def ActivateRemoteStartDrive(password, user):
    data = {'password': str(password)}
    SetTeslaParamater(data, user, 'remote_start_drive')


# rem execute a command, see https://www.teslaapi.io/vehicles/commands for list
def executeCommand(user, command, setOn=None, addParamName=None, addParamValue=None):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    if setOn is None and addParamName is None:
        api_call_response = requests.post(
            "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/command/" + command,
            proxies=GetProxyToUse(), headers=api_call_headers, verify=True)
    else:
        if setOn is not None:
            data = {'on': str(setOn)}
            api_call_response = requests.post(
                "https://owner-api.teslamotors.com/api/1/vehicles/" + str(
                    teslaatoken.vehicle_id) + "/command/" + command,
                proxies=GetProxyToUse(), headers=api_call_headers, verify=True, data=data)
        else:
            data = {addParamName: str(addParamValue)}
            api_call_response = requests.post(
                "https://owner-api.teslamotors.com/api/1/vehicles/" + str(
                    teslaatoken.vehicle_id) + "/command/" + command,
                proxies=GetProxyToUse(), headers=api_call_headers, verify=True, data=data)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()
