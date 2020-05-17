'''Return the battery degradation in %. The problem is to known EPA mileage as
it is not returned and due to invalid codes, it is impossible to know exact car
 and battery model.
 So compute when we can and return None when impossible to compute safely
 batteryrange should be in miles, battery_level is in %'''




# we can get fairly precise extracting from vin car model, year and motors.
# but we miss one info: battery power. If for model 3, there is SR, sr+,
# medium and LR all being single motor.
# Return range (or none)+model, isDual, year extracted from vin
from matesla.VinAnalysis import GetModelFromVin, IsDualMotor, GetYearFromVin
from matesla.models.TeslaCarInfo import TeslaCarInfo

# Return range in miles from cache
def GetEPARangeFromCache(vin):
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0 and carInfos[0].EPARange is not None:
        return carInfos[0].EPARange
    return None


def GetEPARange(vin):
    # Do we already have it?
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    carInfo = None
    if len(carInfos) > 0:
        carInfo = carInfos[0]
        if carInfo.EPARange is not None:
            return carInfo.EPARange, GetModelFromVin(vin), carInfo.isDualMotor, carInfo.modelYear

    # First check which car it is
    # 5YJ3E7EA7KF123456 seems to be the rare LR 1 motor
    # 5YJ3E7EB1KF123456 AWD (mine, I am sure)
    # 5YJ3E7EA4LF123456 std range 1 moteur (friend, I am sure too)
    # 5YJSA7E2XJF123456 model s
    # found the official values on https://www.fueleconomy.gov/feg/Find.do?action=sbsSelect
    model = GetModelFromVin(vin)
    isDual = IsDualMotor(vin)
    year = GetYearFromVin(vin)
    if model is None or isDual is None or year is None:
        return None
    EPARange = None

    # for the car we can identify, assume most frequent configuration
    if model == "3" and isDual == True:
        '''up to 2019, was 310. Then should be 330
        But there is a 2020 model thus brand new we can assume without degradation, and it
        displays 7.4 % if we use 330 (we are in may).
        I thus assume that tesla continue to return the number of miles
        according to old epa
        See https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=41189 for <=2019
        and https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=42274 for 2020'''
        EPARange = 310
    if model == "3" and isDual == False:
        # Same thing here... 2020 EPA range seems not used
        EPARange = 240  # https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=41416
    if model == "S" and isDual == True:
        # no need to test year as 259 seems constant for 75D
        EPARange = 259  # for the 75D https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=39838

    # Save computed value
    if carInfo is not None:
        carInfo.EPARange = EPARange
        carInfo.save()

    return EPARange, model, isDual, year


# Update battery degradation, in case our very crude
# GetEPARange initial result did induce a negative battery
# degradation
def UpdateBatteryDegradation(vin, EPARange):
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0:
        carInfo = carInfos[0]
        carInfo.EPARange = EPARange
        carInfo.save()

#return battery degradation in % with batteryrange vs EPARange (in miles) and battery_level (in %)
def ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange):
    if EPARange is None:
        return None
    batterydegradation = (1. - ((1. * batteryrange) / (battery_level * 1.) * 100.) / EPARange) * 100.
    return batterydegradation

def ComputeBatteryDegradation(batteryrange, battery_level, vin):
    EPARange, model, isDual, year = GetEPARange(vin)
    if EPARange is None:
        return None
    batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
    if model == "3" and isDual is False and batterydegradation < -5:
        '''From https://en.wikipedia.org/wiki/Tesla_Model_3
        RWD: 325 miles (523 km) combined
        single motor with strongly negative battery degradatation means
        probably the LR single motor. It would be nice if someone
        with that model confirms'''
        EPARange = 325.
        batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
        if batterydegradation >= 0.:
            UpdateBatteryDegradation(vin, EPARange)
    if model == "S" and isDual == True and batterydegradation < 0:
        # see all the range for 85, 90, 100 kwh batteries https://en.wikipedia.org/wiki/Tesla_Model_S
        ranges = [270., 294., 335.]
        for EPARange in ranges:
            batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
            if batterydegradation >= 0.:
                UpdateBatteryDegradation(vin, EPARange)
                return batterydegradation
    if batterydegradation < 0.:  # don't return negative degradation
        batterydegradation = 0.
    return batterydegradation
