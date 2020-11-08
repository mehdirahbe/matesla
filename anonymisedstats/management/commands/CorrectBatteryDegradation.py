from django.core.management.base import BaseCommand

from matesla.BatteryDegradation import GetEPARangeFromCache, ComputeBatteryDegradationFromEPARange, GetEPARange
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot


# This is used to recompute all battery degradation.
# First run was in november 2020 when I did realise that there was 2 charge level:
# real and usable, the latter being displayed in the car and the app.
# Logically, degradation is to be computed on usable battery as km of range are
# based on the usable one. And as it was computed on the real battery %,
# it did display a slightly higher degradation, especially when battery
# was far from full or cold-->solution: recompute all.

class Command(BaseCommand):
    help = 'Read all TeslaCarDataSnapshot and recompute battery degradation.'

    def handle(self, *args, **options):
        # loop on all cars
        allCars = TeslaCarInfo.objects.values()
        for car in allCars:
            # get the vin
            vin = car["vin"]
            print(vin)

            # get EPA range
            EPARange = GetEPARangeFromCache(vin)
            # can occur for old entries from may, when epa range was not saved yet
            if EPARange is None:
                # recompute it+ save in model
                GetEPARange(vin)
                EPARange = GetEPARangeFromCache(vin)
                # still nothing? Give up...
                if EPARange is None:
                    continue

            # some entries to recalculate?
            count = TeslaCarDataSnapshot.objects.filter(vin=vin).count()
            if count == 0:
                continue

            # now we cn update
            alltoUpdate = TeslaCarDataSnapshot.objects.filter(vin=vin)
            for entry in alltoUpdate:
                old = entry.battery_degradation
                # if an old entry where degradation was not yet computed
                if old is None:
                    old = 0.
                entry.battery_degradation = ComputeBatteryDegradationFromEPARange(
                    entry.battery_range, entry.usable_battery_level, EPARange)
                # if different of at lat 0.1% (for real, an exact match in a bad idea)
                if int(old * 10) != int(entry.battery_degradation * 10) and entry.battery_degradation >= 0.:
                    entry.save(update_fields=['battery_degradation'])
        return
