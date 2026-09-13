"""Charge-cost config (localhost writes) and the Charges tab (read anywhere)."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user
from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as gettext
from django.views.decorators.http import require_http_methods

from matesla.charge_cost import CHARGE_COST_TZ, _haversine_m, price_sessions_starting_in
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.sqlite_guard import heavy_snapshot_read
from matesla.elia_dayahead import ensure_spots_for_dynamic_period
from matesla.models.ChargeCost import (
    COST_PARTIAL,
    COST_PRICED,
    ChargeCostSettings,
    ChargePlace,
    PlaceTariffPeriod,
    ROLE_HOME,
    ROLE_WORK,
    RULE_PLACE,
    RULE_SUPERCHARGER_INVOICE,
    TARIFF_DAY_NIGHT,
    TARIFF_DYNAMIC,
    TARIFF_FLAT,
    VehiclePlaceRole,
)
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaToken import TeslaVehicle
from matesla.models.VinHash import HashTheVin
from mysite.writable_access import is_writable_request

from personalstats.views import _chrome_or_error, _unknown_hashed_vin_response

# Open-ended tariff with no user-facing period (empty From/To in the form).
OPEN_TARIFF_FROM = date(2010, 1, 1)

def _rule_labels() -> dict[str, str]:
    # gettext at call time so ChargeCosts follows the active language.
    return {
        "home_flat": gettext("Home (flat)"),
        "home_day_night": gettext("Home (day / night)"),
        "home_dynamic": gettext("Home (dynamic)"),
        "work": gettext("Work"),
        "supercharger_invoice": gettext("Tesla Supercharger invoice"),
        "supercharger_rate": gettext("Supercharger average rate"),
        "place": gettext("Named place"),
        "other": gettext("Other chargers"),
        "unpriced": gettext("Unpriced"),
    }


def _rule_label(rule: str, place_name: str | None = None) -> str:
    if place_name:
        return place_name
    return _rule_labels().get(rule, rule)


def _duration_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} h"
    return f"{hours} h {mins:02d}"


def _supercharger_label(name: str) -> str:
    raw = (name or "").strip()
    label = gettext("Supercharger")
    if not raw:
        return label
    lower = raw.lower()
    if "supercharger" in lower or "superchargeur" in lower:
        return raw
    return f"{label} · {raw}"


def _cluster_identity(session, result, place_names: dict, sc_site=None) -> tuple[str, str]:
    if result.place_id and result.place_id in place_names:
        return f"p{result.place_id}", place_names[result.place_id]
    site = (getattr(session, "tesla_site_name", None) or "").strip()
    if not site and isinstance(sc_site, dict):
        site = (sc_site.get("name") or "").strip()
    if site:
        return f"s{site}", _supercharger_label(site)
    if getattr(session, "is_supercharger", False):
        return "sc", gettext("Supercharger")
    lat, lon = session.lat, session.lon
    if lat is not None and lon is not None:
        key = f"g{round(float(lat), 3):.3f}_{round(float(lon), 3):.3f}"
        from matesla.models.AddressFromLatLong import LookupCachedAddress

        addr = LookupCachedAddress(round(float(lat), 4), round(float(lon), 4))
        return key, addr or gettext("Other chargers")
    return "x", _rule_label(result.rule)


def _summarize_clusters(rows: list[dict], top_n: int = 5):
    buckets: dict[str, dict] = {}
    for row in rows:
        key = row["cluster_key"]
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "name": row["cluster_name"],
                "kwh": 0.0,
                "cost_eur": 0.0,
                "priced_kwh": 0.0,
                "n": 0,
            }
            buckets[key] = bucket
        bucket["n"] += 1
        kwh = row["kwh"] or 0.0
        bucket["kwh"] += kwh
        if row["cost_eur"] is not None:
            bucket["cost_eur"] += row["cost_eur"]
            bucket["priced_kwh"] += kwh
    ranked = sorted(buckets.values(), key=lambda item: item["kwh"], reverse=True)
    top = ranked[:top_n]
    top_keys = {item["key"] for item in top}
    for row in rows:
        row["in_top"] = row["cluster_key"] in top_keys
    rest = None
    leftover = ranked[top_n:]
    if leftover:
        rest = {
            "key": "rest",
            "name": gettext("The rest"),
            "kwh": sum(item["kwh"] for item in leftover),
            "cost_eur": sum(item["cost_eur"] for item in leftover),
            "priced_kwh": sum(item["priced_kwh"] for item in leftover),
            "n": sum(item["n"] for item in leftover),
        }
    for item in top + ([rest] if rest else []):
        if item["priced_kwh"] > 0:
            item["avg_eur"] = item["cost_eur"] / item["priced_kwh"]
        else:
            item["avg_eur"] = None
    return top, rest


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_time(raw: str, fallback: time) -> time:
    raw = (raw or "").strip()
    if not raw:
        return fallback
    try:
        parts = raw.split(":")
        return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError):
        return fallback


def _parse_float(raw: str):
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(raw: str):
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return None


def _vehicles_for_config(user) -> list[dict]:
    rows = []
    seen = set()
    if user is not None:
        for vehicle in TeslaVehicle.objects.filter(user=user).order_by(
            "-is_primary", "display_name", "vin"
        ):
            hashed = HashTheVin(vehicle.vin) if vehicle.vin else ""
            if not hashed:
                continue
            seen.add(hashed)
            rows.append({"hashed_vin": hashed, "label": vehicle.label})
    for info in TeslaCarInfo.objects.order_by("vin"):
        hashed = info.hashedVin or ""
        if not hashed or hashed in seen:
            continue
        seen.add(hashed)
        tail = (info.vin or "")[-6:] or hashed[:8]
        rows.append({"hashed_vin": hashed, "label": tail})
    return rows


def _settings_for_user(user) -> ChargeCostSettings:
    obj, _created = ChargeCostSettings.objects.get_or_create(user=user)
    return obj


CLUSTER_MERGE_M = 150.0
CLUSTER_SHOW_DEFAULT = 10
CLUSTER_SHOW_MAX = 200


def _as_aware_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            from zoneinfo import ZoneInfo

            return value.replace(tzinfo=ZoneInfo("UTC"))
        return value
    if isinstance(value, str):
        text = value.replace("T", " ", 1)
        try:
            parsed = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
        from zoneinfo import ZoneInfo

        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return None


def list_charge_clusters(hashed_vin: str) -> list[dict]:
    """
    Charge GPS clusters for one vehicle, largest first.

    Cells are 0.001° (~100 m), then merged if within CLUSTER_MERGE_M so a
    driveway split across two rounding tiles stays one place.
    """
    table = TeslaCarDataSnapshot._meta.db_table
    with heavy_snapshot_read():
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT round(latitude, 3), round(longitude, 3),
                       COUNT(*), MIN("Date"), MAX("Date")
                FROM {table}
                WHERE hashedVin = %s
                  AND latitude IS NOT NULL AND longitude IS NOT NULL
                  AND (charging_state IN ('Charging', 'Starting')
                       OR charger_power > 0.5)
                GROUP BY 1, 2
                ORDER BY 3 DESC
                """,
                [hashed_vin],
            )
            cells = cursor.fetchall()
    clusters: list[dict] = []
    for lat, lon, n, tmin, tmax in cells:
        if lat is None or lon is None or not n:
            continue
        lat_f, lon_f = float(lat), float(lon)
        merged = None
        for cluster in clusters:
            if _haversine_m(lat_f, lon_f, cluster["lat"], cluster["lon"]) <= CLUSTER_MERGE_M:
                merged = cluster
                break
        t0 = _as_aware_dt(tmin)
        t1 = _as_aware_dt(tmax)
        if merged is None:
            clusters.append(
                {
                    "lat": lat_f,
                    "lon": lon_f,
                    "n": int(n),
                    "tmin": t0,
                    "tmax": t1,
                }
            )
            continue
        total = merged["n"] + int(n)
        merged["lat"] = (merged["lat"] * merged["n"] + lat_f * n) / total
        merged["lon"] = (merged["lon"] * merged["n"] + lon_f * n) / total
        merged["n"] = total
        if t0 and (merged["tmin"] is None or t0 < merged["tmin"]):
            merged["tmin"] = t0
        if t1 and (merged["tmax"] is None or t1 > merged["tmax"]):
            merged["tmax"] = t1
    clusters.sort(key=lambda row: row["n"], reverse=True)
    return clusters


