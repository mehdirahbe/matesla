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
    if letter == "2" or letter == "B":
        return True
    if letter == "A":
        return False
    return None

