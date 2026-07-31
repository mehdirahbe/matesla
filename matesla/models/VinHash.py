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
    """
    if hash_string is None:
        return False
    disallowed_character = re.compile(r"[^a-z0-9.]").search
    return not bool(disallowed_character(hash_string))


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