def _annotate_cluster_places(clusters: list[dict], user, hashed_vin: str) -> None:
    from matesla.models.AddressFromLatLong import LookupCachedAddress

    places = list(ChargePlace.objects.filter(user=user).prefetch_related("tariff_periods"))
    roles = list(
        VehiclePlaceRole.objects.filter(
            place__user=user, hashed_vin=hashed_vin
        ).select_related("place")
    )
    for cluster in clusters:
        cluster["lat"] = round(cluster["lat"], 5)
        cluster["lon"] = round(cluster["lon"], 5)
        cluster["address"] = LookupCachedAddress(
            round(cluster["lat"], 4), round(cluster["lon"], 4)
        )
        cluster["place"] = None
        cluster["role"] = None
        cluster["kind"] = ""
        best_d = CLUSTER_MERGE_M + 1
        for place in places:
            dist = _haversine_m(
                cluster["lat"], cluster["lon"], place.latitude, place.longitude
            )
            if dist <= place.radius_m and dist < best_d:
                best_d = dist
                cluster["place"] = place
        if cluster["place"]:
            for assignment in roles:
                if assignment.place_id == cluster["place"].id:
                    cluster["role"] = assignment
                    cluster["kind"] = assignment.role
                    break
            if not cluster["kind"]:
                cluster["kind"] = "other"
        if cluster["tmin"]:
            cluster["data_from_iso"] = (
                cluster["tmin"].astimezone(CHARGE_COST_TZ).date().isoformat()
            )
        else:
            cluster["data_from_iso"] = ""
        if cluster["tmax"]:
            cluster["data_to_iso"] = (
                cluster["tmax"].astimezone(CHARGE_COST_TZ).date().isoformat()
            )
        else:
            cluster["data_to_iso"] = ""
        assignment = cluster["role"]
        if assignment:
            cluster["from_iso"] = assignment.valid_from.isoformat()
            cluster["to_iso"] = (
                assignment.valid_to.isoformat() if assignment.valid_to else ""
            )
        else:
            cluster["from_iso"] = cluster["data_from_iso"]
            cluster["to_iso"] = ""
        cluster["label_guess"] = cluster["address"] or ""
        cluster["price_eur"] = None
        cluster["price_hint"] = ""
        cluster["price_summary"] = ""
        place = cluster["place"]
        if place:
            tariff = _active_tariff(place)
            cluster["price_summary"] = _tariff_summary(tariff)
            if tariff:
                if tariff.mode == TARIFF_FLAT and tariff.flat_eur_per_kwh is not None:
                    cluster["price_eur"] = tariff.flat_eur_per_kwh
                elif tariff.mode == TARIFF_DAY_NIGHT:
                    cluster["price_eur"] = tariff.day_eur_per_kwh
                    cluster["price_hint"] = gettext("Day / night — details")
                elif tariff.mode == TARIFF_DYNAMIC:
                    cents = tariff.dynamic_surcharge_cents
                    if cents is not None:
                        cluster["price_hint"] = gettext("spot + %(n)s ¢") % {"n": cents}


