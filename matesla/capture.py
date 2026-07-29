"""
TeslaFi-style capture: for known vehicles that are already online, pull
vehicle_data and persist snapshots for graphs.

Never wakes cars. Cron may tick every minute; this module spaces real Fleet
calls adaptively from local time + last known activity (see poll_interval_minutes).

Stops early on EXCEEDED_LIMIT so we do not hammer a disabled account.

Ops: each run builds French status messages (JSON ``messages`` + stdout) so
cron logs / the Django console show whether Tesla access worked and why not.
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from django.utils import timezone

from matesla.TeslaConnect import (
    VEHICLE_DATA_ENDPOINTS,
    GetProxyToUse,
    SaveDataHistory,
    TeslaFleetLimitException,
    api_url,
    fetch_vehicles_list,
    fleet_http_error_reason,
    get_vehicle_connectivity,
    is_fleet_limit_response,
    refresh_vehicle_states_from_list,
)
from matesla.TeslaOAuth import TeslaOAuthError, ensure_fresh_access_token
from matesla.TeslaState import TeslaState
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle

# Civil clock for night/day windows (household local time).
CAPTURE_TZ = ZoneInfo("Europe/Brussels")

# Night: 22:00 inclusive → 06:00 exclusive
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6

# Intervals in minutes (user policy).
INTERVAL_DRIVING_MIN = 2
INTERVAL_DC_CHARGE_MIN = 2
INTERVAL_AC_CHARGE_MIN = 10
INTERVAL_CABIN_MIN = 2  # user present, dog/camp/climate keeper
INTERVAL_SENTRY_MIN = 5  # sentry only (no cabin activity)
INTERVAL_ONLINE_IDLE_MIN = 10  # online but no cabin / sentry signal
INTERVAL_ASLEEP_DAY_MIN = 5
INTERVAL_NIGHT_DEFAULT_MIN = 30  # anything except driving at night

# DC heuristic: Supercharger / fast pack, or power well above typical AC.
DC_POWER_KW_MIN = 20.0
DRIVING_SPEED_MPH_MIN = 1.0
# Last snapshot older than this is not trusted for drive/charge/cabin/sentry.
# Otherwise a car that finished charging and went to sleep stays "ac_charge" forever.
ACTIVITY_SNAP_MAX_AGE_MIN = 15.0
# Min gap when forcing a poll because telemetry is stale while list says online.
STALE_ONLINE_FORCE_POLL_MIN = 2.0


def is_night(now: datetime | None = None) -> bool:
    local = (now or timezone.now()).astimezone(CAPTURE_TZ)
    h = local.hour
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR


def _latest_activity_snapshot(vehicle: TeslaVehicle) -> TeslaCarDataSnapshot | None:
    vin = (vehicle.vin or "").strip()
    if not vin:
        return None
    return (
        TeslaCarDataSnapshot.objects.filter(vin=vin)
        .order_by("-Date")
        .only(
            "Date",
            "speed",
            "shift_state",
            "charging_state",
            "charger_power",
            "fast_charger_present",
            "fast_charger_type",
            "charge_rate",
            "is_user_present",
            "sentry_mode",
            "climate_keeper_mode",
            "is_climate_on",
        )
        .first()
    )


def _snap_age_minutes(
    snap: TeslaCarDataSnapshot | None, now: datetime | None = None
) -> float | None:
    if snap is None or getattr(snap, "Date", None) is None:
        return None
    now = now or timezone.now()
    dt = snap.Date
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _snap_is_fresh(
    snap: TeslaCarDataSnapshot | None, now: datetime | None = None
) -> bool:
    age = _snap_age_minutes(snap, now)
    return age is not None and age <= ACTIVITY_SNAP_MAX_AGE_MIN


def _is_driving(snap: TeslaCarDataSnapshot | None) -> bool:
    if snap is None:
        return False
    try:
        if snap.speed is not None and float(snap.speed) >= DRIVING_SPEED_MPH_MIN:
            return True
    except (TypeError, ValueError):
        pass
    shift = (snap.shift_state or "").strip().upper()
    # D/R/N while "on road"; ignore Park and empty
    return shift in {"D", "R", "N"}


def _is_charging(snap: TeslaCarDataSnapshot | None) -> bool:
    if snap is None:
        return False
    cs = (snap.charging_state or "").strip()
    return cs in {"Charging", "Starting"}


def _is_dc_charging(snap: TeslaCarDataSnapshot | None) -> bool:
    if not _is_charging(snap):
        return False
    if snap.fast_charger_present:
        return True
    fct = (snap.fast_charger_type or "").strip().lower()
    if fct and fct not in {"", "none", "ac", "<invalid>"}:
        # e.g. Tesla, Combo, CHAdeMO
        if fct not in {"ac_single", "ac_three"}:
            return True
    try:
        if snap.charger_power is not None and float(snap.charger_power) >= DC_POWER_KW_MIN:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _is_cabin_active(snap: TeslaCarDataSnapshot | None) -> bool:
    """Someone in the car, dog/camp, or climate deliberately kept on."""
    if snap is None:
        return False
    if snap.is_user_present:
        return True
    # Stored as bool: True when climate_keeper_mode not "off" (dog/camp/on)
    if snap.climate_keeper_mode:
        return True
    if snap.is_climate_on:
        return True
    return False


def _is_sentry(snap: TeslaCarDataSnapshot | None) -> bool:
    return bool(snap is not None and snap.sentry_mode)


def activity_kind(
    vehicle: TeslaVehicle,
    snap: TeslaCarDataSnapshot | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """
    Choose the *next poll delay only* (not a live dashboard state).

    Kinds: driving | dc_charge | ac_charge | cabin | sentry | online_idle | asleep

    Inputs:
      - TeslaVehicle.state from the last /vehicles list (asleep wins immediately)
      - latest snapshot *if fresh* (≤ ACTIVITY_SNAP_MAX_AGE_MIN): what the car
        was doing last time we successfully read vehicle_data

    A stale “Charging” snapshot must not keep us on the AC schedule for hours
    after the car has gone to sleep or finished charging.
    """
    now = now or timezone.now()
    if snap is None:
        snap = _latest_activity_snapshot(vehicle)

    state = (vehicle.state or "").strip().lower()
    # Explicit sleep from list → long-ish spacing; do not trust old charge flags.
    if state == "asleep":
        return "asleep"
    # "offline" is unreliable on Fleet — do not treat it as sleep for spacing;
    # fall through to fresh snapshot / online_idle so we keep probing.

    # Only trust rich activity from a recent vehicle_data sample.
    if _snap_is_fresh(snap, now):
        if _is_driving(snap):
            return "driving"
        if _is_dc_charging(snap):
            return "dc_charge"
        if _is_charging(snap):
            return "ac_charge"
        if _is_cabin_active(snap):
            return "cabin"
        if _is_sentry(snap):
            return "sentry"

    if state == "online":
        return "online_idle"
    # offline / unknown with no fresh snap: keep checking on the online-idle cadence
    # (day 10 min) rather than the sleepy 5/30 min — we may still get vehicle_data.
    if state == "offline":
        return "online_idle"
    return "asleep"


def poll_interval_minutes(
    vehicle: TeslaVehicle,
    *,
    now: datetime | None = None,
    snap: TeslaCarDataSnapshot | None = None,
) -> int:
    """
    How long to wait between Fleet polls for this vehicle.

    Night (22h–6h local): 30 min, except driving → 2 min
    (AC charge at night stays 30 min — long sessions).

    Day: driving/DC/cabin 2, AC 10, sentry 5, online idle 10, asleep 5.
    """
    now = now or timezone.now()
    kind = activity_kind(vehicle, snap, now=now)
    night = is_night(now)

    if night:
        if kind == "driving":
            return INTERVAL_DRIVING_MIN
        return INTERVAL_NIGHT_DEFAULT_MIN

    if kind == "driving":
        return INTERVAL_DRIVING_MIN
    if kind == "dc_charge":
        return INTERVAL_DC_CHARGE_MIN
    if kind == "ac_charge":
        return INTERVAL_AC_CHARGE_MIN
    if kind == "cabin":
        return INTERVAL_CABIN_MIN
    if kind == "sentry":
        return INTERVAL_SENTRY_MIN
    if kind == "asleep":
        return INTERVAL_ASLEEP_DAY_MIN
    # online_idle or anything else online-ish
    return INTERVAL_ONLINE_IDLE_MIN


def vehicle_is_due(vehicle: TeslaVehicle, *, now: datetime | None = None) -> bool:
    """True if enough time has passed since last_polled_at for the current policy."""
    now = now or timezone.now()
    last = vehicle.last_polled_at
    if last is None:
        return True

    snap = _latest_activity_snapshot(vehicle)
    age = _snap_age_minutes(snap, now)
    state = (vehicle.state or "").strip().lower()
    # Online in the list but no fresh vehicle_data: re-poll soon (do not keep
    # trusting an hours-old "Charging" snapshot).
    if (
        state == "online"
        and age is not None
        and age > ACTIVITY_SNAP_MAX_AGE_MIN
        and now >= last + timedelta(minutes=STALE_ONLINE_FORCE_POLL_MIN)
    ):
        return True

    interval = timedelta(
        minutes=poll_interval_minutes(vehicle, now=now, snap=snap)
    )
    return now >= last + interval


def _mark_polled(vehicle: TeslaVehicle, when: datetime | None = None) -> None:
    TeslaVehicle.objects.filter(pk=vehicle.pk).update(
        last_polled_at=when or timezone.now()
    )


def _log(messages: list[str], line: str) -> None:
    """Collect for JSON response and echo to process stdout (runserver console)."""
    messages.append(line)
    print(line, flush=True)


def fetch_vehicle_data(
    access_token: str, vehicle_id: str
) -> tuple[dict | None, str | None]:
    """
    Returns (payload, None) on success.
    Raises TeslaFleetLimitException when the account is usage-disabled.
    Returns (None, reason_fr) on soft failure.
    """
    try:
        resp = requests.get(
            api_url(f"/api/1/vehicles/{vehicle_id}/vehicle_data"),
            params={"endpoints": VEHICLE_DATA_ENDPOINTS},
            headers={"Authorization": "Bearer " + access_token},
            proxies=GetProxyToUse(),
            verify=True,
            timeout=60,
        )
    except requests.exceptions.Timeout:
        return None, "vehicle_data: timeout réseau vers Fleet API"
    except requests.exceptions.ConnectionError as exc:
        return None, f"vehicle_data: erreur de connexion ({exc.__class__.__name__})"
    except requests.exceptions.RequestException as exc:
        return None, f"vehicle_data: erreur réseau ({exc})"

    if resp is None:
        return None, "vehicle_data: pas de réponse"
    if is_fleet_limit_response(resp.status_code, resp.text):
        raise TeslaFleetLimitException(
            status_code=resp.status_code, body=resp.text[:500]
        )
    if resp.status_code != 200:
        return None, fleet_http_error_reason(
            resp.status_code, resp.text, what="vehicle_data"
        )
    try:
        return json.loads(resp.text), None
    except json.JSONDecodeError:
        return None, "vehicle_data: JSON invalide"


def capture_one_vehicle(
    vehicle: TeslaVehicle,
    access_token: str,
    *,
    list_payload=None,
) -> tuple[str, str | None]:
    """
    Capture one vehicle when due.

    - List state **asleep**: skip vehicle_data (never wake; save API cost).
    - List state **online**, **offline**, or unknown: call vehicle_data.
      Fleet often marks cars offline while the app still has data; skipping
      offline entirely dropped end-of-charge and drives (only list polls).

    Returns (result, detail): 'saved' | 'skipped_offline' | 'skipped_error'.
    Raises TeslaFleetLimitException when Fleet usage limit is hit.
    """
    state = get_vehicle_connectivity(
        access_token, vehicle.api_id, list_payload=list_payload
    )
    state_norm = (state or "").strip().lower()

    # Only hard-skip when the list is explicitly asleep (same policy as status hub).
    if state_norm == "asleep":
        TeslaVehicle.objects.filter(pk=vehicle.pk).update(
            state="asleep",
            last_polled_at=timezone.now(),
        )
        return "skipped_offline", "état liste=asleep (pas de vehicle_data)"

    if state_norm and state_norm not in {"online", "offline"}:
        # Keep state for UI (e.g. "driving" never appears on list, but be safe)
        TeslaVehicle.objects.filter(pk=vehicle.pk).update(state=state or "")

    payload, err = fetch_vehicle_data(access_token, vehicle.api_id)
    if not payload:
        _mark_polled(vehicle)
        # 408 = vehicle not available (often asleep); record as offline-ish
        if err and "408" in err:
            TeslaVehicle.objects.filter(pk=vehicle.pk).update(state="asleep")
            return "skipped_offline", err
        if state_norm == "offline":
            TeslaVehicle.objects.filter(pk=vehicle.pk).update(state="offline")
            return "skipped_offline", err or "offline + vehicle_data en échec"
        return "skipped_error", err or "vehicle_data échoué sans détail"

    context = payload.get("response") or {}
    vin = context.get("vin") or vehicle.vin
    if not vin:
        _mark_polled(vehicle)
        return "skipped_error", "pas de VIN dans la réponse ni en base"

    # Prefer live state from vehicle_data when present
    live_state = context.get("state") or "online"
    TeslaVehicle.objects.filter(pk=vehicle.pk).update(
        state=live_state,
        display_name=context.get("display_name") or vehicle.display_name,
        vin=vin,
        last_polled_at=timezone.now(),
    )

    ret = TeslaState()
    ret.vin = vin
    ret.name = context.get("display_name") or vehicle.display_name or vin
    ret.vehicle_state = payload
    SaveDataHistory(ret)
    return "saved", None


def _summarize_tesla_access(
    stats: dict,
    messages: list[str],
    *,
    any_due: bool = True,
    no_vehicles: bool = False,
) -> None:
    """
    Set tesla_access* on stats and append a single closing line (no duplicate
    narrative already printed above).
    """
    if stats.get("fleet_limit"):
        stats["tesla_access"] = "failed"
        stats["tesla_access_ok"] = False
        detail = "Limite Fleet / compte désactivé (EXCEEDED_LIMIT ou équivalent)"
    elif stats.get("token_error") and not stats.get("list_ok"):
        stats["tesla_access"] = "failed"
        stats["tesla_access_ok"] = False
        detail = "Échec OAuth / refresh du token Tesla"
    elif stats.get("list_error"):
        stats["tesla_access"] = "failed"
        stats["tesla_access_ok"] = False
        detail = stats["list_error"]
    elif no_vehicles:
        stats["tesla_access"] = "not_called"
        stats["tesla_access_ok"] = None
        detail = "aucun véhicule en base"
    elif not any_due or stats.get("fleet_calls", 0) == 0:
        stats["tesla_access"] = "not_called"
        stats["tesla_access_ok"] = None
        detail = "aucune voiture due (intervalles adaptatifs)"
    elif stats.get("saved", 0) > 0 or stats.get("skipped_offline", 0) > 0:
        stats["tesla_access"] = "ok"
        stats["tesla_access_ok"] = True
        detail = "Accès Tesla OK"
        if stats.get("skipped_error"):
            detail += (
                f" (mais {stats['skipped_error']} vehicle_data en erreur — "
                "voir ci-dessus)"
            )
            stats["tesla_access"] = "partial"
    elif stats.get("skipped_error") and not stats.get("list_ok"):
        stats["tesla_access"] = "failed"
        stats["tesla_access_ok"] = False
        detail = "Échec d’accès Tesla (liste ou données)"
    elif stats.get("skipped_error"):
        stats["tesla_access"] = "partial"
        stats["tesla_access_ok"] = False
        detail = "Liste OK mais vehicle_data en échec"
    else:
        stats["tesla_access"] = "ok"
        stats["tesla_access_ok"] = True
        detail = "Accès Tesla OK"

    stats["tesla_access_detail"] = detail
    flag = {
        "ok": "OK",
        "partial": "PARTIEL",
        "failed": "ÉCHEC",
        "not_called": "NON TESTÉ",
    }.get(stats["tesla_access"], stats["tesla_access"])
    _log(messages, f"→ Accès Tesla: {flag} — {detail}")


def capture_all_online_vehicles() -> dict:
    """
    Walk vehicles; only call Fleet when at least one car is due under adaptive policy.
    One /vehicles list per user token, then vehicle_data only for due + online cars.
    """
    messages: list[str] = []
    stats: dict = {
        "saved": 0,
        "skipped_offline": 0,
        "skipped_error": 0,
        "skipped_wait": 0,
        "token_error": 0,
        "fleet_limit": 0,
        "fleet_calls": 0,
        "list_ok": False,
        "list_error": None,
        "messages": messages,
    }
    now = timezone.now()
    local = now.astimezone(CAPTURE_TZ)
    _log(
        messages,
        f"Capture {local.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"({'nuit' if is_night(now) else 'jour'})",
    )

    user_ids = list(
        TeslaVehicle.objects.order_by()
        .values_list("user_id", flat=True)
        .distinct()
    )

    if not user_ids:
        _summarize_tesla_access(stats, messages, any_due=False, no_vehicles=True)
        return stats

    any_due = False

    for user_id in user_ids:
        token = TeslaToken.objects.filter(user_id=user_id).first()
        if not token:
            _log(messages, f"user {user_id}: pas de TeslaToken en base")
            stats["token_error"] += 1
            continue

        vehicles = list(TeslaVehicle.objects.filter(user_id=user_id))
        due = [v for v in vehicles if vehicle_is_due(v, now=now)]
        for v in vehicles:
            if v not in due:
                stats["skipped_wait"] += 1
                label = (v.display_name or v.vin or v.api_id).strip()
                mins = poll_interval_minutes(v, now=now)
                kind = activity_kind(v)
                _log(
                    messages,
                    f"  {label}: attente (prochain poll ≥ {mins} min, kind={kind})",
                )

        if not due:
            continue

        any_due = True
        due_labels = ", ".join(
            (v.display_name or v.vin or v.api_id).strip() for v in due
        )
        _log(messages, f"user {user_id}: voitures dues → {due_labels}")

        try:
            token = ensure_fresh_access_token(token)
        except TeslaOAuthError as exc:
            stats["token_error"] += 1
            _log(
                messages,
                f"  ÉCHEC token OAuth (refresh): {exc}",
            )
            continue

        try:
            list_payload, list_err = fetch_vehicles_list(token.access_token)
            stats["fleet_calls"] += 1
        except TeslaFleetLimitException as exc:
            stats["fleet_limit"] += 1
            stats["fleet_calls"] += 1
            body = (getattr(exc, "body", None) or str(exc))[:200]
            _log(
                messages,
                f"  ÉCHEC accès Tesla: limite Fleet (liste) — {body}",
            )
            continue

        if list_payload is None:
            stats["list_error"] = list_err or "Liste véhicules: échec inconnu"
            _log(messages, f"  ÉCHEC accès Tesla: {stats['list_error']}")
            # Still try vehicle_data for due cars? Without list, connectivity unknown.
            # Prefer not to spam: stop this token for this tick.
            continue

        stats["list_ok"] = True
        n_cars = len(list_payload.get("response") or [])
        _log(messages, f"  Accès Tesla OK (liste /vehicles, {n_cars} véhicule(s))")

        try:
            refresh_vehicle_states_from_list(token.user_id, list_payload)
        except Exception:
            traceback.print_exc()
            _log(messages, "  Avertissement: refresh états locaux a échoué")

        due_ids = {v.pk for v in due}
        for vehicle in TeslaVehicle.objects.filter(user_id=user_id, pk__in=due_ids):
            label = (vehicle.display_name or vehicle.vin or vehicle.api_id).strip()
            try:
                result, detail = capture_one_vehicle(
                    vehicle,
                    token.access_token,
                    list_payload=list_payload,
                )
                stats["fleet_calls"] += 1
                stats[result] = stats.get(result, 0) + 1
                if result == "saved":
                    _log(messages, f"  {label}: capturé OK")
                elif result == "skipped_offline":
                    _log(messages, f"  {label}: pas online ({detail})")
                else:
                    _log(messages, f"  {label}: ERREUR capture — {detail}")
            except TeslaFleetLimitException as exc:
                stats["fleet_limit"] += 1
                stats["fleet_calls"] += 1
                body = (getattr(exc, "body", None) or str(exc))[:200]
                _log(
                    messages,
                    f"  {label}: ÉCHEC accès Tesla — limite Fleet — {body}",
                )
                break
            except Exception as exc:
                traceback.print_exc()
                stats["skipped_error"] += 1
                _mark_polled(vehicle)
                _log(messages, f"  {label}: ERREUR inattendue — {type(exc).__name__}: {exc}")

    _summarize_tesla_access(stats, messages, any_due=any_due)
    return stats
