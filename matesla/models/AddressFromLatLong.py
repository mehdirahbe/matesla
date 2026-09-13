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
import re
import time
from dataclasses import dataclass, replace
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


class ForwardGeocodeCache(models.Model):
    """Forward lookup cache (query text → ranked candidates with bbox)."""

    query_key = models.CharField(max_length=240, unique=True)
    payload = models.JSONField()
    fetched_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["fetched_at"])]


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


# ---------------------------------------------------------------------------
# Forward geocode (place / region text → bbox). Same providers + quota as reverse.
# ---------------------------------------------------------------------------

FORWARD_CACHE_DAYS = 180
FORWARD_MAX_HITS = 8
FORWARD_SHOW_HITS = 3
# Bump when ranking / amenity-supplement changes so stale city-only rows are ignored.
FORWARD_CACHE_VERSION = "v2"

# City vs region bbox policy (degrees). City must stay tight enough that
# "Namur" does not become the whole province; region keeps the geocoder outline.
CITY_MAX_LAT_SPAN = 0.45
CITY_MAX_LON_SPAN = 0.65
CITY_HALF_LAT = 0.14
CITY_HALF_LON = 0.20
REGION_PAD = 0.06
CITY_PAD = 0.03
OTHER_PAD = 0.06
POINT_HALF_LAT = 0.08
POINT_HALF_LON = 0.12

_REGION_TYPES = frozenset(
    {"state", "country", "region", "nation", "territory", "state_district"}
)
_CITY_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "municipality",
        "hamlet",
        "suburb",
        "locality",
        "city_district",
        "district",
        "neighbourhood",
        "postcode",
    }
)
_COUNTY_TYPES = frozenset({"county", "province", "department", "arrondissement"})
_AREA_TYPES = frozenset(
    {
        "river",
        "waterway",
        "lake",
        "reservoir",
        "canyon",
        "gorge",
        "stream",
        "canal",
        "park",
        "protected_area",
        "nature_reserve",
        "wood",
        "forest",
    }
)
# Promote amenity/natural hits whose outline is clearly bigger than a village.
AREA_MIN_SPAN = 0.35


@dataclass(frozen=True)
class GeocodeHit:
    label: str
    name: str
    kind: str  # region | city | county | other
    lat: float
    lon: float
    south: float
    north: float
    west: float
    east: float
    provider: str
    importance: float = 0.0


@dataclass(frozen=True)
class ForwardGeocodeResult:
    hits: list[GeocodeHit]
    error: str | None = None  # None | "quota" | "network" | "empty"
    cached: bool = False