def _active_tariff(place):
    periods = list(place.tariff_periods.all())
    if not periods:
        return None
    today = date.today()
    matching = [
        period
        for period in periods
        if period.valid_from <= today
        and (period.valid_to is None or period.valid_to >= today)
    ]
    return matching[-1] if matching else periods[-1]


def _tariff_summary(tariff) -> str:
    if tariff is None:
        return gettext("No price yet")
    if tariff.mode == TARIFF_FLAT and tariff.flat_eur_per_kwh is not None:
        return f"{tariff.flat_eur_per_kwh:g} €/kWh"
    if tariff.mode == TARIFF_DAY_NIGHT:
        day_p = tariff.day_eur_per_kwh
        night_p = tariff.night_eur_per_kwh
        if day_p is not None and night_p is not None:
            return f"{day_p:g} / {night_p:g} €/kWh"
        if day_p is not None:
            return f"{day_p:g} €/kWh"
    if tariff.mode == TARIFF_DYNAMIC and tariff.dynamic_surcharge_cents is not None:
        return gettext("spot + %(n)s ¢") % {"n": tariff.dynamic_surcharge_cents}
    return gettext("No price yet")


def _period_from_label(period) -> str:
    if period.valid_from == OPEN_TARIFF_FROM:
        return gettext("From the start")
    return date_format(period.valid_from, "j N Y")


