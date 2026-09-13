"""Where-did-I-go page: place/region + date range → civil days + map."""

from __future__ import annotations

import json
from datetime import date, datetime

from django.shortcuts import render
from django.utils.formats import date_format
from django.utils.translation import gettext as gettext
from django.views.decorators.http import require_GET

from matesla.models.AddressFromLatLong import ForwardGeocode, GeocodeHit
from matesla.place_search import (
    MAX_RANGE_DAYS,
    PLACE_SEARCH_TZ,
    civil_bounds,
    days_from_snapshots,
    fetch_snapshots_in_bbox,
    month_bounds,
    order_hits_by_presence,
    range_too_long,
    summer_bounds,
    traces_from_snapshots,
)
from matesla.units import get_distance_unit, is_km, km_to_display, unit_labels
from personalstats.views import (
    _chrome_or_error,
    _invalid_query_response,
    _parse_day_string,
    _unknown_hashed_vin_response,
)


def _today() -> date:
    return datetime.now(PLACE_SEARCH_TZ).date()


def _preset_dates(today: date | None = None) -> dict:
    today = today or _today()
    this_from, this_to = month_bounds(today.year, today.month)
    this_to = min(this_to, today)
    if today.month == 1:
        last_from, last_to = month_bounds(today.year - 1, 12)
    else:
        last_from, last_to = month_bounds(today.year, today.month - 1)
    summer_year = today.year if today.month >= 6 else today.year - 1
    summer_from, summer_to = summer_bounds(summer_year)
    return {
        "this_month_from": this_from.isoformat(),
        "this_month_to": this_to.isoformat(),
        "last_month_from": last_from.isoformat(),
        "last_month_to": last_to.isoformat(),
        "summer_from": summer_from.isoformat(),
        "summer_to": summer_to.isoformat(),
        "summer_year": summer_year,
    }


def _kind_label(kind: str) -> str:
    return {
        "region": gettext("Region"),
        "city": gettext("City"),
        "county": gettext("County"),
    }.get(kind, "")


def _error_message(code: str | None) -> str | None:
    if code == "quota":
        return gettext("Geocoding quota reached. Try again later.")
    if code == "network":
        return gettext("Could not look up this place.")
    if code == "empty":
        return gettext("No matching place. Try a city or region name.")
    return None


@require_GET
def PlaceSearch(request, hashedVin):
    """
    Form: free-text place/region + from/to dates.
    Forward geocode once → bbox → snapshots in that box → civil days + map.
    """
    denied = _unknown_hashed_vin_response(request, hashedVin)
    if denied:
        return denied
    context, chrome_error = _chrome_or_error(request, hashedVin)
    if chrome_error:
        return chrome_error

    today = _today()
    query = (request.GET.get("q") or "").strip()
    if len(query) > 120:
        query = query[:120]
    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    pick_raw = (request.GET.get("c") or "").strip()

    start_day = _parse_day_string(raw_from) if raw_from else None
    end_day = _parse_day_string(raw_to) if raw_to else None
    if raw_from and start_day is None:
        return _invalid_query_response(
            gettext("Invalid date. Use DD/MM/YYYY or YYYY-MM-DD.")
        )
    if raw_to and end_day is None:
        return _invalid_query_response(
            gettext("Invalid date. Use DD/MM/YYYY or YYYY-MM-DD.")
        )
    # Empty "To" → that one civil day (Namur on 31 Dec, not a range).
    if start_day is not None and end_day is None:
        end_day = start_day

    unit = get_distance_unit(request)
    labels = unit_labels(unit)
    context.update(
        {
            "hashedVin": hashedVin,
            "query": query,
            "from_iso": start_day.isoformat() if start_day else "",
            "to_iso": end_day.isoformat() if end_day else "",
            "presets": _preset_dates(today),
            "searched": False,
            "days": [],
            "candidates": [],
            "chosen": None,
            "path_json": "[]",
            "markers_json": "[]",
            "error": None,
            "empty": False,
            "u_dist": labels["distance"],
            "is_metric": is_km(unit),
        }
    )

    if not query:
        return render(request, "personalstats/place_search.html", context)

    if start_day is None:
        context["error"] = gettext("Enter a place and a date range.")
        return render(request, "personalstats/place_search.html", context)

    if end_day < start_day:
        start_day, end_day = end_day, start_day
        context["from_iso"] = start_day.isoformat()
        context["to_iso"] = end_day.isoformat()

    if range_too_long(start_day, end_day):
        context["error"] = gettext("Date range cannot exceed %(n)s days.") % {
            "n": MAX_RANGE_DAYS
        }
        return render(request, "personalstats/place_search.html", context)

    geo = ForwardGeocode(query)
    if geo.error and not geo.hits:
        context["error"] = _error_message(geo.error)
        return render(request, "personalstats/place_search.html", context)

    window_start, window_end = civil_bounds(start_day, end_day)
    hits = order_hits_by_presence(
        hashedVin, window_start, window_end, list(geo.hits)
    )
    if not hits:
        context["error"] = _error_message(geo.error or "empty")
        return render(request, "personalstats/place_search.html", context)
    pick = 0
    if pick_raw.isdigit():
        pick = int(pick_raw)
    if pick < 0 or pick >= len(hits):
        pick = 0
    chosen: GeocodeHit = hits[pick]
    candidates = []
    for index, hit in enumerate(hits):
        candidates.append(
            {
                "index": index,
                "label": hit.label,
                "kind": hit.kind,
                "kind_label": _kind_label(hit.kind),
                "selected": index == pick,
            }
        )

    rows = fetch_snapshots_in_bbox(
        hashedVin,
        window_start,
        window_end,
        chosen.south,
        chosen.north,
        chosen.west,
        chosen.east,
    )
    day_hits = days_from_snapshots(rows)
    traces = traces_from_snapshots(rows)
    polylines = [segment for segment in traces if len(segment) >= 2]
    markers = [segment[0] for segment in traces if len(segment) == 1]

    days = []
    for hit in day_hits:
        km_display = km_to_display(hit.km, unit) if hit.km is not None else None
        days.append(
            {
                "day": hit.day,
                "day_iso": hit.day.isoformat(),
                "day_label": date_format(hit.day, "l j N Y"),
                "point_count": hit.point_count,
                "distance": round(km_display, 1) if km_display is not None else None,
            }
        )

    context.update(
        {
            "searched": True,
            "days": days,
            "candidates": candidates,
            "chosen": {
                "label": chosen.label,
                "kind": chosen.kind,
                "kind_label": _kind_label(chosen.kind),
                "south": chosen.south,
                "north": chosen.north,
                "west": chosen.west,
                "east": chosen.east,
            },
            "pick": pick,
            "path_json": json.dumps(polylines),
            "markers_json": json.dumps(markers),
            "empty": not days,
            "day_count": len(days),
            "point_count": sum(hit.point_count for hit in day_hits),
        }
    )
    if not days:
        context["error"] = gettext("No days in this place for that period.")
    return render(request, "personalstats/place_search.html", context)
