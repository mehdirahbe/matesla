from random import random

from django.utils import timezone
from django.db import models

from matesla.BatteryDegradation import ComputeBatteryDegradationFromEPARange, GetEPARangeFromCache, ComputeNumCycles
from matesla.models.VinHash import HashTheVin


# Data to save car info which change


class TeslaCarDataSnapshot(models.Model):
    vin = models.TextField()  # vin of the car (allow to link with TeslaCarInfo is necessary)
    hashedVin = models.TextField(null=True)  # hash of the vin, to use in URL of graphs
    Date = models.DateTimeField(default=timezone.now)  # When the data was taken
    DateOnlyDay = models.DateField(null=True)  # The day when the data was taken
    # From charge_state
    battery_level = models.IntegerField()  # IE 71
    battery_range = models.FloatField()  # IE 205.95
    charge_limit_soc = models.IntegerField()  # IE 80
    charge_rate = models.FloatField()  # IE 0.0
    charger_actual_current = models.IntegerField()  # IE 0
    charger_phases = models.IntegerField()  # IE None
    charger_power = models.IntegerField()  # IE 0
    charger_voltage = models.IntegerField()  # IE 2
    charging_state = models.TextField()  # IE Disconnected
    est_battery_range = models.FloatField()  # IE 160.87
    fast_charger_brand = models.TextField(null=True)  # IE <invalid>
    fast_charger_present = models.BooleanField()  # IE False
    fast_charger_type = models.TextField(null=True)  # IE <invalid>
    max_range_charge_counter = models.IntegerField()  # IE 0
    usable_battery_level = models.IntegerField()  # IE 71
    # From climate_state
    climate_keeper_mode = models.BooleanField()  # IE off
    driver_temp_setting = models.FloatField()  # IE 20.5
    inside_temp = models.FloatField()  # IE 20.8
    is_climate_on = models.BooleanField()  # IE False
    outside_temp = models.FloatField()  # IE 20.5
    passenger_temp_setting = models.FloatField()  # IE 20.0
    # From drive_state
    latitude = models.FloatField()  # IE 50.79621
    longitude = models.FloatField()  # IE 4.335445
    power = models.IntegerField()  # IE 0
    speed = models.IntegerField()  # IE None
    # from vehicle_state
    odometer = models.IntegerField()  # in miles
    battery_degradation = models.FloatField(null=True)  # computed degradation in %
    NumberCycles = models.FloatField(null=True)  # Number of cycles of the battery
    randomNr = models.FloatField(null=True)  # random number to easily generate samples

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        indexes = [
            # to retrieve easily all infos on a car
            models.Index(fields=['vin']),
            models.Index(fields=['hashedVin']),
            # to generate top samples in a fast way
            models.Index(fields=['randomNr']),
            # idem in perso stats
            models.Index(fields=['hashedVin', 'randomNr']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['vin', 'Date'],
                                    name='TeslaCarDataSnapshot: unique version at same date for car')
        ]

    def SaveIfDontExistsYet(self, vin, context):
        if TeslaCarDataSnapshot.objects.filter(vin=vin).filter(Date=timezone.now()).count() == 0:
            # Add the car
            self.vin = vin
            self.hashedVin = HashTheVin(vin)
            self.DateOnlyDay = timezone.now().date()
            # From charge_state
            charge_state = context['charge_state']
            self.battery_level = charge_state['battery_level']
            self.battery_range = charge_state['battery_range']
            self.charge_limit_soc = charge_state['charge_limit_soc']
            self.charge_rate = charge_state['charge_rate']
            self.charger_actual_current = charge_state['charger_actual_current']
            self.charger_phases = charge_state['charger_phases']
            if self.charger_phases is None:
                self.charger_phases = 0
            self.charger_power = charge_state['charger_power']
            self.charger_voltage = charge_state['charger_voltage']
            self.charging_state = charge_state['charging_state']
            self.est_battery_range = charge_state['est_battery_range']
            self.fast_charger_brand = charge_state['fast_charger_brand']
            if self.fast_charger_brand == '<invalid>':
                self.fast_charger_brand = None
            self.fast_charger_present = charge_state['fast_charger_present']
            self.fast_charger_type = charge_state['fast_charger_type']
            if self.fast_charger_type == '<invalid>':
                self.fast_charger_type = None
            self.max_range_charge_counter = charge_state['max_range_charge_counter']
            self.usable_battery_level = charge_state['usable_battery_level']
            # From climate_state
            climate_state = context['climate_state']
            # Why did tesla put a text value for this?
            if climate_state['climate_keeper_mode'] == 'off':
                self.climate_keeper_mode = False
            else:
                self.climate_keeper_mode = True
            self.driver_temp_setting = climate_state['driver_temp_setting']
            self.inside_temp = climate_state['inside_temp']
            self.is_climate_on = climate_state['is_climate_on']
            self.outside_temp = climate_state['outside_temp']
            self.passenger_temp_setting = climate_state['passenger_temp_setting']
            # From drive_state
            drive_state = context['drive_state']
            self.latitude = drive_state['latitude']
            self.longitude = drive_state['longitude']
            self.power = drive_state['power']
            self.speed = drive_state['speed']
            if self.speed is None:
                self.speed = 0
            # from vehicle_state
            vehicle_state = context['vehicle_state']
            self.odometer = vehicle_state['odometer']
            EPARange = GetEPARangeFromCache(vin)
            self.NumberCycles = ComputeNumCycles(EPARange, self.odometer)
            self.battery_degradation = ComputeBatteryDegradationFromEPARange(
                self.battery_range, self.usable_battery_level, EPARange)
            self.randomNr = random()
            self.save()
