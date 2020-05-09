import json
import time

import requests
from django.core.management.base import BaseCommand

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaToken import TeslaToken

class Command(BaseCommand):
    help = 'Runs through token and save all car inf' \
           'os. Do not wake them, let them sleep'

    '''You can run this:
    python3 manage.py RefreshAllRawCarInfos
    For scheduler details, see refreshallrawcarinfos.py
    '''

    def RefreshOneCarInfo(self, vehicle_id, access_token):
        if vehicle_id is None or access_token is None:
            print("Id ou token is missing\n")
            return
        api_call_headers = {'Authorization': 'Bearer ' + access_token}
        api_call_response = requests.get(
            "https://owner-api.teslamotors.com/api/1/vehicles/" + str(vehicle_id) + "/vehicle_data",
            headers=api_call_headers, verify=True)
        if api_call_response is None or api_call_response.status_code != 200:
            print("Give up this car, probably asleep\n")
            return  # some error
        # save info we need
        vehicle_state = json.loads(api_call_response.text)
        context = vehicle_state["response"]
        toSave = TeslaCarDataSnapshot()
        toSave.SaveIfDontExistsYet(context["vin"], context)
        print("Info refreshed for " + context["display_name"] + "\n")
        return

    def handle(self, *args, **options):
        # Loop on all tokens
        allTokens = TeslaToken.objects.values()
        countCars = 0
        for teslaatoken in allTokens:
            try:
                self.RefreshOneCarInfo(teslaatoken['vehicle_id'], teslaatoken['access_token'])
                countCars = countCars + 1
            except Exception:
                print("This car did throw an exception\n")
                pass
        print(str(countCars) + " cars processed\n")
