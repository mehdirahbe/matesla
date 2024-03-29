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
        and https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=42274 for 2020
        
        And for 2021, with a new 82 kwH battery:
        https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=43401  353 miles LR
        https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=43402 315 perf
        
        But the 20 december 2020, I found in the site someone with a 2021 car
        (vin 5YJ3E7EB0MF...) and its autonomy at 100% is about 541 km=338 miles
        (325 km at 50%)
        
        And electrek (https://electrek.co/2020/12/18/tesla-increases-model-3-range-software-update/)
        says that 2020.48.12 is bringing a range upgrade (the car has that version).
        
        So, what is the range??? In a first step, I will use 338. Will refine furter
        when we know more.
        
        Edit june 2021: a new car delivered in june has 567 km=352 miles
        EAP says 353. It does match, so lets use that
        '''
        EPARange = 310
        if year is not None and year >= 2021:
            EPARange = 353  # https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=43401
    if model == "3" and isDual == False:
        # Same thing here... 2020 EPA range seems not used
        EPARange = 240  # https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=41416
        if year is not None and year >= 2021:
            EPARange = 263  # https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=41416&id=43821
    if model == "S" and isDual == True:
        # no need to test year as 259 seems constant for 75D
        EPARange = 259  # for the 75D https://www.fueleconomy.gov/feg/Find.do?action=sbs&id=39838

    # Save computed value
    if carInfo is not None:
        carInfo.EPARange = EPARange
        carInfo.save()

    return EPARange, model, isDual, year


# Update EPARange (miles), in case our very crude
# GetEPARange initial result did induce a negative battery
# degradation
def UpdateBatteryEPARange(vin, EPARange):
    carInfos = TeslaCarInfo.objects.filter(vin=vin)
    if len(carInfos) > 0:
        carInfo = carInfos[0]
        carInfo.EPARange = EPARange
        carInfo.save()


# return battery degradation in % with batteryrange vs EPARange (in miles) and battery_level (in %)
def ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange):
    if EPARange is None:
        return None
    batterydegradation = (1. - ((1. * batteryrange) / (battery_level * 1.) * 100.) / EPARange) * 100.
    return batterydegradation


def ComputeNumCycles(EPARange, odometerMiles):
    if EPARange is None or odometerMiles is None:
        return None
    if EPARange == 0:
        return None
    cycles = (1. * odometerMiles) / EPARange
    # rough guess add 20 % due to regenerative braking adding cycles to the battery
    # and vampire drain being reffiled without the car driving
    cycles = cycles * 1.2
    return cycles


# return battery degradation and number of cycles of the battery+EPA Range in Miles
# odometer is used to estimates number of cycles
# usable_battery_level must be used.
def ComputeBatteryDegradation(batteryrange, battery_level, vin, odometerMiles):
    EPARange, model, isDual, year = GetEPARange(vin)
    if EPARange is None:
        return None, None, None
    batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
    # note 15/9/2021: limit was -5%. Very dangerous, as SR+ went in 2021 from 240 to 263 miles and did fall in this code.
    # Hence receiving 325 miles of autonomy and letting the user have an apparent 20% of degradation on his new battery
    # so correction was double: adapt autonomy for SR from 2021 and add a far bigger percentage here (20 instead of 5)
    if model == "3" and isDual is False and batterydegradation < -20:
        '''From https://en.wikipedia.org/wiki/Tesla_Model_3
        RWD: 325 miles (523 km) combined
        single motor with strongly negative battery degradatation means
        probably the LR single motor. It would be nice if someone
        with that model confirms'''
        EPARange = 325.
        batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
        if batterydegradation >= 0.:
            UpdateBatteryEPARange(vin, EPARange)
    if model == "S" and isDual == True and batterydegradation < 0:
        # see all the range for 85, 90, 100 kwh batteries https://en.wikipedia.org/wiki/Tesla_Model_S
        ranges = [270., 294., 335.]
        for EPARange in ranges:
            batterydegradation = ComputeBatteryDegradationFromEPARange(batteryrange, battery_level, EPARange)
            if batterydegradation >= 0.:
                UpdateBatteryEPARange(vin, EPARange)
                return batterydegradation, ComputeNumCycles(EPARange, odometerMiles), EPARange
    if batterydegradation < 0.:  # don't return negative degradation
        batterydegradation = 0.
    return batterydegradation, ComputeNumCycles(EPARange, odometerMiles), EPARange
