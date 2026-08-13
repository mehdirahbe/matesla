"""
Geo cache: lat/lon → address and/or elevation (Open-Meteo DEM).

Reverse-geocode provider:
  - Geoapify if GEOAPIFY_API_KEY (or GEOAPIFY_KEY) is set — ~5 req/s free tier
  - else public Nominatim via geopy — polite ~1 req/s

- Results are cached in the local DB (empty after a fresh install → real HTTP calls).
- We enforce a min interval + a daily cap (shared counter table NominatimDailyQuota).
- Elevation is filled by matesla.geo_enrich (capture cron); address may stay empty
  until reverse-geocode is needed or the address backfill queue picks the grid.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import requests
from django.conf import settings
from django.db import models, transaction
from django.utils.timezone import now
from geopy.geocoders import Nominatim

logger = logging.getLogger(__name__)


class AddressFromLatLong(models.Model):
    """Grid cache (~11 m at 4 decimals). Address and elevation are independent."""

    latitude = models.FloatField()  # IE 50.7962 (typically round 4)
    longitude = models.FloatField()  # IE 4.3354
    # Empty until reverse-geocode succeeds; elev-only rows are allowed.
    address = models.TextField(blank=True, default="")
    date = models.DateField()
    # Metres above sea level (Open-Meteo DEM or legacy TeslaFi). Null = unknown.
    elevation = models.FloatField(null=True, blank=True)
    elevation_fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["latitude", "longitude"],
                name="AddressFromLatLong: unique address for same latitude and longiture",
            )
        ]


class NominatimDailyQuota(models.Model):
    """
    Tracks how many reverse-geocode HTTP calls we made today (local rate limit).

    Name is historical (started with Nominatim only); used for Geoapify too.
    """

    day = models.DateField(unique=True)
    call_count = models.PositiveIntegerField(default=0)
    last_call_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.day}: {self.call_count} calls"


# purpose= for slot / reverse: interactive UI must not starve behind cron backfill.
NOMINATIM_PURPOSE_INTERACTIVE = "interactive"
NOMINATIM_PURPOSE_BACKFILL = "backfill"

# Tesla GPS → prefer driveable roads over park footways / cycle paths.
_CAR_ROAD_KEYS = (
    "road",
    "residential",
    "service",
    "industrial",
    "unclassified",
)
_PEDESTRIAN_KEYS = (
    "pedestrian",
    "footway",
    "path",
    "cycleway",
)


def _geoapify_api_key() -> str | None:
    """
    Return Geoapify key from Django settings, or None → use Nominatim.

    settings.GEOAPIFY_API_KEY is loaded from env in mysite/settings.py
    (GEOAPIFY_API_KEY or GEOAPIFY_KEY). Tests use override_settings.
    """
    val = (
        getattr(settings, "GEOAPIFY_API_KEY", None)
        or getattr(settings, "GEOAPIFY_KEY", None)
        or ""
    )
    val = str(val).strip()
    return val or None


def active_geocoder() -> str:
    """'geoapify' if a key is configured, else 'nominatim'."""
    return "geoapify" if _geoapify_api_key() else "nominatim"


def _geocode_max_per_day() -> int:
    """
    Hard daily cap (all callers).

    Nominatim: self-imposed politeness (public instance has no hard published
    daily limit, but bulk abuse gets blocked). Default raised from 300 → 1000.
    Geoapify free tier: 3000/day — default 2500 with headroom.
    """
    if active_geocoder() == "geoapify":
        return int(getattr(settings, "GEOAPIFY_MAX_PER_DAY", 2500))
    return int(getattr(settings, "NOMINATIM_MAX_PER_DAY", 1000))


def _geocode_backfill_max_per_day() -> int:
    """
    Soft cap for capture-cron address enrichment only.

    Leaves the rest of the hard daily cap for day-map / drives AJAX
    (ResolveAddress) and status-page reverse-geocode.
    """
    hard = _geocode_max_per_day()
    if active_geocoder() == "geoapify":
        soft = int(getattr(settings, "GEOAPIFY_BACKFILL_MAX_PER_DAY", 2000))
    else:
        soft = int(getattr(settings, "NOMINATIM_BACKFILL_MAX_PER_DAY", 800))
    return max(0, min(soft, hard))


def _geocode_min_interval_sec() -> float:
    if active_geocoder() == "geoapify":
        # Free tier ~5 req/s
        return float(getattr(settings, "GEOAPIFY_MIN_INTERVAL_SEC", 0.25))
    # Public Nominatim: max ~1 req/s
    return float(getattr(settings, "NOMINATIM_MIN_INTERVAL_SEC", 1.1))


# Back-compat aliases (tests / older imports)
def _nominatim_max_per_day() -> int:
    return _geocode_max_per_day()


def _nominatim_backfill_max_per_day() -> int:
    return _geocode_backfill_max_per_day()


def _nominatim_min_interval_sec() -> float:
    return _geocode_min_interval_sec()


def _nominatim_limit_for_purpose(purpose: str) -> int:
    if purpose == NOMINATIM_PURPOSE_BACKFILL:
        return _geocode_backfill_max_per_day()
    return _geocode_max_per_day()


def _acquire_nominatim_slot(*, purpose: str = NOMINATIM_PURPOSE_INTERACTIVE) -> bool:
    """
    Reserve one reverse-geocode HTTP call under daily + per-second limits.

    purpose:
      - interactive: day-map AJAX, status page — may use the full daily hard cap
      - backfill: capture cron — stops earlier so UI still has budget left

    Returns False if that purpose's budget is exhausted.
    """
    today = date.today()
    min_interval = _geocode_min_interval_sec()
    max_day = _nominatim_limit_for_purpose(purpose)

    with transaction.atomic():
        row, _ = NominatimDailyQuota.objects.select_for_update().get_or_create(
            day=today, defaults={"call_count": 0}
        )
        if row.call_count >= max_day:
            return False
        if row.last_call_at is not None:
            elapsed = (now() - row.last_call_at).total_seconds()
            if elapsed < min_interval:
                wait = min_interval - elapsed
            else:
                wait = 0
        else:
            wait = 0

    if wait > 0:
        time.sleep(wait)

    with transaction.atomic():
        row, _ = NominatimDailyQuota.objects.select_for_update().get_or_create(
            day=today, defaults={"call_count": 0}
        )
        if row.call_count >= max_day:
            return False
        if row.last_call_at is not None:
            elapsed = (now() - row.last_call_at).total_seconds()
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
                row.refresh_from_db()
        row.call_count += 1
        row.last_call_at = now()
        row.save(update_fields=["call_count", "last_call_at"])
    return True


def _pick_one_language(part, prefer_fr=True):
    """
    From "Uccle - Ukkel" or "België / Belgique / Belgien", keep one label.
    Prefer French when we can detect it (UI is primarily FR for BE users).
    """
    part_text = part.strip()
    if not part_text:
        return part_text
    if " - " in part_text:
        options = [option.strip() for option in part_text.split(" - ") if option.strip()]
    elif " / " in part_text:
        options = [option.strip() for option in part_text.split(" / ") if option.strip()]
    else:
        return part_text
    if len(options) == 1:
        return options[0]
    if prefer_fr:
        preferred = {
            "Belgique",
            "France",
            "Bruxelles",
            "Bruxelles-Capitale",
            "Région de Bruxelles-Capitale",
            "Flandre",
            "Wallonie",
        }
        for option in options:
            if option in preferred:
                return option
        french_markers = (
            "é",
            "è",
            "ê",
            "à",
            "ù",
            "ô",
            "î",
            "ç",
            "Chaussée",
            "chaussée",
            "Rue",
            "Avenue",
            "Région",
            "Bruxelles",
        )

        def french_score(label):
            return sum(1 for marker in french_markers if marker in label)

        ranked = sorted(options, key=french_score, reverse=True)
        if french_score(ranked[0]) > french_score(ranked[-1]):
            return ranked[0]
    return options[0]


def CleanAddressDisplay(address):
    """
    Nominatim (esp. in BE) often returns bilingual labels like:
      "Uccle - Ukkel", "België / Belgique / Belgien"
    Keep a single form so the UI is readable (prefer French when possible).
    """
    if not address or address == "Unknown":
        return address
    parts = []
    for part in address.split(","):
        cleaned_part = _pick_one_language(part, prefer_fr=True)
        if cleaned_part:
            parts.append(cleaned_part)
    return ", ".join(parts)


def _prefer_language_code():
    """Map Django language to Nominatim accept-language (prefer French for BE)."""
    try:
        from django.utils.translation import get_language

        lang = (get_language() or "fr")[:2].lower()
    except Exception:
        lang = "fr"
    if lang == "fr":
        return "fr,en"
    if lang == "nl":
        return "nl,fr,en"
    if lang == "de":
        return "de,fr,en"
    if lang == "es":
        return "es,fr,en"
    if lang in ("nb", "nn", "no"):
        return "nb,no,en"
    return f"{lang},fr,en"


def _prefer_lang_short() -> str:
    """Single language code for Geoapify lang=."""
    try:
        from django.utils.translation import get_language

        return (get_language() or "fr")[:2].lower()
    except Exception:
        return "fr"


def _format_from_components(raw_address, *, car_roads_only: bool = True):
    """
    Build a short street-style line from structured address fields.

    car_roads_only=True (default): do not fall back to pedestrian/footway/path
    (park trails next to a street — wrong for Tesla GPS labels).
    """
    if not raw_address:
        return None
    road = None
    for key in _CAR_ROAD_KEYS:
        if raw_address.get(key):
            road = raw_address[key]
            break
    if not road and not car_roads_only:
        for key in _PEDESTRIAN_KEYS:
            if raw_address.get(key):
                road = raw_address[key]
                break

    place = (
        raw_address.get("park")
        or raw_address.get("leisure")
        or raw_address.get("attraction")
        or raw_address.get("amenity")
        or raw_address.get("building")
        or raw_address.get("tourism")
    )
    house = raw_address.get("house_number")
    if road and house:
        street = f"{house}, {road}"
    elif road:
        street = road
    elif place:
        street = place
    elif house:
        street = house
    else:
        street = None

    locality = (
        raw_address.get("city")
        or raw_address.get("town")
        or raw_address.get("village")
        or raw_address.get("municipality")
        or raw_address.get("city_district")
        or raw_address.get("suburb")
    )
    suburb = raw_address.get("suburb") or raw_address.get("neighbourhood")
    postcode = raw_address.get("postcode")
    country = raw_address.get("country")

    bits = []
    if street:
        bits.append(street)
    if place and place != street:
        bits.append(place)
    if suburb and suburb != locality and suburb not in (street or ""):
        bits.append(suburb)
    if locality:
        bits.append(locality)
    if postcode:
        bits.append(postcode)
    if country:
        bits.append(country)
    if not bits:
        return None
    return CleanAddressDisplay(", ".join(bits))


def _has_street_detail(raw_address: dict) -> bool:
    """True if we got a car-relevant street/place detail (not only a locality)."""
    return any(
        raw_address.get(field_name)
        for field_name in (
            "road",
            "residential",
            "service",
            "house_number",
            "park",
            "leisure",
            "amenity",
        )
    )


def _nominatim_reverse(
    latitude,
    longitude,
    zoom=18,
    *,
    purpose: str = NOMINATIM_PURPOSE_INTERACTIVE,
):
    if not _acquire_nominatim_slot(purpose=purpose):
        return None
    geolocator = Nominatim(user_agent="matesla-personal-tesla-stats/1.0")
    return geolocator.reverse(
        f"{latitude},{longitude}",
        language=_prefer_language_code(),
        addressdetails=True,
        exactly_one=True,
        timeout=12,
        zoom=zoom,
    )


def _format_geoapify_props(props: dict) -> str | None:
    """Turn Geoapify feature properties into our short display line."""
    if not props:
        return None
    # Structured path first (control street vs footway-ish names)
    addr_bits = {
        "house_number": props.get("housenumber") or props.get("house_number"),
        "road": props.get("street") or props.get("road"),
        "city": props.get("city") or props.get("town") or props.get("village"),
        "town": props.get("town"),
        "village": props.get("village"),
        "municipality": props.get("municipality") or props.get("county"),
        "suburb": props.get("suburb") or props.get("district"),
        "postcode": props.get("postcode"),
        "country": props.get("country"),
        "amenity": props.get("name")
        if props.get("result_type") in ("amenity", "building")
        or props.get("category")
        else None,
        "leisure": props.get("name")
        if (props.get("category") or "").startswith("leisure")
        else None,
    }
    # If name is a POI and we have a street, include POI as place
    name = props.get("name")
    street = props.get("street")
    if name and street and name != street:
        if not addr_bits.get("amenity") and not addr_bits.get("leisure"):
            # Useful nearby POI (e.g. boulodrome at camping parking)
            addr_bits["amenity"] = name

    structured = _format_from_components(addr_bits, car_roads_only=True)
    if structured:
        return structured

    # Fallback: Geoapify formatted line
    formatted = props.get("formatted") or props.get("address_line1")
    if formatted:
        return CleanAddressDisplay(formatted)
    return None


def _geoapify_reverse(
    latitude,
    longitude,
    *,
    purpose: str = NOMINATIM_PURPOSE_INTERACTIVE,
) -> str | None:
    """
    One Geoapify reverse call. Returns display string, or None if quota/network/empty.
    """
    api_key = _geoapify_api_key()
    if not api_key:
        return None
    if not _acquire_nominatim_slot(purpose=purpose):
        return None
    try:
        r = requests.get(
            "https://api.geoapify.com/v1/geocode/reverse",
            params={
                "lat": latitude,
                "lon": longitude,
                "apiKey": api_key,
                "lang": _prefer_lang_short(),
                "limit": 1,
            },
            headers={"User-Agent": "matesla-personal-tesla-stats/1.0"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        feats = data.get("features") or []
        if not feats:
            return None
        props = feats[0].get("properties") or {}
        return _format_geoapify_props(props)
    except Exception as exc:
        logger.warning("Geoapify reverse failed for %s,%s: %s", latitude, longitude, exc)
        return None


def LookupCachedAddress(latitude, longitude):
    """
    Fast path: return a cleaned address only if already in the local DB.
    Never hits the network (page render must stay instant).
    """
    row = AddressFromLatLong.objects.filter(
        latitude=latitude, longitude=longitude
    ).first()
    if not row:
        return None
    cleaned = CleanAddressDisplay(row.address or "")
    if not cleaned or cleaned == "Unknown":
        return None
    return cleaned


def _store_address(latitude, longitude, display: str) -> str:
    row, created = AddressFromLatLong.objects.get_or_create(
        latitude=latitude,
        longitude=longitude,
        defaults={
            "address": display,
            "date": now().date(),
        },
    )
    if not created:
        if not (row.address or "").strip() or CleanAddressDisplay(row.address) in (
            "",
            "Unknown",
            None,
        ):
            row.address = display
            row.date = now().date()
            row.save(update_fields=["address", "date"])
    return display


def GetAddressFromLatLong(
    latitude,
    longitude,
    *,
    purpose: str = NOMINATIM_PURPOSE_INTERACTIVE,
):
    """
    Reverse-geocode lat/lon; cache successful results in local DB.

    Provider: Geoapify if GEOAPIFY_API_KEY is set, else Nominatim.
    Daily cap + min interval depend on the active provider.
    Prefer LookupCachedAddress() on hot page renders; call this from async API.

    purpose="interactive" (default): day-map AJAX / status — full hard daily cap.
    purpose="backfill": capture cron — softer daily cap so UI never starves.

    Preserves an existing elevation cache row when only the address was missing.
    """
    cached = LookupCachedAddress(latitude, longitude)
    if cached is not None:
        return cached

    try:
        if active_geocoder() == "geoapify":
            # Single reverse call (Geoapify free tier is roomy enough; no dual zoom).
            display = _geoapify_reverse(latitude, longitude, purpose=purpose)
            if not display or display == "Unknown":
                return "Unknown"
            return _store_address(latitude, longitude, display)

        # Nominatim path: interactive may try fine then coarser zoom.
        display = None
        zooms = (18, 16) if purpose != NOMINATIM_PURPOSE_BACKFILL else (16,)
        for zoom in zooms:
            location = _nominatim_reverse(
                latitude, longitude, zoom=zoom, purpose=purpose
            )
            if location is None:
                break
            raw = getattr(location, "raw", None) or {}
            addr_bits = raw.get("address") or {}
            structured = _format_from_components(addr_bits, car_roads_only=True)
            candidate = structured or CleanAddressDisplay(location.address or "")
            if not candidate or candidate == "Unknown":
                continue
            display = candidate
            if _has_street_detail(addr_bits) or zoom <= 16:
                break

        if not display:
            return "Unknown"
        return _store_address(latitude, longitude, display)
    except Exception:
        return "Unknown"