def _timeline_for_place(place):
    """Sequential rate cards + holes, so the last open-ended rate stays in force."""
    periods = list(place.tariff_periods.all().order_by("valid_from", "id"))
    items = []
    for index, period in enumerate(periods):
        if index:
            prev = periods[index - 1]
            if prev.valid_to:
                hole_from = prev.valid_to + timedelta(days=1)
                if hole_from < period.valid_from:
                    hole_to = period.valid_from - timedelta(days=1)
                    items.append(
                        {
                            "gap": True,
                            "from_iso": hole_from.isoformat(),
                            "to_iso": hole_to.isoformat(),
                            "from_label": date_format(hole_from, "j N Y"),
                            "to_label": date_format(hole_to, "j N Y"),
                        }
                    )
        items.append(
            {
                "gap": False,
                "period": period,
                "is_last": index == len(periods) - 1,
                "open_ended": period.valid_to is None,
                "from_label": _period_from_label(period),
                "to_label": (
                    gettext("still in force")
                    if period.valid_to is None
                    else date_format(period.valid_to, "j N Y")
                ),
                "summary": _tariff_summary(period),
            }
        )
    next_from = None
    needs_future = not periods
    if periods:
        last = periods[-1]
        if last.valid_to:
            next_from = last.valid_to + timedelta(days=1)
            needs_future = True
    return items, next_from, needs_future


def _close_open_ended_before(place, new_from: date, exclude_id=None) -> None:
    qs = place.tariff_periods.filter(valid_to__isnull=True)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    for prev in qs:
        if prev.valid_from < new_from:
            end = new_from - timedelta(days=1)
            if end >= prev.valid_from:
                prev.valid_to = end
                prev.save(update_fields=["valid_to"])


@require_http_methods(["GET", "POST"])
def ChargeCostsSetup(request):
    """Create/edit places, roles and tariffs. Local writable host + login only."""
    if not is_writable_request(request):
        return HttpResponseNotFound()
    user = get_user(request)
    if not user.is_authenticated:
        return redirect("login")

    hashed_vin = (request.GET.get("vin") or request.POST.get("vin") or "").strip()
    show = _parse_int(request.GET.get("show") or request.POST.get("show")) or CLUSTER_SHOW_DEFAULT
    show = max(10, min(show, CLUSTER_SHOW_MAX))
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        reopen_place = None
        try:
            reopen_place = _handle_setup_post(request, user, action)
        except ValueError as exc:
            messages.error(request, str(exc))
            reopen_place = _parse_int(request.POST.get("place_id"))
        url = reverse("PersoChargeCostsSetup")
        params = []
        if hashed_vin:
            params.append(f"vin={hashed_vin}")
        if show != CLUSTER_SHOW_DEFAULT:
            params.append(f"show={show}")
        if reopen_place:
            params.append(f"place={reopen_place}")
        if params:
            url = f"{url}?{'&'.join(params)}"
        return redirect(url)

    context = {
        "hashedVin": hashed_vin or None,
        "settings": _settings_for_user(user),
        "places": list(
            ChargePlace.objects.filter(user=user)
            .prefetch_related("tariff_periods", "vehicle_roles")
            .order_by("name")
        ),
        "vehicles": _vehicles_for_config(user),
        "role_home": ROLE_HOME,
        "role_work": ROLE_WORK,
        "mode_flat": TARIFF_FLAT,
        "mode_day_night": TARIFF_DAY_NIGHT,
        "mode_dynamic": TARIFF_DYNAMIC,
    }
    for place in context["places"]:
        place.summary = _tariff_summary(_active_tariff(place))
        timeline, next_from, needs_future = _timeline_for_place(place)
        place.timeline = timeline
        place.next_from_iso = next_from.isoformat() if next_from else ""
        place.needs_future = needs_future
    context["vehicles"] = _vehicles_for_config(user)
    context["open_place_id"] = _parse_int(request.GET.get("place"))
    context["show"] = show
    context["clusters"] = []
    context["cluster_total"] = 0
    context["cluster_has_more"] = False
    context["cluster_next_show"] = None
    chrome_vin = hashed_vin
    if not chrome_vin:
        from matesla.TeslaConnect import resolve_active_vehicle

        active = resolve_active_vehicle(user, request)
        if active and active.vin:
            chrome_vin = HashTheVin(active.vin)
            context["hashedVin"] = chrome_vin
    if chrome_vin:
        chrome, err = _chrome_or_error(request, chrome_vin)
        if not err:
            context.update(chrome)
            context.setdefault("hashedVin", chrome_vin)
        all_clusters = list_charge_clusters(chrome_vin)
        _annotate_cluster_places(all_clusters, user, chrome_vin)
        context["cluster_total"] = len(all_clusters)
        context["clusters"] = all_clusters[:show]
        if len(all_clusters) > show:
            context["cluster_has_more"] = True
            context["cluster_next_show"] = min(show + 10, CLUSTER_SHOW_MAX, len(all_clusters))
    return render(request, "personalstats/charge_costs_setup.html", context)


