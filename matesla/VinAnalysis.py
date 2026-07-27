"""VIN helpers: year, model, drivetrain, plant, wheel size, rough trim."""

import re


# Return the year, see https://en.wikipedia.org/wiki/Vehicle_identification_number
# in practical, char 10 (base 1) mean: A is 2010, K is 2019 and L 2020
# and Y will be 2030 (no letter Z, 1 is 2031). And many holes, so complex
def GetYearFromVin(vin):
    if not vin or len(vin) < 10:
        return None
    letter = vin[9]
    if "A" <= letter <= "H":
        return ord(letter) - ord("A") + 2010
    if "J" <= letter <= "N":
        return ord(letter) - ord("J") + 2018
    if letter == "P":
        return 2023
    if "R" <= letter <= "T":
        return ord(letter) - ord("R") + 2024
    if "V" <= letter <= "Y":
        return ord(letter) - ord("V") + 2027
    if "1" <= letter <= "9":
        return ord(letter) - ord("1") + 2031
    return None


# Pos 4 (base 1) is the model->S3XY
def GetModelFromVin(vin):
    if not vin or len(vin) < 4:
        return None
    letter = vin[3]
    return letter


# Pos 8 (base 1) allow to know if single or dual motor
def IsDualMotor(vin):
    if not vin or len(vin) < 8:
        return None
    letter = vin[7]
    # 4=performance dual motor, cf teslatap
    # 5 = P2 Dual Motor
    # B = Dual Motor - Standard Model 3
    # C = Dual Motor - Performance Model 3
    # E = Dual Motor - Standard Model Y
    # F = Dual Motor - Performance Model Y
    # K = Dual Motor - China
    if letter in ("2", "5", "B", "C", "E", "F", "K", "4"):
        return True
    # A = Single Motor - Standard Model 3
    # D = Single Motor - Standard or Performance Model Y
    if letter in ("A", "D"):
        return False
    return None


def IsPerformanceMotor(vin) -> bool:
    """Best-effort: VIN motor codes associated with Performance."""
    if not vin or len(vin) < 8:
        return False
    return vin[7] in ("C", "F", "4")


def GetPlantRegionFromVin(vin) -> str | None:
    """
    Manufacturing region from WMI / plant code.
    US / CN / EU — used for EPA catalog hints (not perfect for EU-delivered US cars).
    """
    if not vin or len(vin) < 3:
        return None
    wmi = vin[0:3].upper()
    # World manufacturer identifiers
    if wmi in ("LRW",):  # Tesla China (Shanghai)
        return "CN"
    if wmi in ("XP7",):  # Tesla Germany (Berlin) Model Y
        return "EU"
    if wmi in ("5YJ", "7SA", "7G2"):  # Fremont / Austin / US
        # Plant digit (pos 11, base 1) can refine
        if len(vin) >= 11:
            plant = vin[10].upper()
            if plant in ("C",):  # sometimes used; prefer WMI
                pass
            if plant in ("B",):
                return "EU"
            if plant in ("A",):  # Austin
                return "US"
            if plant in ("F", "P", "R", "N", "C"):
                # F=Fremont common; C on US WMI is still US
                return "US"
        return "US"
    # Fallback plant char only
    if len(vin) >= 11:
        plant = vin[10].upper()
        if plant == "C" and wmi.startswith("LR"):
            return "CN"
        if plant == "B":
            return "EU"
        if plant in ("F", "A", "P"):
            return "US"
    return None


def WheelInchesFromType(wheel_type) -> int | None:
    """Extract diameter from Fleet wheel_type (Pinwheel18, Glider18, UberTurbine19…)."""
    if not wheel_type:
        return None
    m = re.search(r"(1[5-9]|2[0-3])", str(wheel_type))
    if not m:
        return None
    return int(m.group(1))


def GuessTrimFromVin(vin, *, dual=None, performance=None) -> str | None:
    """
    Rough trim from VIN only.

    Dual motor → lr (or perf). Single motor → ambiguous (sr vs lr): return None
    so the EPA picker can use projected full-charge range to decide.
    """
    if performance is None:
        performance = IsPerformanceMotor(vin)
    if performance:
        return "perf"
    if dual is None:
        dual = IsDualMotor(vin)
    if dual is True:
        return "lr"
    # RWD: SR+ vs LR RWD cannot be told from motor letter alone
    return None
