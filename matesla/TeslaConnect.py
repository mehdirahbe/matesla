import datetime
import traceback

import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError

from .BatteryDegradation import ComputeBatteryDegradation
from .models.AddressFromLatLong import AddressFromLatLong, GetAddressFromLatLong
from .models.AllSuperchargers import AllSuperchargers
from .models.SuperchargerUse import SuperchargerUse
from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaToken import TeslaToken
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo
from matesla.TeslaState import TeslaState
from matesla.GetProxyToUse import GetProxyToUse


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
# 2 june 2021: previous token attempts always fail in cloud due to tesla blocking
# heroku. Again a change of way-->too much is too much, I now expect token to be provided
# by user, using an internet/mobile app tool. -->ignore date, refresh...
# We have a valid token or not.
def Connect(user):
    try:
        teslatoken = TeslaToken.objects.get(user_id=user.id)
    except ObjectDoesNotExist:
        raise TeslaNoUserException()
    # if we don't have way to compute expiry date or refresh token, continue we what we have
    # And if tesla site eject us, we will arrive to tesla account page
    if teslatoken.vehicle_id is None:
        raise TeslaNoVehiculeException()
    return teslatoken


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
    # save nearby superchargers info for superchargers activity tracking
    try:
        SaveNearbyChargingSitesStats(teslaatoken.access_token, teslaatoken.vehicle_id)
    except Exception as ex:  # it should not except here and I don't want to crash display for that
        traceback.print_exc()  # but log the problem so that I am aware of it
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


# See https://tesla-api.timdorr.com/vehicle/state/nearbychargingsites
# Allow to receive all destination and superchargers
# Returns a list of superchargers, with each entry
# being a dico with those infos:
# 'location' = {dict: 2} {'lat': 50.88771, 'long': 4.453051}
# 'name' = {str} 'Machelen, Belgium'
# 'type' = {str} 'supercharger'
# 'distance_miles' = {float} 8.137976
# 'available_stalls' = {int} 5
# 'total_stalls' = {int} 8
# 'site_closed' = {bool} False
def GetNearbyChargingSites(access_token, vehicle_id):
    api_call_headers = {'Authorization': 'Bearer ' + access_token}
    api_call_response = requests.get(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(vehicle_id) + "/nearby_charging_sites",
        proxies=GetProxyToUse(), headers=api_call_headers, verify=True)
    if api_call_response is not None and api_call_response.status_code == 408:
        raise TeslaIsAsleepException
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaCommandException()
    return json.loads(api_call_response.content)["response"]["superchargers"]


def SaveNearbyChargingSitesStats(access_token, vehicle_id):
    return  # todo later, does not work yet
    chargers = GetNearbyChargingSites(access_token, vehicle_id)
    for charger in chargers:
        location = charger["location"]
        toSave = AllSuperchargers()
        fkey = toSave.SaveIfDontExistsYet(charger["name"], charger["type"],
                                          location["lat"], location["long"])
        toSave = SuperchargerUse()
        toSave.Save(charger["available_stalls"], charger["total_stalls"], charger["site_closed"], fkey)
