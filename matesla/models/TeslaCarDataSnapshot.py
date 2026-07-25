from random import random

from django.db import models
from django.utils import timezone

from matesla.BatteryDegradation import (
    ComputeBatteryDegradationFromEPARange,
    GetEPARangeFromCache,
    ComputeNumCycles,
)
from matesla.models.VinHash import HashTheVin


def _int(value, default=0):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value, default=False):
    if value is None:
        return default
    return bool(value)


class TeslaCarDataSnapshot(models.Model):
    """Time-series sample for graphs (TeslaFi-style history)."""

    vin = models.TextField()
    hashedVin = models.TextField(null=True)
    Date = models.DateTimeField(default=timezone.now, db_index=True)
    DateOnlyDay = models.DateField(null=True)
    # charge_state
    battery_level = models.IntegerField()
    battery_range = models.FloatField()
    charge_limit_soc = models.IntegerField()
    charge_rate = models.FloatField()
    charger_actual_current = models.IntegerField()
    charger_phases = models.IntegerField()
    charger_power = models.IntegerField()
    charger_voltage = models.IntegerField()
    charging_state = models.TextField()
    est_battery_range = models.FloatField()
    fast_charger_brand = models.TextField(null=True)
    fast_charger_present = models.BooleanField()
    fast_charger_type = models.TextField(null=True)
    max_range_charge_counter = models.IntegerField()
    usable_battery_level = models.IntegerField()
    # climate_state
    climate_keeper_mode = models.BooleanField()
    driver_temp_setting = models.FloatField()
    inside_temp = models.FloatField()
    is_climate_on = models.BooleanField()
    outside_temp = models.FloatField()
    passenger_temp_setting = models.FloatField()
    # drive_state
    latitude = models.FloatField()
    longitude = models.FloatField()
    power = models.IntegerField()
    speed = models.IntegerField()
    # vehicle_state
    odometer = models.IntegerField()  # miles
    battery_degradation = models.FloatField(null=True)
    NumberCycles = models.FloatField(null=True)
    randomNr = models.FloatField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["vin"]),
            models.Index(fields=["hashedVin"]),
            models.Index(fields=["randomNr"]),
            models.Index(fields=["hashedVin", "randomNr"]),
            models.Index(fields=["vin", "Date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["vin", "Date"],
                name="TeslaCarDataSnapshot: unique version at same date for car",
            )
        ]

    def SaveSnapshot(self, vin, context):
        """Always insert a new sample (TeslaFi-style). Safe for Fleet null fields."""
        now = timezone.now()
        charge_state = context.get("charge_state") or {}
        climate_state = context.get("climate_state") or {}
        drive_state = context.get("drive_state") or {}
        vehicle_state = context.get("vehicle_state") or {}

        self.vin = vin
        self.hashedVin = HashTheVin(vin)
        self.Date = now
        self.DateOnlyDay = now.date()

        self.battery_level = _int(charge_state.get("battery_level"))
        self.battery_range = _float(charge_state.get("battery_range"))
        self.charge_limit_soc = _int(charge_state.get("charge_limit_soc"), 80)
        self.charge_rate = _float(charge_state.get("charge_rate"))
        self.charger_actual_current = _int(charge_state.get("charger_actual_current"))
        self.charger_phases = _int(charge_state.get("charger_phases"))
        self.charger_power = _int(charge_state.get("charger_power"))
        self.charger_voltage = _int(charge_state.get("charger_voltage"))
        self.charging_state = charge_state.get("charging_state") or "Unknown"
        self.est_battery_range = _float(
            charge_state.get("est_battery_range") or charge_state.get("battery_range")
        )
        brand = charge_state.get("fast_charger_brand")
        self.fast_charger_brand = None if brand in (None, "<invalid>") else brand
        self.fast_charger_present = _bool(charge_state.get("fast_charger_present"))
        ftype = charge_state.get("fast_charger_type")
        self.fast_charger_type = None if ftype in (None, "<invalid>") else ftype
        self.max_range_charge_counter = _int(charge_state.get("max_range_charge_counter"))
        self.usable_battery_level = _int(
            charge_state.get("usable_battery_level"), self.battery_level
        )

        keeper = climate_state.get("climate_keeper_mode")
        self.climate_keeper_mode = bool(keeper) and keeper != "off"
        self.driver_temp_setting = _float(climate_state.get("driver_temp_setting"), 20.0)
        self.inside_temp = _float(climate_state.get("inside_temp"), self.driver_temp_setting)
        self.is_climate_on = _bool(climate_state.get("is_climate_on"))
        self.outside_temp = _float(climate_state.get("outside_temp"), self.inside_temp)
        self.passenger_temp_setting = _float(
            climate_state.get("passenger_temp_setting"), self.driver_temp_setting
        )

        self.latitude = _float(drive_state.get("latitude"))
        self.longitude = _float(drive_state.get("longitude"))
        self.power = _int(drive_state.get("power"))
        self.speed = _int(drive_state.get("speed"))

        self.odometer = _int(vehicle_state.get("odometer"))
        epa = GetEPARangeFromCache(vin)
        self.NumberCycles = ComputeNumCycles(epa, self.odometer)
        self.battery_degradation = ComputeBatteryDegradationFromEPARange(
            self.battery_range, self.usable_battery_level, epa
        )
        self.randomNr = random()
        self.save()
        return self

    def SaveIfDontExistsYet(self, vin, context):
        """Backward-compatible name: always records a new snapshot."""
        return self.SaveSnapshot(vin, context)
