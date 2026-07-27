"""
Fleet API partner registration.

Tesla requires every developer app to be registered per region before
vehicle list / data calls work. That needs:

1. A public HTTPS domain (not localhost)
2. EC public key at:
   https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
3. Domain listed as Allowed Origin on developer.tesla.com
4. POST /api/1/partner_accounts with a partner (client_credentials) token

Auth endpoints are free; this unlocks the rest of the API.
"""

from __future__ import annotations

from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.utils.translation import gettext as _

from matesla.GetProxyToUse import GetProxyToUse
from matesla.models.TeslaAppSettings import TeslaAppSettings
from matesla.TeslaOAuth import TOKEN_URL, TeslaOAuthError

KEYS_DIR = Path(settings.BASE_DIR) / "tesla_keys"
PRIVATE_KEY_PATH = KEYS_DIR / "private-key.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "public-key.pem"
PUBLIC_KEY_WELLKNOWN_NAME = "com.tesla.3p.public-key.pem"


class TeslaPartnerError(Exception):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def ensure_key_pair() -> tuple[Path, Path]:
    """Generate prime256v1 EC key pair if missing. Returns (private, public) paths."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_KEY_PATH.exists() and PUBLIC_KEY_PATH.exists():
        return PRIVATE_KEY_PATH, PUBLIC_KEY_PATH

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    PRIVATE_KEY_PATH.write_bytes(private_pem)
    PRIVATE_KEY_PATH.chmod(0o600)
    PUBLIC_KEY_PATH.write_bytes(public_pem)
    return PRIVATE_KEY_PATH, PUBLIC_KEY_PATH


def public_key_pem_text() -> str:
    ensure_key_pair()
    return PUBLIC_KEY_PATH.read_text()


def public_key_url(domain: str) -> str:
    domain = domain.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{domain}/.well-known/appspecific/{PUBLIC_KEY_WELLKNOWN_NAME}"


def check_public_key_reachable(domain: str) -> tuple[bool, str]:
    """Tesla must be able to fetch the public key over HTTPS."""
    url = public_key_url(domain)
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        return False, _("Cannot reach %(url)s: %(err)s") % {"url": url, "err": exc}
    if resp.status_code != 200:
        return False, _("%(url)s → HTTP %(code)s") % {
            "url": url,
            "code": resp.status_code,
        }
    body = resp.text.strip()
    if "BEGIN PUBLIC KEY" not in body:
        return False, _("%(url)s does not contain a valid public key PEM") % {
            "url": url
        }
    local = public_key_pem_text().strip()
    if body != local and body.replace("\r\n", "\n") != local.replace("\r\n", "\n"):
        return (
            False,
            _(
                "%(url)s is reachable but does not match the local key "
                "(%(path)s). Re-upload the file."
            )
            % {"url": url, "path": PUBLIC_KEY_PATH},
        )
    return True, _("Public key OK: %(url)s") % {"url": url}


def get_partner_token(app: TeslaAppSettings | None = None) -> str:
    app = app or TeslaAppSettings.get_solo()
    if not app or not app.client_id or not app.client_secret:
        raise TeslaPartnerError(_("Missing Client ID / Secret."))
    data = {
        "grant_type": "client_credentials",
        "client_id": app.client_id,
        "client_secret": app.client_secret,
        "audience": app.api_base.rstrip("/"),
        "scope": "openid offline_access user_data vehicle_device_data vehicle_cmds vehicle_charging_cmds",
    }
    resp = requests.post(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=GetProxyToUse(),
        timeout=60,
    )
    if resp.status_code != 200:
        raise TeslaPartnerError(
            _("Partner token failed (%(code)s): %(body)s")
            % {"code": resp.status_code, "body": resp.text[:800]},
            response=resp,
        )
    return resp.json()["access_token"]


def register_partner_account(domain: str, app: TeslaAppSettings | None = None) -> dict:
    """
    POST /api/1/partner_accounts for the configured region.
    domain: bare hostname, e.g. robotcar.example.com (no scheme).
    """
    app = app or TeslaAppSettings.get_solo()
    if not app:
        raise TeslaPartnerError(_("Missing app settings."))

    domain = (
        domain.strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .split("/")[0]
    )
    if not domain or domain in ("localhost", "127.0.0.1"):
        raise TeslaPartnerError(
            _(
                "Tesla rejects localhost as a partner domain. "
                "Use a public HTTPS domain (e.g. robotcar.example.com)."
            )
        )

    ok, msg = check_public_key_reachable(domain)
    if not ok:
        raise TeslaPartnerError(
            _("Public key not reachable by Tesla before register: %(msg)s")
            % {"msg": msg}
        )

    partner_token = get_partner_token(app)
    api_base = app.api_base.rstrip("/")
    resp = requests.post(
        f"{api_base}/api/1/partner_accounts",
        headers={
            "Authorization": f"Bearer {partner_token}",
            "Content-Type": "application/json",
        },
        json={"domain": domain},
        proxies=GetProxyToUse(),
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise TeslaPartnerError(
            _("Partner register failed (%(code)s): %(body)s")
            % {"code": resp.status_code, "body": resp.text[:1000]},
            response=resp,
        )
    # Persist domain as registered
    app.partner_domain = domain
    app.partner_registered = True
    app.save(update_fields=["partner_domain", "partner_registered", "updated_at"])
    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "text": resp.text[:500]}
