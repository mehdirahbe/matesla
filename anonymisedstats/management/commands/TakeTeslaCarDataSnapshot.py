import json
import traceback
from random import random

import requests
from django.core.management.base import BaseCommand

from matesla.TeslaConnect import GetProxyToUse, SaveNearbyChargingSitesStats, api_url
from matesla.TeslaOAuth import ensure_fresh_access_token, TeslaOAuthError
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle


class Command(BaseCommand):
    help = "Iterate all known vehicles and snapshot data without waking them."

    def RefreshOneCarInfo(self, vehicle_id, access_token):
        if vehicle_id is None or access_token is None:
            print("Id ou token is missing\n")
            return
        api_call_headers = {"Authorization": "Bearer " + access_token}
        api_call_response = requests.get(
            api_url(f"/api/1/vehicles/{vehicle_id}/vehicle_data"),
            proxies=GetProxyToUse(),
            headers=api_call_headers,
            verify=True,
            timeout=60,
        )
        if api_call_response is None or api_call_response.status_code != 200:
            print("Give up this car, probably asleep\n")
            return
        vehicle_state = json.loads(api_call_response.text)
        context = vehicle_state["response"]
        toSave = TeslaCarDataSnapshot()
        toSave.SaveIfDontExistsYet(context["vin"], context)
        try:
            SaveNearbyChargingSitesStats(access_token, vehicle_id, context["vin"])
        except Exception:
            traceback.print_exc()
        print("Info refreshed for " + context["display_name"] + "\n")

    def UpdateNewlyAddedFields(self):
        alltoUpdate = TeslaCarDataSnapshot.objects.filter(randomNr__isnull=True)
        for entry in alltoUpdate:
            entry.randomNr = random()
            entry.save(update_fields=["randomNr"])

    def handle(self, *args, **options):
        self.UpdateNewlyAddedFields()
        countCars = 0
        for vehicle in TeslaVehicle.objects.select_related("user").all():
            token = TeslaToken.objects.filter(user_id=vehicle.user_id).first()
            if not token:
                continue
            try:
                token = ensure_fresh_access_token(token)
                self.RefreshOneCarInfo(vehicle.api_id, token.access_token)
                countCars += 1
            except TeslaOAuthError as exc:
                print(f"Token error for {vehicle}: {exc}\n")
            except Exception:
                print("This car did throw an exception\n")
                traceback.print_exc()
        print(str(countCars) + " cars processed\n")
