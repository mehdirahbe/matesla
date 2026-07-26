from django.db import models
from django.utils.timezone import now
from geopy.geocoders import Nominatim


# Get address from latitude+longitude, avoid a nearly 1 sec lookup to geopy each time


class AddressFromLatLong(models.Model):
    latitude = models.FloatField()  # IE 50.79621
    longitude = models.FloatField()  # IE 4.335445
    address = models.TextField()
    date = models.DateField()

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        indexes = [
            # to retrieve easily address
            models.Index(fields=["latitude", "longitude"]),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(
                fields=["latitude", "longitude"],
                name="AddressFromLatLong: unique address for same latitude and longiture",
            )
        ]


def _pick_one_language(part, prefer_fr=True):
    """
    From "Uccle - Ukkel" or "België / Belgique / Belgien", keep one label.
    Prefer French when we can detect it (UI is primarily FR for BE users).
    """
    p = part.strip()
    if not p:
        return p
    if " - " in p:
        options = [x.strip() for x in p.split(" - ") if x.strip()]
    elif " / " in p:
        options = [x.strip() for x in p.split(" / ") if x.strip()]
    else:
        return p
    if len(options) == 1:
        return options[0]
    if prefer_fr:
        # Exact known FR country/region tokens first
        preferred = {
            "Belgique",
            "France",
            "Bruxelles",
            "Bruxelles-Capitale",
            "Région de Bruxelles-Capitale",
            "Flandre",
            "Wallonie",
        }
        for o in options:
            if o in preferred:
                return o
        # Heuristic: French-looking accents / words
        fr_markers = (
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

        def fr_score(s):
            return sum(1 for m in fr_markers if m in s)

        ranked = sorted(options, key=fr_score, reverse=True)
        if fr_score(ranked[0]) > fr_score(ranked[-1]):
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
        p = _pick_one_language(part, prefer_fr=True)
        if p:
            parts.append(p)
    return ", ".join(parts)


def _prefer_language_code():
    """Map Django language to Nominatim accept-language (prefer French for BE)."""
    try:
        from django.utils.translation import get_language

        lang = (get_language() or "fr")[:2].lower()
    except Exception:
        lang = "fr"
    # Nominatim: comma-separated preference list
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
    """
    Build a short street-style line from Nominatim structured fields.
    raw_address is location.raw.get('address') dict.
    """
    if not raw_address:
        return None
    # House + road
    road = (
        raw_address.get("road")
        or raw_address.get("pedestrian")
        or raw_address.get("footway")
        or raw_address.get("path")
        or raw_address.get("cycleway")
    )
    house = raw_address.get("house_number")
    street = None
    if road and house:
        street = f"{house}, {road}"
    elif road:
        street = road
    elif house:
        street = house

    locality = (
        raw_address.get("city")
        or raw_address.get("town")
        or raw_address.get("village")
        or raw_address.get("municipality")
        or raw_address.get("city_district")
        or raw_address.get("suburb")
    )
    # neighbourhood / suburb as extra if different
    suburb = raw_address.get("suburb") or raw_address.get("neighbourhood")
    postcode = raw_address.get("postcode")
    country = raw_address.get("country")

    bits = []
    if street:
        bits.append(street)
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


def GetAddressFromLatLong(latitude, longitude):
    results = AddressFromLatLong.objects.filter(latitude=latitude).filter(
        longitude=longitude
    )
    if len(results) == 1:
        # Always clean on read so old bilingual cache rows look decent
        return CleanAddressDisplay(results[0].address)
    try:
        # not yet known-->add it
        geolocator = Nominatim(user_agent="matesla-daymap")
        location = geolocator.reverse(
            f"{latitude},{longitude}",
            language=_prefer_language_code(),
            addressdetails=True,
            exactly_one=True,
            timeout=10,
        )
        if location is None:
            return "Unknown"
        raw = getattr(location, "raw", None) or {}
        structured = _format_from_components(raw.get("address") or {})
        display = structured or CleanAddressDisplay(location.address or "Unknown")
        add = AddressFromLatLong()
        add.latitude = latitude
        add.longitude = longitude
        add.address = display
        add.date = now().date()
        add.save()
        return display
    except Exception:
        # return unknown and don't save it of course
        return "Unknown"
