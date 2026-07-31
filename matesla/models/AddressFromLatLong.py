"""
Reverse geocoding (lat/lon → address) via OpenStreetMap Nominatim.

- Results are cached in the local DB (empty after a fresh install → real HTTP calls).
- Nominatim usage policy: identify the app, max ~1 request/second, no bulk abuse.
  We enforce a min interval + a daily cap so a personal matesla instance is not blocked.
"""

from __future__ import annotations

import time
from datetime import date

from django.conf import settings
from django.db import models, transaction
from django.utils.timezone import now
from geopy.geocoders import Nominatim


class AddressFromLatLong(models.Model):
    latitude = models.FloatField()  # IE 50.79621
    longitude = models.FloatField()  # IE 4.335445
    address = models.TextField()
    date = models.DateField()

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


def _nominatim_max_per_day() -> int:
    # Personal app default: generous enough for a few day-map views, low enough
    # to stay polite to public Nominatim (override in settings if you self-host).
    return int(getattr(settings, "NOMINATIM_MAX_PER_DAY", 250))


def _nominatim_min_interval_sec() -> float:
    # Official guidance is max 1 req/s
    return float(getattr(settings, "NOMINATIM_MIN_INTERVAL_SEC", 1.1))


def _acquire_nominatim_slot() -> bool:
    """
    Reserve one Nominatim HTTP call under daily + per-second limits.
    Returns False if the daily budget is exhausted.
    """
    today = date.today()
    min_interval = _nominatim_min_interval_sec()
    max_day = _nominatim_max_per_day()

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
        # bump optimistically after wait outside lock if needed
        call_count = row.call_count
        last_at = row.last_call_at

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


def _nominatim_reverse(latitude, longitude, zoom=18):
    if not _acquire_nominatim_slot():
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
    cleaned = CleanAddressDisplay(row.address)
    if not cleaned or cleaned == "Unknown":
        return None
    return cleaned


def GetAddressFromLatLong(latitude, longitude):
    """
    Reverse-geocode lat/lon via Nominatim; cache successful results in local DB.

    Empty DB after project revive → real HTTP calls until the point is cached.
    Daily cap + 1 req/s so we stay within public Nominatim politeness rules.
    Prefer LookupCachedAddress() on hot page renders; call this from async API.
    """
    cached = LookupCachedAddress(latitude, longitude)
    if cached is not None:
        return cached

    try:
        display = None
        # At most 2 HTTP calls: fine zoom (parks/side streets), then coarser
        for zoom in (18, 16):
            location = _nominatim_reverse(latitude, longitude, zoom=zoom)
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

        add = AddressFromLatLong()
        add.latitude = latitude
        add.longitude = longitude
        add.address = display
        add.date = now().date()
        add.save()
        return display
    except Exception:
        return "Unknown"
