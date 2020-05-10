import datetime

import requests
import json

from django.core.exceptions import ObjectDoesNotExist
from geopy.geocoders import Nominatim

from .models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from .models.TeslaToken import TeslaToken
from .models.TeslaFirmwareHistory import TeslaFirmwareHistory
from .models.TeslaCarInfo import TeslaCarInfo
from matesla.TeslaState import TeslaState


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
        headers=api_call_headers, verify=True)
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
    context = teslaState.vehicle_state["response"]
    vehicle_config = context["vehicle_config"]
    vehicle_state = context["vehicle_state"]
    # Firmware updates
    toSave = TeslaFirmwareHistory()
    toSave.SaveIfDontExistsYet(teslaState.vin, vehicle_state["car_version"], vehicle_config["car_type"])
    # Car infos
    toSave = TeslaCarInfo()
    toSave.SaveIfDontExistsYet(teslaState.vin, context)
    # Car variable infos
    toSave = TeslaCarDataSnapshot()
    toSave.SaveIfDontExistsYet(teslaState.vin, context)


'''Return the battery degradation in %. The problem is to known EPA mileage as
it is not returned and due to invalid codes, it is impossible to know exact car
 and battery model.
 So compute when we can and return None when impossible to compute safely
 batteryrange should be in miles, battery_level is in %'''


def ComputeBatteryDegradation(batteryrange, battery_level, vin):
    # First check which car it is
    # https://teslatap.com/vin-decoder/
    # position 8 (base 1) B = Dual Motor - Standard or Performance Model 3
    # A = Single Motor - Standard Model 3
    # In pos 4 (base 1), the 3 means model 3
    # 5YJ3E7EA7KFxxxxxx seems to be the rare LR 1 motor
    # 5YJ3E7EB1KFxxxxxx AWD (mine, I am sure)
    # 5YJ3E7EA4LFxxxxxx std range 1 moteur (friend, I am sure too)
    model = vin[3]
    if model != "3":
        return None
    isDual = (vin[7] == "B")
    if isDual is True:
        '''From https://en.wikipedia.org/wiki/Tesla_Model_3
        the optional 75-kWh battery would have a range of about 310 miles (500 km)
        It seems that in 2020, a new rating was set to 322 miles, but clearly mar
        AWD car still computes based on 310 (500 km)'''
        EPARange = 310.
    else:
        '''From https://en.wikipedia.org/wiki/Tesla_Model_3        
        It seems that in 2020, a new rating was set to 250 miles (402 km), but clearly
        car bought new in february/march 2020 don't use that, as nearly
        new in april 2020, they shouldn't have a degradation > 5%
        -->After 1 hour doing google searches, impossible to find old value.
        So back to https://forums.automobile-propre.com/topic/kilom%C3%A9trage-max-qu-indique-votre-voiture-%C3%A0-100-15552/?&page=78#comments
        There Kratos said 378 km for SR+-->235 miles for a new car from march 2020
        Let's use that, it should at least be reasonable'''
        EPARange = 235.
    batterydegradation = (1. - ((1. * batteryrange) / (battery_level * 1.) * 100.) / EPARange) * 100.
    if isDual is False and batterydegradation < -5:
        '''From https://en.wikipedia.org/wiki/Tesla_Model_3
        RWD: 325 miles (523 km) combined
        single motor with strongly negative battery degradatation means
        probably the LR single motor. It would be nice if someone
        with that model confirms'''
        EPARange = 325.
        batterydegradation = (1. - ((1. * batteryrange) / (battery_level * 1.) * 100.) / EPARange) * 100.
    if batterydegradation < 0.:  # don't return negative degradation
        batterydegradation = 0.
    return batterydegradation


# Just get the vin, don't wake up car
def JustGetTheVin(user):
    teslaatoken = Connect(user)
    api_call_headers = {'Authorization': 'Bearer ' + teslaatoken.access_token}
    api_call_response = requests.get(
        "https://owner-api.teslamotors.com/api/1/vehicles/" + str(teslaatoken.vehicle_id) + "/vehicle_data",
        headers=api_call_headers, verify=True)
    if api_call_response is not None and api_call_response.status_code == 408:
        return json.loads(api_call_response.text)["response"]["vin"]  # asleep, don't care vin is present
    if api_call_response is not None and api_call_response.status_code == 401:
        raise TeslaUnauthorisedException
    if api_call_response is None or api_call_response.status_code != 200:
        raise TeslaServerException()
    return json.loads(api_call_response.text)["response"]["vin"]  # car is awake


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
    ret.batterydegradation = ComputeBatteryDegradation(chargestate["battery_range"], chargestate["battery_level"],
                                                       ret.vin)
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
