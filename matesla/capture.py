"""
TeslaFi-style capture: for every known vehicle that is already online,
pull full vehicle_data and persist snapshots + history for graphs.

Never wakes cars. Safe to run every minute via cron.
"""

from __future__ import annotations

import json
import traceback

import requests

from matesla.TeslaConnect import (
    VEHICLE_DATA_ENDPOINTS,
    GetProxyToUse,
    SaveDataHistory,
    api_url,
    get_vehicle_connectivity,
    get_vehicles_list_payload,
    refresh_vehicle_states_from_list,
)
from matesla.TeslaOAuth import TeslaOAuthError, ensure_fresh_access_token
from matesla.TeslaState import TeslaState
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle


def fetch_vehicle_data(access_token: str, vehicle_id: str) -> dict | None:
    resp = requests.get(
        api_url(f"/api/1/vehicles/{vehicle_id}/vehicle_data"),
        params={"endpoints": VEHICLE_DATA_ENDPOINTS},
        headers={"Authorization": "Bearer " + access_token},
        proxies=GetProxyToUse(),
        verify=True,
        timeout=60,
    )
    if resp is None or resp.status_code != 200:
        return None
    return json.loads(resp.text)


def capture_one_vehicle(
    vehicle: TeslaVehicle,
    access_token: str,
    *,
    list_payload=None,
) -> str:
    """
    Capture one vehicle if online.
    Returns: 'saved' | 'skipped_offline' | 'skipped_error'
    """
    state = get_vehicle_connectivity(
        access_token, vehicle.api_id, list_payload=list_payload
    )
    if state is not None and state != "online":
        TeslaVehicle.objects.filter(pk=vehicle.pk).update(state=state or "")
        return "skipped_offline"

    payload = fetch_vehicle_data(access_token, vehicle.api_id)
    if not payload:
        return "skipped_error"

    context = payload.get("response") or {}
    vin = context.get("vin") or vehicle.vin
    if not vin:
        return "skipped_error"

    TeslaVehicle.objects.filter(pk=vehicle.pk).update(
        state=context.get("state") or "online",
        display_name=context.get("display_name") or vehicle.display_name,
        vin=vin,
    )

    ret = TeslaState()
    ret.vin = vin
    ret.name = context.get("display_name") or vehicle.display_name or vin
    ret.vehicle_state = payload
    SaveDataHistory(ret)
    return "saved"


def capture_all_online_vehicles() -> dict:
    """
    Walk all TeslaVehicle rows; one /vehicles list per token, then vehicle_data
    only for cars that are online.
    """
    stats = {"saved": 0, "skipped_offline": 0, "skipped_error": 0, "token_error": 0}

    # Meta.ordering + distinct() on SQLite returns one row per vehicle (duplicates).
    # order_by() clears that so each user_id appears once.
    user_ids = list(
        TeslaVehicle.objects.order_by()
        .values_list("user_id", flat=True)
        .distinct()
    )

    for user_id in user_ids:
        token = TeslaToken.objects.filter(user_id=user_id).first()
        if not token:
            continue
        try:
            token = ensure_fresh_access_token(token)
        except TeslaOAuthError:
            stats["token_error"] += 1
            continue

        list_payload = get_vehicles_list_payload(token.access_token)
        try:
            refresh_vehicle_states_from_list(token.user_id, list_payload)
        except Exception:
            traceback.print_exc()

        for vehicle in TeslaVehicle.objects.filter(user_id=user_id):
            label = (vehicle.display_name or vehicle.vin or vehicle.api_id).strip()
            try:
                result = capture_one_vehicle(
                    vehicle,
                    token.access_token,
                    list_payload=list_payload,
                )
                stats[result] = stats.get(result, 0) + 1
                print(f"  {label}: {result}")
            except Exception:
                traceback.print_exc()
                stats["skipped_error"] += 1
                print(f"  {label}: skipped_error")

    return stats
