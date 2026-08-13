"""
Geo cache: lat/lon → address (Nominatim) and/or elevation (Open-Meteo DEM).

- Results are cached in the local DB (empty after a fresh install → real HTTP calls).
- Nominatim usage policy: identify the app, max ~1 request/second, no bulk abuse.
  We enforce a min interval + a daily cap so a personal matesla instance is not blocked.
- Elevation is filled by matesla.geo_enrich (capture cron); address may stay empty
  until reverse-geocode is needed or the address backfill queue picks the grid.
"""

from __future__ import annotations

import time
from datetime import date

from django.conf import settings
from django.db import models, transaction
from django.utils.timezone import now
from geopy.geocoders import Nominatim


class AddressFromLatLong(models.Model):
    """Grid cache (~11 m at 4 decimals). Address and elevation are independent."""

    latitude = models.FloatField()  # IE 50.7962 (typically round 4)
    longitude = models.FloatField()  # IE 4.3354
    # Empty until Nominatim succeeds; elev-only rows are allowed.
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
    """Tracks how many Nominatim HTTP calls we made today (local rate limit)."""

    day = models.DateField(unique=True)
    call_count = models.PositiveIntegerField(default=0)
    last_call_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.day}: {self.call_count} calls"


# purpose= for slot / reverse: interactive UI must not starve behind cron backfill.
NOMINATIM_PURPOSE_INTERACTIVE = "interactive"
NOMINATIM_PURPOSE_BACKFILL = "backfill"


def _nominatim_max_per_day() -> int:
    # Hard daily cap (all callers). Override in settings if you self-host Nominatim.
    return int(getattr(settings, "NOMINATIM_MAX_PER_DAY", 300))


def _nominatim_backfill_max_per_day() -> int:
    """
    Soft cap for capture-cron address enrichment only.

    Leaves the rest of NOMINATIM_MAX_PER_DAY for day-map / drives AJAX
    (ResolveAddress) and status-page reverse-geocode. Without this, a full
    backlog burns the whole daily budget overnight and the UI never resolves.
    """
    hard = _nominatim_max_per_day()
    soft = int(getattr(settings, "NOMINATIM_BACKFILL_MAX_PER_DAY", 200))
    return max(0, min(soft, hard))


def _nominatim_min_interval_sec() -> float:
    # Official guidance is max 1 req/s
    return float(getattr(settings, "NOMINATIM_MIN_INTERVAL_SEC", 1.1))


def _nominatim_limit_for_purpose(purpose: str) -> int:
    if purpose == NOMINATIM_PURPOSE_BACKFILL:
        return _nominatim_backfill_max_per_day()
    return _nominatim_max_per_day()


def _acquire_nominatim_slot(*, purpose: str = NOMINATIM_PURPOSE_INTERACTIVE) -> bool:
    """
    Reserve one Nominatim HTTP call under daily + per-second limits.

    purpose:
      - interactive: day-map AJAX, status page — may use the full daily hard cap
      - backfill: capture cron — stops earlier so UI still has budget left

    Returns False if that purpose's budget is exhausted.
    """
    today = date.today()
    min_interval = _nominatim_min_interval_sec()
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
                # release lock before sleeping
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
        # re-check interval after sleep (another worker may have called)
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


def _format_from_components(raw_address):
    """Build a short street-style line from Nominatim structured fields."""
    if not raw_address:
        return None
    road = (
        raw_address.get("road")
        or raw_address.get("pedestrian")
        or raw_address.get("footway")
        or raw_address.get("path")
        or raw_address.get("cycleway")
        or raw_address.get("residential")
    )
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
    return any(
        raw_address.get(field_name)
        for field_name in (
            "road",
            "pedestrian",
            "footway",
            "path",
            "park",
            "leisure",
            "house_number",
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


def LookupCachedAddress(latitude, longitude):
    """
    Fast path: return a cleaned address only if already in the local DB.
    Never hits Nominatim (page render must stay instant).
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


def GetAddressFromLatLong(
    latitude,
    longitude,
    *,
    purpose: str = NOMINATIM_PURPOSE_INTERACTIVE,
):
    """
    Reverse-geocode lat/lon via Nominatim; cache successful results in local DB.

    Empty DB after project revive → real HTTP calls until the point is cached.
    Daily cap + 1 req/s so we stay within public Nominatim politeness rules.
    Prefer LookupCachedAddress() on hot page renders; call this from async API.

    purpose="interactive" (default): day-map AJAX / status — full hard daily cap.
    purpose="backfill": capture cron — softer daily cap so UI never starves.

    Preserves an existing elevation cache row when only the address was missing.
    """
    cached = LookupCachedAddress(latitude, longitude)
    if cached is not None:
        return cached

    try:
        display = None
        # Interactive: fine zoom then coarser. Backfill: one coarser call only
        # (saves scarce Nominatim slots while still good enough for park grids).
        zooms = (18, 16) if purpose != NOMINATIM_PURPOSE_BACKFILL else (16,)
        for zoom in zooms:
            location = _nominatim_reverse(
                latitude, longitude, zoom=zoom, purpose=purpose
            )
            if location is None:
                # quota exhausted or network failure
                break
            raw = getattr(location, "raw", None) or {}
            addr_bits = raw.get("address") or {}
            structured = _format_from_components(addr_bits)
            candidate = structured or CleanAddressDisplay(location.address or "")
            if not candidate or candidate == "Unknown":
                continue
            display = candidate
            if _has_street_detail(addr_bits) or zoom <= 16:
                break

        if not display:
            return "Unknown"

        row, created = AddressFromLatLong.objects.get_or_create(
            latitude=latitude,
            longitude=longitude,
            defaults={
                "address": display,
                "date": now().date(),
            },
        )
        if not created:
            # Elev-only or previous Unknown — fill address without wiping elevation.
            if not (row.address or "").strip() or CleanAddressDisplay(row.address) in (
                "",
                "Unknown",
                None,
            ):
                row.address = display
                row.date = now().date()
                row.save(update_fields=["address", "date"])
        return display
    except Exception:
        return "Unknown"