def normalize_forward_query(query: str) -> str:
    text = (query or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _forward_cache_key(query: str) -> str:
    lang = _prefer_lang_short()
    return f"{active_geocoder()}|{FORWARD_CACHE_VERSION}|{lang}|{query.casefold()}"


def _hit_to_dict(hit: GeocodeHit) -> dict:
    return {
        "label": hit.label,
        "name": hit.name,
        "kind": hit.kind,
        "lat": hit.lat,
        "lon": hit.lon,
        "south": hit.south,
        "north": hit.north,
        "west": hit.west,
        "east": hit.east,
        "provider": hit.provider,
        "importance": hit.importance,
    }


def _hit_from_dict(data: dict) -> GeocodeHit | None:
    try:
        return GeocodeHit(
            label=str(data["label"]),
            name=str(data.get("name") or data["label"]),
            kind=str(data.get("kind") or "other"),
            lat=float(data["lat"]),
            lon=float(data["lon"]),
            south=float(data["south"]),
            north=float(data["north"]),
            west=float(data["west"]),
            east=float(data["east"]),
            provider=str(data.get("provider") or ""),
            importance=float(data.get("importance") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _bbox_span(south, north, west, east) -> float:
    try:
        return max(float(north) - float(south), float(east) - float(west))
    except (TypeError, ValueError):
        return 0.0


def _classify_kind(
    type_token: str,
    *,
    osm_class: str = "",
    category: str = "",
    south=None,
    north=None,
    west=None,
    east=None,
) -> str:
    token = (type_token or "").strip().lower()
    osm_class = (osm_class or "").strip().lower()
    category = (category or "").strip().lower()
    if token in _REGION_TYPES:
        return "region"
    if token in _CITY_TYPES:
        return "city"
    if token in _COUNTY_TYPES:
        return "county"
    span = (
        _bbox_span(south, north, west, east)
        if south is not None and north is not None and west is not None and east is not None
        else 0.0
    )
    is_natural = (
        token in _AREA_TYPES
        or osm_class in ("waterway", "natural", "leisure")
        or any(tag in category for tag in ("natural", "water", "park"))
    )
    # Geoapify often labels a river as result_type=amenity with a wide bbox
    # ("Le Verdon") while tiny homonymous villages are result_type=city.
    if span >= AREA_MIN_SPAN and (is_natural or token in ("amenity", "other", "")):
        return "region"
    if is_natural:
        return "region"
    return "other"


def _clamp_bbox(south, north, west, east):
    south = max(-90.0, min(90.0, float(south)))
    north = max(-90.0, min(90.0, float(north)))
    west = max(-180.0, min(180.0, float(west)))
    east = max(-180.0, min(180.0, float(east)))
    if south > north:
        south, north = north, south
    if west > east:
        west, east = east, west
    return south, north, west, east


def _bbox_from_center(lat: float, lon: float, kind: str):
    if kind == "region":
        dlat, dlon = 1.5, 2.0
    elif kind == "city":
        dlat, dlon = POINT_HALF_LAT, POINT_HALF_LON
    else:
        dlat, dlon = 0.05, 0.08
    return _clamp_bbox(lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def apply_bbox_policy(hit: GeocodeHit) -> GeocodeHit:
    """
    City: tight bbox (never the whole province). Region: geocoder outline + pad.

    A city result whose bbox is huge (province returned as city) is rebuilt
    around the centre. Regions are padded a little so GPS on the border still
    matches (Brittany north coast).
    """
    south, north, west, east = hit.south, hit.north, hit.west, hit.east
    lat_span = north - south
    lon_span = east - west
    pad = 0.0
    if hit.kind == "city" and (
        lat_span > CITY_MAX_LAT_SPAN or lon_span > CITY_MAX_LON_SPAN
    ):
        south = hit.lat - CITY_HALF_LAT
        north = hit.lat + CITY_HALF_LAT
        west = hit.lon - CITY_HALF_LON
        east = hit.lon + CITY_HALF_LON
    elif hit.kind == "region":
        pad = REGION_PAD
    elif hit.kind == "city":
        pad = CITY_PAD
    else:
        pad = OTHER_PAD
    if pad:
        south -= pad
        north += pad
        west -= pad
        east += pad
    south, north, west, east = _clamp_bbox(south, north, west, east)
    return replace(hit, south=south, north=north, west=west, east=east)


def _query_tokens(text: str) -> list[str]:
    return re.findall(r"[a-zà-ÿ0-9]+", (text or "").casefold())


def rank_forward_hits(query: str, hits: list[GeocodeHit]) -> list[GeocodeHit]:
    """
    Named regions first (Bretagne, or a river/gorge like Le Verdon), then a
    matching city (Namur). Tiny homonymous villages must not beat the feature
    the query actually names.
    """
    if not hits:
        return []
    q_tokens = [tok for tok in _query_tokens(query) if len(tok) >= 3] or _query_tokens(
        query
    )

    def name_matches(hit: GeocodeHit) -> bool:
        hay = _query_tokens(hit.name) + _query_tokens(hit.label)
        if not q_tokens or not hay:
            return False
        return all(tok in hay for tok in q_tokens)

    def bbox_area(hit: GeocodeHit) -> float:
        return max(0.0, hit.north - hit.south) * max(0.0, hit.east - hit.west)

    matching_regions = [
        hit for hit in hits if hit.kind == "region" and name_matches(hit)
    ]
    matching_cities = [
        hit for hit in hits if hit.kind == "city" and name_matches(hit)
    ]
    if matching_regions:
        primary = matching_regions
    elif matching_cities:
        primary = matching_cities
    else:
        primary = []

    def importance_of(hit: GeocodeHit) -> float:
        return (hit.importance, bbox_area(hit))

    seen = set()
    ordered: list[GeocodeHit] = []
    for group in (primary, hits):
        for hit in sorted(group, key=importance_of, reverse=True):
            key = (
                round(hit.lat, 3),
                round(hit.lon, 3),
                round(hit.south, 3),
                round(hit.north, 3),
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(hit)
    return ordered[:FORWARD_SHOW_HITS]


def _bbox_from_geoapify_feature(feat: dict, lat: float, lon: float, kind: str):
    bbox = feat.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        west, south, east, north = (float(v) for v in bbox)
        return _clamp_bbox(south, north, west, east)
    props = feat.get("properties") or {}
    pb = props.get("bbox")
    if isinstance(pb, dict) and {"lat1", "lat2", "lon1", "lon2"} <= set(pb):
        lats = [float(pb["lat1"]), float(pb["lat2"])]
        lons = [float(pb["lon1"]), float(pb["lon2"])]
        return _clamp_bbox(min(lats), max(lats), min(lons), max(lons))
    return _bbox_from_center(lat, lon, kind)


def _importance_from_rank(rank: dict) -> float:
    if not isinstance(rank, dict):
        return 0.0
    for key in ("importance", "popularity", "confidence"):
        raw = rank.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _hits_from_geoapify_features(query: str, features) -> list[GeocodeHit]:
    hits: list[GeocodeHit] = []
    for feat in features or []:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        try:
            lon = float(props.get("lon") if props.get("lon") is not None else coords[0])
            lat = float(props.get("lat") if props.get("lat") is not None else coords[1])
        except (TypeError, ValueError, IndexError):
            continue
        result_type = str(props.get("result_type") or props.get("type") or "")
        south, north, west, east = _bbox_from_geoapify_feature(
            feat, lat, lon, result_type
        )
        kind = _classify_kind(
            result_type,
            category=str(props.get("category") or ""),
            south=south,
            north=north,
            west=west,
            east=east,
        )
        name = str(props.get("name") or props.get("city") or props.get("state") or query)
        label = CleanAddressDisplay(
            props.get("formatted") or props.get("address_line1") or name
        )
        hits.append(
            apply_bbox_policy(
                GeocodeHit(
                    label=label or name,
                    name=name,
                    kind=kind,
                    lat=lat,
                    lon=lon,
                    south=south,
                    north=north,
                    west=west,
                    east=east,
                    provider="geoapify",
                    importance=_importance_from_rank(props.get("rank") or {}),
                )
            )
        )
    return hits


def _geoapify_search_json(
    query: str, extra_params: dict | None = None
) -> tuple[dict | None, str | None]:
    api_key = _geoapify_api_key()
    if not api_key:
        return None, "network"
    if not _acquire_nominatim_slot(purpose=NOMINATIM_PURPOSE_INTERACTIVE):
        return None, "quota"
    params = {
        "text": query,
        "apiKey": api_key,
        "lang": _prefer_lang_short(),
        "limit": FORWARD_MAX_HITS,
    }
    if extra_params:
        params.update(extra_params)
    try:
        response = requests.get(
            "https://api.geoapify.com/v1/geocode/search",
            params=params,
            headers={"User-Agent": "matesla-personal-tesla-stats/1.0"},
            timeout=12,
        )
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        logger.warning("Geoapify forward failed for %r: %s", query, exc)
        return None, "network"


def _geoapify_forward(query: str) -> ForwardGeocodeResult:
    data, error = _geoapify_search_json(query)
    if data is None:
        return ForwardGeocodeResult(hits=[], error=error or "network")

    hits = _hits_from_geoapify_features(query, data.get("features") or [])
    # Default search is administrative-only: "verdon" → tiny communes, never
    # the river / gorges. One amenity lookup fills that gap (cached after).
    if not any(hit.kind == "region" for hit in hits):
        extra, extra_error = _geoapify_search_json(
            query, extra_params={"type": "amenity"}
        )
        if extra is not None:
            hits.extend(
                _hits_from_geoapify_features(query, extra.get("features") or [])
            )
        elif extra_error == "quota" and not hits:
            return ForwardGeocodeResult(hits=[], error="quota")
    ranked = rank_forward_hits(query, hits)
    if not ranked:
        return ForwardGeocodeResult(hits=[], error="empty")
    return ForwardGeocodeResult(hits=ranked)


def _nominatim_forward(query: str) -> ForwardGeocodeResult:
    if not _acquire_nominatim_slot(purpose=NOMINATIM_PURPOSE_INTERACTIVE):
        return ForwardGeocodeResult(hits=[], error="quota")
    try:
        geolocator = Nominatim(user_agent="matesla-personal-tesla-stats/1.0")
        locations = geolocator.geocode(
            query,
            language=_prefer_language_code(),
            addressdetails=True,
            exactly_one=False,
            limit=FORWARD_MAX_HITS,
            timeout=12,
        )
    except Exception as exc:
        logger.warning("Nominatim forward failed for %r: %s", query, exc)
        return ForwardGeocodeResult(hits=[], error="network")
    if not locations:
        return ForwardGeocodeResult(hits=[], error="empty")

    hits: list[GeocodeHit] = []
    for location in locations:
        raw = getattr(location, "raw", None) or {}
        try:
            lat = float(location.latitude)
            lon = float(location.longitude)
        except (TypeError, ValueError):
            continue
        type_token = str(
            raw.get("addresstype") or raw.get("type") or raw.get("class") or ""
        )
        addr = raw.get("address") or {}
        name = str(
            raw.get("name")
            or addr.get("river")
            or addr.get("state")
            or addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
            or query
        )
        label = CleanAddressDisplay(location.address or name)
        bbox_raw = raw.get("boundingbox")
        if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4:
            try:
                south, north, west, east = (float(v) for v in bbox_raw)
                south, north, west, east = _clamp_bbox(south, north, west, east)
            except (TypeError, ValueError):
                south, north, west, east = _bbox_from_center(lat, lon, "other")
        else:
            south, north, west, east = _bbox_from_center(lat, lon, "other")
        kind = _classify_kind(
            type_token,
            osm_class=str(raw.get("class") or ""),
            south=south,
            north=north,
            west=west,
            east=east,
        )
        try:
            importance = float(raw.get("importance") or 0)
        except (TypeError, ValueError):
            importance = 0.0
        hits.append(
            apply_bbox_policy(
                GeocodeHit(
                    label=label or name,
                    name=name,
                    kind=kind,
                    lat=lat,
                    lon=lon,
                    south=south,
                    north=north,
                    west=west,
                    east=east,
                    provider="nominatim",
                    importance=importance,
                )
            )
        )
    ranked = rank_forward_hits(query, hits)
    if not ranked:
        return ForwardGeocodeResult(hits=[], error="empty")
    return ForwardGeocodeResult(hits=ranked)


def _store_forward_cache(query_key: str, result: ForwardGeocodeResult) -> None:
    if result.error or not result.hits:
        return
    payload = {
        "error": result.error,
        "hits": [_hit_to_dict(hit) for hit in result.hits],
    }
    ForwardGeocodeCache.objects.update_or_create(
        query_key=query_key,
        defaults={"payload": payload, "fetched_at": now()},
    )


def _load_forward_cache(query_key: str) -> ForwardGeocodeResult | None:
    row = ForwardGeocodeCache.objects.filter(query_key=query_key).first()
    if row is None:
        return None
    fetched = row.fetched_at
    if fetched is not None:
        age_days = (now() - fetched).days
        if age_days > FORWARD_CACHE_DAYS:
            return None
    payload = row.payload if isinstance(row.payload, dict) else {}
    hits = []
    for item in payload.get("hits") or []:
        hit = _hit_from_dict(item)
        if hit is not None:
            hits.append(hit)
    error = payload.get("error")
    if hits:
        error = None
    elif error not in ("empty", "quota", "network"):
        error = "empty"
    return ForwardGeocodeResult(hits=hits, error=error, cached=True)


def ForwardGeocode(query: str) -> ForwardGeocodeResult:
    """
    Forward-geocode a free-text place / region. Uses Geoapify if a key is set,
    else Nominatim. Shares the reverse-geocode daily cap + min interval.

    Results are cached locally so a bookmarked search does not spend quota.
    """
    text = normalize_forward_query(query)
    if not text:
        return ForwardGeocodeResult(hits=[], error="empty")
    cache_key = _forward_cache_key(text)
    cached = _load_forward_cache(cache_key)
    if cached is not None:
        return cached
    if active_geocoder() == "geoapify":
        result = _geoapify_forward(text)
    else:
        result = _nominatim_forward(text)
    _store_forward_cache(cache_key, result)
    return result
