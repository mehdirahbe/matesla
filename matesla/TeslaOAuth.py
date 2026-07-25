"""Tesla Fleet API OAuth 2.0 (authorization code + refresh)."""

from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.utils import timezone

from matesla.GetProxyToUse import GetProxyToUse
from matesla.models.TeslaAppSettings import TeslaAppSettings
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle

AUTH_AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"

DEFAULT_SCOPES = (
    "openid offline_access user_data vehicle_device_data "
    "vehicle_location vehicle_cmds vehicle_charging_cmds"
)


class TeslaOAuthError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def _require_app_settings() -> TeslaAppSettings:
    app = TeslaAppSettings.get_solo()
    if not app or not app.client_id or not app.client_secret:
        raise TeslaOAuthError(
            "Application Tesla non configurée (Client ID / Secret manquants)."
        )
    return app


def build_authorize_url(state: str, locale: str = "fr-BE") -> str:
    app = _require_app_settings()
    params = {
        "response_type": "code",
        "client_id": app.client_id,
        "redirect_uri": app.redirect_uri,
        "scope": DEFAULT_SCOPES,
        "state": state,
        "prompt_missing_scopes": "true",
        "locale": locale,
    }
    return f"{AUTH_AUTHORIZE_URL}?{urlencode(params)}"


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def exchange_code_for_tokens(code: str) -> dict:
    app = _require_app_settings()
    data = {
        "grant_type": "authorization_code",
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "code": code,
        "audience": app.api_base.rstrip("/"),
        "redirect_uri": app.redirect_uri,
    }
    resp = requests.post(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=GetProxyToUse(),
        timeout=60,
    )
    if resp.status_code != 200:
        raise TeslaOAuthError(
            f"Échange du code OAuth échoué ({resp.status_code}): {resp.text[:500]}",
            response=resp,
        )
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    app = _require_app_settings()
    data = {
        "grant_type": "refresh_token",
        "client_id": app.client_id,
        "refresh_token": refresh_token,
    }
    resp = requests.post(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=GetProxyToUse(),
        timeout=60,
    )
    if resp.status_code != 200:
        raise TeslaOAuthError(
            f"Refresh token échoué ({resp.status_code}): {resp.text[:500]}",
            response=resp,
        )
    return resp.json()


def apply_token_response(user, token_payload: dict) -> TeslaToken:
    """Persist account-level tokens. refresh_token is rotated — always save the new one."""
    access = token_payload["access_token"]
    refresh = token_payload.get("refresh_token") or ""
    expires_in = int(token_payload.get("expires_in") or 0)
    expires_at = timezone.now() + timedelta(seconds=expires_in) if expires_in else None

    existing = TeslaToken.objects.filter(user_id=user).first()
    if existing:
        existing.access_token = access
        if refresh:
            existing.refresh_token = refresh
        existing.expires_at = expires_at
        existing.save()
        return existing

    return TeslaToken.objects.create(
        user_id=user,
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
    )


def ensure_fresh_access_token(tesla_token: TeslaToken) -> TeslaToken:
    if not tesla_token.is_access_token_expired():
        return tesla_token
    if not tesla_token.refresh_token:
        raise TeslaOAuthError("Access token expiré et aucun refresh_token en base.")
    payload = refresh_access_token(tesla_token.refresh_token)
    return apply_token_response(tesla_token.user_id, payload)


def sync_vehicles_from_api(user, vehicles_payload: dict) -> list[TeslaVehicle]:
    """
    Upsert all vehicles returned by GET /api/1/vehicles.
    Removes vehicles no longer on the account.
    Ensures one primary vehicle.
    """
    response = (vehicles_payload or {}).get("response") or []
    seen_ids = []
    created_or_updated = []

    for v in response:
        api_id = str(v.get("id") or v.get("vehicle_id") or "")
        if not api_id:
            continue
        seen_ids.append(api_id)
        obj, _ = TeslaVehicle.objects.update_or_create(
            user=user,
            api_id=api_id,
            defaults={
                "vin": v.get("vin") or "",
                "display_name": v.get("display_name") or "",
                "state": v.get("state") or "",
            },
        )
        created_or_updated.append(obj)

    if seen_ids:
        TeslaVehicle.objects.filter(user=user).exclude(api_id__in=seen_ids).delete()
    else:
        TeslaVehicle.objects.filter(user=user).delete()

    # Ensure exactly one primary among remaining vehicles
    vehicles = list(TeslaVehicle.objects.filter(user=user))
    if vehicles and not any(v.is_primary for v in vehicles):
        vehicles[0].is_primary = True
        vehicles[0].save(update_fields=["is_primary"])
    elif vehicles:
        # If several primaries (edge case), keep the first
        primaries = [v for v in vehicles if v.is_primary]
        if len(primaries) > 1:
            for extra in primaries[1:]:
                extra.is_primary = False
                extra.save(update_fields=["is_primary"])

    return list(TeslaVehicle.objects.filter(user=user))
