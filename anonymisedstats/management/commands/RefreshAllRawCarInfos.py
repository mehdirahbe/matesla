import json
import time

import requests
from django.core.management.base import BaseCommand, CommandError

from matesla.TeslaConnect import SendWakeUpCommand, SaveDataHistory, GetProxyToUse
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaToken import TeslaToken
from matesla.TeslaState import TeslaState
from matesla.models.VinHash import HashTheVin


class Command(BaseCommand):
    help = 'Runs through token and refresh all car info'

    '''See https://medium.com/@bencleary/django-scheduled-tasks-queues-part-1-62d6b6dc24f8
    You can run this:
    python3 manage.py RefreshAllRawCarInfos
    In heroku, it runs with a scheduled task once a day (as it has to wake up cars)
    To add the scheduler, it is:
    heroku addons:create scheduler:standard  -a matesla
    Then configure it via the web interface
    You can see execution logs like this:
    heroku logs --ps scheduler -a matesla
    '''

    def RefreshOneCarInfo(self, vehicle_id, access_token):
        if vehicle_id is None or access_token is None:
            print("Id ou token is missing\n")
            return
        api_call_headers = {'Authorization': 'Bearer ' + access_token}
        # Loop as car is probably asleep
        whichPass = 0
        while True:
            whichPass = whichPass + 1
            if whichPass > 10:
                print("Give up this car, it doesn't wakeup\n")
                return
            api_call_response = requests.get(
                "https://owner-api.teslamotors.com/api/1/vehicles/" + str(vehicle_id) + "/vehicle_data",
                proxies=GetProxyToUse(), headers=api_call_headers, verify=True)
            # If asleep, wait 1 second
            if api_call_response is not None and api_call_response.status_code == 408:
                print("Car asleep, will try to wake it\n")
                SendWakeUpCommand(access_token, vehicle_id)
                time.sleep(1)
                continue
            if api_call_response is None or api_call_response.status_code != 200:
                print("Give up this car, error received\n")
                return  # some error
            # save info we need
            ret = TeslaState
            vehicle_state = json.loads(api_call_response.text)
            ret.vehicle_state = vehicle_state
            context = vehicle_state["response"]
            ret.vin = context["vin"]
            SaveDataHistory(ret)
            print("Info refreshed for " + context["display_name"] + "\n")
            return

    # in case some vin don't have their hash, as field was added after 1 week
    def UpdateHashVin(self):
        # car info
        alltoUpdate = TeslaCarInfo.objects.filter(hashedVin__isnull=True)
        for entry in alltoUpdate:
            entry.hashedVin = HashTheVin(entry.vin)
            entry.save(update_fields=['hashedVin'])
        # firmware history
        alltoUpdate = TeslaFirmwareHistory.objects.filter(hashedVin__isnull=True)
        for entry in alltoUpdate:
            entry.hashedVin = HashTheVin(entry.vin)
            entry.save(update_fields=['hashedVin'])

    def handle(self, *args, **options):
        self.UpdateHashVin()
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
