import json
import traceback

import requests
from django.core.management.base import BaseCommand

from matesla.TeslaConnect import SaveDataHistory, GetProxyToUse, api_url, get_vehicle_connectivity
from matesla.TeslaOAuth import ensure_fresh_access_token, TeslaOAuthError
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle
from matesla.TeslaState import TeslaState
from matesla.models.VinHash import HashTheVin


class Command(BaseCommand):
    help = (
        "Refresh car info for vehicles that are already online. "
        "Never wakes cars (free-tier / no credit-card policy)."
    )

    def RefreshOneCarInfo(self, vehicle_id, access_token):
        if vehicle_id is None or access_token is None:
            print("Id ou token is missing\n")
            return
        state = get_vehicle_connectivity(access_token, vehicle_id)
        if state is not None and state != "online":
            print(f"Car not online (state={state}) — skip, no wake\n")
            return
        api_call_headers = {"Authorization": "Bearer " + access_token}
        api_call_response = requests.get(
            api_url(f"/api/1/vehicles/{vehicle_id}/vehicle_data"),
            proxies=GetProxyToUse(),
            headers=api_call_headers,
            verify=True,
            timeout=60,
        )
        if api_call_response is not None and api_call_response.status_code == 408:
            print("Car asleep — skip, no wake\n")
            return
        if api_call_response is None or api_call_response.status_code != 200:
            print("Give up this car, error received\n")
            return
        ret = TeslaState
        vehicle_state = json.loads(api_call_response.text)
        ret.vehicle_state = vehicle_state
        context = vehicle_state["response"]
        ret.vin = context["vin"]
        SaveDataHistory(ret)
        print("Info refreshed for " + context["display_name"] + "\n")

    def UpdateHashVin(self):
        for entry in TeslaCarInfo.objects.filter(hashedVin__isnull=True):
            entry.hashedVin = HashTheVin(entry.vin)
            entry.save(update_fields=["hashedVin"])
        for entry in TeslaFirmwareHistory.objects.filter(hashedVin__isnull=True):
            entry.hashedVin = HashTheVin(entry.vin)
            entry.save(update_fields=["hashedVin"])

    def handle(self, *args, **options):
        self.UpdateHashVin()
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