def _handle_setup_post(request, user, action: str):
    if action == "tag_cluster":
        hashed = (request.POST.get("hashed_vin") or request.POST.get("vin") or "").strip()
        if len(hashed) < 16:
            raise ValueError(gettext("Choose a vehicle."))
        lat = _parse_float(request.POST.get("latitude"))
        lon = _parse_float(request.POST.get("longitude"))
        if lat is None or lon is None:
            raise ValueError(gettext("Unknown place."))
        role = (request.POST.get("role") or "other").strip()
        if role not in (ROLE_HOME, ROLE_WORK, "other"):
            role = "other"
        name = (request.POST.get("name") or "").strip()
        if not name:
            from matesla.models.AddressFromLatLong import LookupCachedAddress

            name = LookupCachedAddress(round(lat, 4), round(lon, 4)) or gettext("Place")
        place = None
        best_d = CLUSTER_MERGE_M + 1
        for candidate in ChargePlace.objects.filter(user=user):
            dist = _haversine_m(lat, lon, candidate.latitude, candidate.longitude)
            if dist <= candidate.radius_m and dist < best_d:
                best_d = dist
                place = candidate
        if place is None:
            place = ChargePlace.objects.create(
                user=user,
                name=name[:128],
                latitude=lat,
                longitude=lon,
                radius_m=150,
            )
        elif name and place.name != name:
            place.name = name[:128]
            place.save(update_fields=["name"])
        price = _parse_float(request.POST.get("price_eur_per_kwh"))
        valid_from = _parse_date(request.POST.get("valid_from")) or OPEN_TARIFF_FROM
        valid_to = _parse_date(request.POST.get("valid_to"))
        if price is not None:
            period = (
                place.tariff_periods.filter(mode=TARIFF_FLAT)
                .order_by("-valid_from")
                .first()
            )
            if period is None:
                PlaceTariffPeriod.objects.create(
                    place=place,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    mode=TARIFF_FLAT,
                    flat_eur_per_kwh=price,
                )
            else:
                period.flat_eur_per_kwh = price
                period.valid_from = valid_from
                period.valid_to = valid_to
                period.save()
        if role in (ROLE_HOME, ROLE_WORK):
            valid_from = _parse_date(request.POST.get("valid_from"))
            if valid_from is None:
                valid_from = OPEN_TARIFF_FROM
            VehiclePlaceRole.objects.filter(hashed_vin=hashed, place=place).exclude(
                role=role
            ).delete()
            assignment, created = VehiclePlaceRole.objects.get_or_create(
                hashed_vin=hashed,
                place=place,
                role=role,
                defaults={
                    "valid_from": valid_from,
                    "valid_to": _parse_date(request.POST.get("valid_to")),
                },
            )
            if not created:
                assignment.valid_from = valid_from
                assignment.valid_to = _parse_date(request.POST.get("valid_to"))
                assignment.save()
            messages.success(request, gettext("Home / work assignment saved."))
        else:
            VehiclePlaceRole.objects.filter(hashed_vin=hashed, place=place).delete()
            messages.success(request, gettext("Place saved."))
        return

    if action == "save_rates":
        settings = _settings_for_user(user)
        settings.other_eur_per_kwh = _parse_float(request.POST.get("other_eur_per_kwh"))
        settings.supercharger_eur_per_kwh = _parse_float(
            request.POST.get("supercharger_eur_per_kwh")
        )
        settings.save()
        messages.success(request, gettext("Rates saved."))
        return

    if action == "save_place":
        place_id = _parse_int(request.POST.get("place_id"))
        name = (request.POST.get("name") or "").strip()
        lat = _parse_float(request.POST.get("latitude"))
        lon = _parse_float(request.POST.get("longitude"))
        radius = _parse_int(request.POST.get("radius_m")) or 150
        if not name or lat is None or lon is None:
            raise ValueError(gettext("Name, latitude and longitude are required."))
        if place_id:
            place = ChargePlace.objects.filter(pk=place_id, user=user).first()
            if not place:
                raise ValueError(gettext("Unknown place."))
        else:
            place = ChargePlace(user=user)
        place.name = name[:128]
        place.latitude = lat
        place.longitude = lon
        place.radius_m = max(10, min(radius, 5000))
        place.save()
        messages.success(request, gettext("Place saved."))
        return

    if action == "delete_place":
        place_id = _parse_int(request.POST.get("place_id"))
        deleted, _ = ChargePlace.objects.filter(pk=place_id, user=user).delete()
        if deleted:
            messages.success(request, gettext("Place deleted."))
        return

    if action == "save_tariff":
        place_id = _parse_int(request.POST.get("place_id"))
        place = ChargePlace.objects.filter(pk=place_id, user=user).first()
        if not place:
            raise ValueError(gettext("Unknown place."))
        tariff_id = _parse_int(request.POST.get("tariff_id"))
        posted_from = (request.POST.get("valid_from") or "").strip()
        posted_to = (request.POST.get("valid_to") or "").strip()
        valid_from = _parse_date(posted_from)
        valid_to = _parse_date(posted_to)
        mode = (request.POST.get("mode") or TARIFF_FLAT).strip()
        if mode not in (TARIFF_FLAT, TARIFF_DAY_NIGHT, TARIFF_DYNAMIC):
            mode = TARIFF_FLAT
        # Empty field (placeholder "0") is 0, not "missing".
        flat_eur = _parse_float(request.POST.get("flat_eur_per_kwh"))
        day_eur = _parse_float(request.POST.get("day_eur_per_kwh"))
        night_eur = _parse_float(request.POST.get("night_eur_per_kwh"))
        cents = _parse_int(request.POST.get("dynamic_surcharge_cents"))
        if mode == TARIFF_FLAT and flat_eur is None:
            flat_eur = 0.0
        elif mode == TARIFF_DAY_NIGHT:
            if day_eur is None:
                day_eur = 0.0
            if night_eur is None:
                night_eur = 0.0
        elif mode == TARIFF_DYNAMIC and cents is None:
            cents = 0
        if tariff_id:
            period = PlaceTariffPeriod.objects.filter(
                pk=tariff_id, place=place
            ).first()
            if not period:
                raise ValueError(gettext("Unknown tariff period."))
            if valid_from is None:
                valid_from = period.valid_from
        else:
            last = place.tariff_periods.order_by("-valid_from", "-id").first()
            if valid_from is None and last and last.valid_to is None:
                period = last
                valid_from = last.valid_from
            else:
                if valid_from is None:
                    if last and last.valid_to:
                        valid_from = last.valid_to + timedelta(days=1)
                    else:
                        valid_from = OPEN_TARIFF_FROM
                period = PlaceTariffPeriod(place=place)
                _close_open_ended_before(place, valid_from)
        period.valid_from = valid_from
        period.valid_to = valid_to
        period.mode = mode
        period.flat_eur_per_kwh = flat_eur
        period.day_eur_per_kwh = day_eur
        period.night_eur_per_kwh = night_eur
        period.night_start = _parse_time(request.POST.get("night_start"), time(22, 0))
        period.night_end = _parse_time(request.POST.get("night_end"), time(7, 0))
        period.dynamic_surcharge_cents = cents
        period.save()
        if mode == TARIFF_DYNAMIC:
            summary = ensure_spots_for_dynamic_period(valid_from, valid_to)
            messages.success(
                request,
                gettext(
                    "Tariff saved. Day-ahead prices: %(ok)s day(s) fetched, "
                    "%(skipped)s already cached, %(failed)s failed."
                )
                % {
                    "ok": summary.get("days_ok", 0),
                    "skipped": summary.get("days_skipped", 0),
                    "failed": summary.get("days_failed", 0),
                },
            )
            if summary.get("days_truncated"):
                messages.warning(
                    request,
                    gettext(
                        "Price history is long; %(n)s older day(s) were not fetched. "
                        "Save again to continue."
                    )
                    % {"n": summary["days_truncated"]},
                )
        else:
            messages.success(request, gettext("Tariff saved."))
        return place.id

    if action == "delete_tariff":
        tariff_id = _parse_int(request.POST.get("tariff_id"))
        period = (
            PlaceTariffPeriod.objects.filter(pk=tariff_id, place__user=user)
            .select_related("place")
            .first()
        )
        place_id = period.place_id if period else None
        if period:
            period.delete()
            messages.success(request, gettext("Tariff period deleted."))
        return place_id

    if action == "save_role":
        place_id = _parse_int(request.POST.get("place_id"))
        place = ChargePlace.objects.filter(pk=place_id, user=user).first()
        if not place:
            raise ValueError(gettext("Unknown place."))
        hashed = (request.POST.get("hashed_vin") or "").strip()
        if len(hashed) < 16:
            raise ValueError(gettext("Choose a vehicle."))
        role = (request.POST.get("role") or ROLE_HOME).strip()
        if role not in (ROLE_HOME, ROLE_WORK):
            role = ROLE_HOME
        valid_from = _parse_date(request.POST.get("valid_from"))
        if valid_from is None:
            raise ValueError(gettext("Start date is required."))
        role_id = _parse_int(request.POST.get("role_id"))
        if role_id:
            assignment = VehiclePlaceRole.objects.filter(
                pk=role_id, place__user=user
            ).first()
            if not assignment:
                raise ValueError(gettext("Unknown assignment."))
        else:
            assignment = VehiclePlaceRole(place=place)
        assignment.place = place
        assignment.hashed_vin = hashed
        assignment.role = role
        assignment.valid_from = valid_from
        assignment.valid_to = _parse_date(request.POST.get("valid_to"))
        assignment.save()
        messages.success(request, gettext("Home / work assignment saved."))
        return

    if action == "delete_role":
        role_id = _parse_int(request.POST.get("role_id"))
        VehiclePlaceRole.objects.filter(pk=role_id, place__user=user).delete()
        messages.success(request, gettext("Assignment deleted."))
        return

    raise ValueError(gettext("Unknown action."))


