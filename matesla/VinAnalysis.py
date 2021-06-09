# Return the year, see https://en.wikipedia.org/wiki/Vehicle_identification_number
# in practical, char 10 (base 1) mean: A is 2010, K is 2019 and L 2020
# and Y will be 2030 (no letter Z, 1 is 2031). And many holes, so complex
def GetYearFromVin(vin):
    if len(vin) < 10:
        return None
    letter = vin[9]
    if 'A' <= letter <= 'H':
        return ord(letter) - ord('A') + 2010
    if 'J' <= letter <= 'N':
        return ord(letter) - ord('J') + 2018
    if letter == 'P':
        return 2023
    if 'R' <= letter <= 'T':
        return ord(letter) - ord('R') + 2024
    if 'V' <= letter <= 'Y':
        return ord(letter) - ord('V') + 2027
    if '1' <= letter <= '9':
        return ord(letter) - ord('1') + 2031
    return None


# Pos 4 (base 1) is the model->S3XY
def GetModelFromVin(vin):
    if len(vin) < 4:
        return None
    letter = vin[3]
    return letter


# Pos 8 (base 1) allow to know if single or dual motor
def IsDualMotor(vin):
    if len(vin) < 8:
        return None
    letter = vin[7]
    # 4=performance dual motor, cf teslatap
    # 5 = P2 Dual Motor
    # B = Dual Motor - Standard Model 3
    # C = Dual Motor - Performance Model 3
    # E = Dual Motor - Standard Model Y
    # F = Dual Motor - Performance Model Y
    # K = Dual Motor - China
    if letter == "2" or letter == "5" or letter == "B" or letter == "C" or letter == "E" or letter == "F" or letter == "K" or letter == "4":
        return True
    # A = Single Motor - Standard Model 3
    # D = Single Motor - Standard or Performance Model Y
    if letter == "A" or letter == "D":
        return False
    return None

