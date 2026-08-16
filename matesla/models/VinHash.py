"""
VIN hashing for non-guessable personal-stats URLs.

We never put raw VINs in public graph URLs. HashTheVin(vin) produces a stable
hex digest (sha224 of vin + SECRET_KEY). IsValidHash rejects anything that is
not [a-z0-9.] so path parameters cannot smuggle SQL/injection characters.
"""

import hashlib
import re

from mysite.settings import SECRET_KEY


def IsValidHash(hash_string):
    """
    True if hash_string is a safe path token (lowercase alphanumerics and dots).

    Used as a gate on every personalstats URL that takes hashedVin.
    Format-only: a one-character typo in a real digest still passes.
    Pair with IsKnownHashedVin so unknown tokens 404 instead of an empty page.
    """
    if hash_string is None:
        return False
    disallowed_character = re.compile(r"[^a-z0-9.]").search
    return not bool(disallowed_character(hash_string))


def IsKnownHashedVin(hash_string):
    """
    True if this token belongs to a vehicle we have seen or linked.

    Distinguishes a mistyped URL (404) from a real car with no rows yet
    (legitimate empty state). Checks telemetry, firmware, car info, then
    TeslaVehicle VINs — a newly linked car may have no polls yet.
    """
    if not hash_string or not IsValidHash(hash_string):
        return False
    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

    if TeslaCarDataSnapshot.objects.filter(hashedVin=hash_string).exists():
        return True
    from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory

    if TeslaFirmwareHistory.objects.filter(hashedVin=hash_string).exists():
        return True
    from matesla.models.TeslaCarInfo import TeslaCarInfo

    if TeslaCarInfo.objects.filter(hashedVin=hash_string).exists():
        return True
    from matesla.poll_diagnostics import resolve_vehicle_for_hashed_vin

    return resolve_vehicle_for_hashed_vin(hash_string) is not None


def HashTheVin(vin):
    """
    Stable non-reversible token for a VIN, for use in graph/stats URLs.

    Salted with SECRET_KEY so digests are site-specific and not enumerable
    from public VIN lists alone.
    """
    if vin is None:
        return None
    # https://docs.python.org/3/library/hashlib.html
    payload = bytearray()
    payload.extend(map(ord, vin + SECRET_KEY))
    return hashlib.sha224(payload).hexdigest()