@require_http_methods(["GET"])
def ChargeCosts(request, hashedVin):
    """Month / year charge list with persisted € (read-only on remote hosts)."""
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied
    context, chrome_error = _chrome_or_error(request, hashedVin)
    if chrome_error:
        return chrome_error

    today = datetime.now(CHARGE_COST_TZ).date()
    year_raw = (request.GET.get("year") or "").strip()
    month_raw = (request.GET.get("month") or "").strip()
    view = "month"
    year = today.year
    month = today.month
    if year_raw.isdigit() and not month_raw:
        view = "year"
        year = int(year_raw)
        if year < 2010 or year > today.year + 1:
            from personalstats.views import _invalid_query_response

            return _invalid_query_response("Invalid year")
        window_start = datetime(year, 1, 1, tzinfo=CHARGE_COST_TZ)
        window_end = datetime(year + 1, 1, 1, tzinfo=CHARGE_COST_TZ)
        prev_year, next_year = year - 1, year + 1
        prev_month = next_month = None
    else:
        if month_raw:
            try:
                year_s, month_s = month_raw.split("-", 1)
                year, month = int(year_s), int(month_s)
            except ValueError:
                from personalstats.views import _invalid_query_response

                return _invalid_query_response("Invalid month")
        if month < 1 or month > 12 or year < 2010 or year > today.year + 1:
            from personalstats.views import _invalid_query_response

            return _invalid_query_response("Invalid month")
        window_start = datetime(year, month, 1, tzinfo=CHARGE_COST_TZ)
        if month == 12:
            window_end = datetime(year + 1, 1, 1, tzinfo=CHARGE_COST_TZ)
        else:
            window_end = datetime(year, month + 1, 1, tzinfo=CHARGE_COST_TZ)
        prev_d = window_start.date() - timedelta(days=1)
        prev_month = f"{prev_d.year:04d}-{prev_d.month:02d}"
        last = date(year, month, monthrange(year, month)[1]) + timedelta(days=1)
        next_month = f"{last.year:04d}-{last.month:02d}"
        prev_year = next_year = None

    sc_match = None
    try:
        from django.core.cache import cache as django_cache

        from matesla.superchargers import CACHE_KEY as SC_CACHE_KEY
        from matesla.superchargers import nearest_supercharger

        if django_cache.get(SC_CACHE_KEY) is not None:

            def sc_match(lat, lon):
                return nearest_supercharger(lat, lon)

    except Exception:
        sc_match = None

    priced_rows = price_sessions_starting_in(
        hashedVin, window_start, window_end, supercharger_match=sc_match
    )
    place_ids = {result.place_id for _, result in priced_rows if result.place_id}
    place_names = {
        row.id: row.name
        for row in ChargePlace.objects.filter(pk__in=place_ids).only("id", "name")
    } if place_ids else {}
    rows = []
    total_eur = 0.0
    priced_kwh = 0.0
    unpriced_kwh = 0.0
    for session, result in priced_rows:
        kwh = session.kwh or 0.0
        if result.status in (COST_PRICED, COST_PARTIAL) and result.cost_eur is not None:
            total_eur += result.cost_eur
            priced_kwh += result.priced_kwh or kwh
            unpriced_kwh += result.missing_kwh or 0.0
        else:
            unpriced_kwh += kwh
        local_start = session.start.astimezone(CHARGE_COST_TZ)
        local_end = session.end.astimezone(CHARGE_COST_TZ)
        minutes = max(
            0, int(round((session.end - session.start).total_seconds() / 60.0))
        )
        sc_site = None
        if sc_match and session.lat is not None and session.lon is not None:
            try:
                sc_site = sc_match(session.lat, session.lon)
            except Exception:
                sc_site = None
        place_name = (
            (place_names.get(result.place_id) if result.place_id else None)
            or session.tesla_site_name
            or (sc_site.get("name") if isinstance(sc_site, dict) else None)
        )
        cluster_key, cluster_name = _cluster_identity(
            session, result, place_names, sc_site=sc_site
        )
        rows.append(
            {
                "start": session.start,
                "day": local_start.date(),
                "day_label": date_format(local_start, "j N"),
                "month_key": f"{local_start.year:04d}-{local_start.month:02d}",
                "month_label": date_format(local_start, "F Y"),
                "start_local": local_start.strftime("%H:%M"),
                "end_local": local_end.strftime("%H:%M"),
                "day_iso": local_start.date().isoformat(),
                "minutes": minutes,
                "duration_label": _duration_label(minutes),
                "kwh": session.kwh,
                "cost_eur": result.cost_eur,
                "status": result.status,
                "rule": result.rule,
                "rule_label": _rule_label(result.rule, place_name),
                "cluster_key": cluster_key,
                "cluster_name": cluster_name,
                "spans_midnight": local_start.date() != local_end.date(),
            }
        )

    top_clusters, rest_cluster = _summarize_clusters(rows)
    kwh_all = priced_kwh + unpriced_kwh
    context.update(
        {
            "hashedVin": hashedVin,
            "view": view,
            "year": year,
            "month": month,
            "month_iso": f"{year:04d}-{month:02d}",
            "prev_month": prev_month,
            "next_month": next_month,
            "prev_year": prev_year,
            "next_year": next_year,
            "rows": rows,
            "top_clusters": top_clusters,
            "rest_cluster": rest_cluster,
            "total_eur": total_eur,
            "priced_kwh": priced_kwh,
            "unpriced_kwh": unpriced_kwh,
            "kwh_all": kwh_all,
            "avg_eur": (total_eur / priced_kwh) if priced_kwh else None,
            "pct_priced": (100.0 * priced_kwh / kwh_all) if kwh_all else None,
            "allow_setup": is_writable_request(request),
            "max_year": today.year + 1,
        }
    )
    return render(request, "personalstats/charge_costs.html", context)
