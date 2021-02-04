import base64
import hashlib
import os
import re
from urllib.parse import parse_qs
import requests
from matesla.models.TeslaToken import TeslaToken

# see https://tesla-api.timdorr.com/api-basics/authentication for new api mandatory since feb 2021
# Code inspired from https://github.com/enode-engineering/tesla-oauth2/blob/main/tesla.py

# tesla client id and secret which are everywhere on internet
CLIENT_ID = "81527cff06843c8634fdc09e8ac0abefb46ac849f38fe1e431c2ef2106796384"
# Avoid setting a User-Agent header that looks like a browser (such as Chrome or
# Safari). The SSO service has protections in place that will require executing
# JavaScript if a browser-like user agent is detected
UA = "Mozilla/5.0 (Linux; Android 10; Pixel 3 Build/QQ2A.200305.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/85.0.4183.81 Mobile Safari/537.36"
X_TESLA_USER_AGENT = "TeslaApp/3.10.9-433/adff2e065/android/10"

# Subsequent requests to the SSO service will require a "code verifier" and
# "code challenge". These are a random 86-character alphanumeric string and
# its SHA-256 hash encoded in URL-safe base64 (base64url).
# You will also need a stable state value for requests, which is a random
# string of any length
def gen_params():
    verifier_bytes = os.urandom(86)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier).digest()).rstrip(b"=")
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("utf-8")
    return code_verifier, code_challenge, state


# Either return a valid TeslaToken of None if login did fail
def GetTokenFromLoginPW(teslalogin, teslapw):
    headers = {
        "User-Agent": UA,
        "x-tesla-user-agent": X_TESLA_USER_AGENT,
        "X-Requested-With": "com.teslamotors.tesla",
    }

    # Step 1: Obtain the login page
    code_verifier, code_challenge, state = gen_params()

    # The request is made with a redirect_url of
    # "https://auth.tesla.com/void/callback", which is a non-existent page
    params = (
        ("client_id", "ownerapi"),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
        ("redirect_uri", "https://auth.tesla.com/void/callback"),
        ("response_type", "code"),
        ("scope", "openid email offline_access"),
        ("state", state),
    )

    session = requests.Session()
    resp = session.get("https://auth.tesla.com/oauth2/v3/authorize", headers=headers, params=params)

    if not(resp.ok and "<title>" in resp.text):
        return None

    # Step 2: Obtain an authorization code
    # This will simulate a user submitting the form from the previous request
    # in their browser. Ensure that the hidden <input>s are provided as POST
    # body parameters and the Cookie header is set
    csrf = re.search(r'name="_csrf".+value="([^"]+)"', resp.text).group(1)
    transaction_id = re.search(r'name="transaction_id".+value="([^"]+)"', resp.text).group(1)

    data = {
        "_csrf": csrf,
        "_phase": "authenticate",
        "_process": "1",
        "transaction_id": transaction_id,
        "cancel": "",
        "identity": teslalogin,
        "credential": teslapw,
    }

    resp = session.post(
        "https://auth.tesla.com/oauth2/v3/authorize", headers=headers, params=params, data=data,
        allow_redirects=False
    )
    # This will respond with a 302 HTTP response code, which will attempt to
    # redirect to the redirect_uri with additional query parameters added.
    # Returns 200 if login/PW is invalid
    if not (resp.ok and (resp.status_code == 302 or "<title>" in resp.text)):
        return None

    # Step 3: Exchange authorization code for bearer token
    # This new URL is located in the location header. You should not follow it, as
    # it is non-existent. Instead, you should parse this URL and extract the
    # code query parameter, which is your authorization code.
    code = parse_qs(resp.headers["location"])["https://auth.tesla.com/void/callback?code"]

    # This is a standard OAuth 2.0 Authorization Code exchange. This endpoint uses
    # JSON for the request and response bodies
    headers = {"user-agent": UA, "x-tesla-user-agent": X_TESLA_USER_AGENT}
    payload = {
        "grant_type": "authorization_code",
        "client_id": "ownerapi",
        "code_verifier": code_verifier.decode("utf-8"),
        "code": code,
        "redirect_uri": "https://auth.tesla.com/void/callback",
    }

    resp = session.post("https://auth.tesla.com/oauth2/v3/token", headers=headers, json=payload)
    resp_json = resp.json()
    access_token = resp_json["access_token"]

    # Step 4: Exchange bearer token for access token
    headers["authorization"] = "bearer " + access_token
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": CLIENT_ID,
    }
    resp = session.post("https://owner-api.teslamotors.com/oauth/token", headers=headers, json=payload)

    # save our tokens
    tokens = resp.json()
    teslatoken = TeslaToken()
    teslatoken.access_token = tokens["access_token"]
    teslatoken.expires_in = int(tokens["expires_in"])
    teslatoken.created_at = int(tokens["created_at"])
    teslatoken.refresh_token = tokens["refresh_token"]
    teslatoken.vehicle_id = None
    return teslatoken


# Either return a valid TeslaToken of None if login did fail
def GetTokenFromRefreshToken(refreshtoken):
    headers = {"user-agent": UA, "x-tesla-user-agent": X_TESLA_USER_AGENT}
    payload = {
        "grant_type": "refresh_token",
        "client_id": "ownerapi",
        "refresh_token": refreshtoken,
        "scope": "openid email offline_access",
    }
    session = requests.Session()

    resp = session.post("https://auth.tesla.com/oauth2/v3/token", headers=headers, json=payload)
    resp_json = resp.json()
    try:
        #will not be found if refresh token was invalid
        access_token = resp_json["access_token"]
    except KeyError:
        return None

    # Step 4: Exchange bearer token for access token
    headers["authorization"] = "bearer " + access_token
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": CLIENT_ID,
    }
    resp = session.post("https://owner-api.teslamotors.com/oauth/token", headers=headers, json=payload)

    # save our tokens
    tokens = resp.json()
    teslatoken = TeslaToken()
    teslatoken.access_token = tokens["access_token"]
    teslatoken.expires_in = int(tokens["expires_in"])
    teslatoken.created_at = int(tokens["created_at"])
    teslatoken.refresh_token = tokens["refresh_token"]
    teslatoken.vehicle_id = None
    return teslatoken
