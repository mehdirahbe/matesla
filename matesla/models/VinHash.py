import re
from mysite.settings import SECRET_KEY
import hashlib


# check that hash contains only chars and digits and thus cannot be
# used for SQL injection
def IsValidHash(strg):
    if strg is None:
        return False
    search = re.compile(r'[^a-z0-9.]').search
    return not bool(search(strg))


# Hash the vin so that it can be used in an non guessable URL to display the
# graphs with stats on the car
def HashTheVin(vin):
    if vin is None:
        return None
    # see https://docs.python.org/3/library/hashlib.html
    b = bytearray()
    b.extend(map(ord, vin + SECRET_KEY))
    return hashlib.sha224(b).hexdigest()
