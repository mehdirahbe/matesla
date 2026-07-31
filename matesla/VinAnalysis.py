"""VIN helpers: year, model, drivetrain, plant, wheel size, rough trim."""

import re
from urllib.parse import quote


# Tesla WMIs that NHTSA vPIC usually decodes (US manufacture / US sales registration).
_NHTSA_FRIENDLY_WMI = frozenset(
    {
        "5YJ",  # Tesla US (Model S/3/X/Y historically)
        "7SA",  # Tesla US (Cybertruck / newer)
        "7G2",  # Tesla US (some Austin)
    }
)


def GetVinDecoderUrl(vin) -> str:
    """
    External VIN decoder link.

    - US WMIs → NHTSA with vin= pre-filled (works well for Fremont/Austin).
    - China / Europe (LRW, XP7, …) → TeslaTap (NHTSA returns error 7 / empty).
      TeslaTap has no URL prefill; user pastes if needed, but decode quality is better.
    """
    if not vin:
        return "https://teslatap.com/vin-decoder/"
    vin = str(vin).strip().upper()
    if len(vin) < 3:
        return "https://teslatap.com/vin-decoder/"
    world_manufacturer_id = vin[:3]
    if world_manufacturer_id in _NHTSA_FRIENDLY_WMI:
        # Official US decoder (GET form action is /decoder/VinDecoder, not /Decoder)
        return (
            "https://vpic.nhtsa.dot.gov/decoder/VinDecoder"
            f"?vin={quote(vin, safe='')}"
        )
    return "https://teslatap.com/vin-decoder/"


def GetYearFromVin(vin):
    """
    Model year from VIN position 10 (1-based).

    See https://en.wikipedia.org/wiki/Vehicle_identification_number
    Practical mapping: A=2010 … K=2019, L=2020, Y=2030 (no Z), 1=2031, …
    """
    if not vin or len(vin) < 10:
        return None
    year_code = vin[9]
    if "A" <= year_code <= "H":
        return ord(year_code) - ord("A") + 2010
    if "J" <= year_code <= "N":
        return ord(year_code) - ord("J") + 2018
    if year_code == "P":
        return 2023
    if "R" <= year_code <= "T":
        return ord(year_code) - ord("R") + 2024
    if "V" <= year_code <= "Y":
        return ord(year_code) - ord("V") + 2027
    if "1" <= year_code <= "9":
        return ord(year_code) - ord("1") + 2031
    return None


def GetModelFromVin(vin):
    """Model letter from VIN position 4 (1-based): S / 3 / X / Y / …"""
    if not vin or len(vin) < 4:
        return None
    return vin[3]


def IsDualMotor(vin):
    """
    Dual vs single motor from VIN position 8 (1-based) motor code.

    Codes from TeslaTap community tables (Performance dual, dual std, RWD, …).
    Returns True/False, or None if the letter is unknown.
    """
    if not vin or len(vin) < 8:
        return None
    motor_code = vin[7]
    # Dual motor codes (incl. performance dual)
    if motor_code in ("2", "5", "B", "C", "E", "F", "K", "4"):
        return True
    # Single motor
    if motor_code in ("A", "D"):
        return False
    return None


def IsPerformanceMotor(vin) -> bool:
    """Best-effort: VIN motor codes associated with Performance packs."""
    if not vin or len(vin) < 8:
        return False
    return vin[7] in ("C", "F", "4")


def GetPlantRegionFromVin(vin) -> str | None:
    """
    Manufacturing region from WMI / plant code: US / CN / EU.

    Used for EPA catalog hints (not perfect for EU-delivered US-built cars).
    """
    if not vin or len(vin) < 3:
        return None
    world_manufacturer_id = vin[0:3].upper()
    if world_manufacturer_id in ("LRW",):  # Tesla China (Shanghai)
        return "CN"
    if world_manufacturer_id in ("XP7",):  # Tesla Germany (Berlin) Model Y
        return "EU"
    if world_manufacturer_id in ("5YJ", "7SA", "7G2"):  # Fremont / Austin / US
        if len(vin) >= 11:
            plant_code = vin[10].upper()
            if plant_code in ("B",):
                return "EU"
            if plant_code in ("A",):  # Austin
                return "US"
            if plant_code in ("F", "P", "R", "N", "C"):
                # F=Fremont common; C on US WMI is still US
                return "US"
        return "US"
    # Fallback: plant character only when WMI is unfamiliar
    if len(vin) >= 11:
        plant_code = vin[10].upper()
        if plant_code == "C" and world_manufacturer_id.startswith("LR"):
            return "CN"
        if plant_code == "B":
            return "EU"
        if plant_code in ("F", "A", "P"):
            return "US"
    return None


def WheelInchesFromType(wheel_type) -> int | None:
    """
    Extract diameter inches from Fleet wheel_type strings.

    Examples: Pinwheel18, Glider18, UberTurbine19 → 18 / 19.
    """
    if not wheel_type:
        return None
    diameter_match = re.search(r"(1[5-9]|2[0-3])", str(wheel_type))
    if not diameter_match:
        return None
    return int(diameter_match.group(1))


def GuessTrimFromVin(vin, *, dual=None, performance=None) -> str | None:
    """
    Rough trim from VIN only: 'perf', 'lr', or None if ambiguous.

    Dual motor → lr (or perf). Single motor → SR vs LR RWD cannot be told
    from the motor letter alone, so return None and let EPA picker use
    projected full-charge range.
    """
    if performance is None:
        performance = IsPerformanceMotor(vin)
    if performance:
        return "perf"
    if dual is None:
        dual = IsDualMotor(vin)
    if dual is True:
        return "lr"
    return None
