from datetime import timezone as dt_timezone
from random import random

from django.db import models
from django.utils import timezone

from matesla.BatteryDegradation import (
    ComputeBatteryDegradationFromEPARange,
    GetEPARangeFromCache,
    ComputeNumCycles,
)
from matesla.models.VinHash import HashTheVin


def _int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("0", "false", "no", "off", "none"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
    return bool(value)


def _str(value, default=None):
    if value is None or value == "":
        return default
    s = str(value).strip()
    if s in ("<invalid>", "None", "null"):
        return default
    return s


class TeslaCarDataSnapshot(models.Model):
    """Time-series sample for graphs (TeslaFi-compatible field set)."""

    vin = models.TextField()
    hashedVin = models.TextField(null=True, blank=True)
    Date = models.DateTimeField(default=timezone.now, db_index=True)
    DateOnlyDay = models.DateField(null=True, blank=True)

    # --- charge_state (core) ---
    battery_level = models.FloatField(null=True, blank=True)
    battery_range = models.FloatField(null=True, blank=True)
    charge_limit_soc = models.FloatField(null=True, blank=True)
    charge_rate = models.FloatField(null=True, blank=True)
    charger_actual_current = models.FloatField(null=True, blank=True)
    charger_phases = models.FloatField(null=True, blank=True)
    charger_power = models.FloatField(null=True, blank=True)
    charger_voltage = models.FloatField(null=True, blank=True)
    charging_state = models.TextField(null=True, blank=True)
    est_battery_range = models.FloatField(null=True, blank=True)
    ideal_battery_range = models.FloatField(null=True, blank=True)
    fast_charger_brand = models.TextField(null=True, blank=True)
    fast_charger_present = models.BooleanField(null=True, blank=True)
    fast_charger_type = models.TextField(null=True, blank=True)
    max_range_charge_counter = models.FloatField(null=True, blank=True)
    usable_battery_level = models.FloatField(null=True, blank=True)
    charge_energy_added = models.FloatField(null=True, blank=True)
    charge_miles_added_rated = models.FloatField(null=True, blank=True)
    charge_miles_added_ideal = models.FloatField(null=True, blank=True)
    time_to_full_charge = models.FloatField(null=True, blank=True)
    charge_current_request = models.FloatField(null=True, blank=True)
    charge_current_request_max = models.FloatField(null=True, blank=True)
    charge_port_door_open = models.BooleanField(null=True, blank=True)
    battery_heater_on = models.BooleanField(null=True, blank=True)
    battery_current = models.FloatField(null=True, blank=True)
    energy_remaining = models.FloatField(null=True, blank=True)
    pack_voltage = models.FloatField(null=True, blank=True)

    # --- climate_state ---
    climate_keeper_mode = models.BooleanField(null=True, blank=True)
    driver_temp_setting = models.FloatField(null=True, blank=True)
    inside_temp = models.FloatField(null=True, blank=True)
    is_climate_on = models.BooleanField(null=True, blank=True)
    outside_temp = models.FloatField(null=True, blank=True)
    passenger_temp_setting = models.FloatField(null=True, blank=True)
    is_auto_conditioning_on = models.BooleanField(null=True, blank=True)
    fan_status = models.FloatField(null=True, blank=True)
    is_front_defroster_on = models.BooleanField(null=True, blank=True)
    is_rear_defroster_on = models.BooleanField(null=True, blank=True)

    # --- drive_state / location ---
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    power = models.FloatField(null=True, blank=True)
    speed = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    shift_state = models.TextField(null=True, blank=True)
    gps_as_of = models.BigIntegerField(null=True, blank=True)
    elevation = models.FloatField(null=True, blank=True)

    # --- vehicle_state / vehicle ---
    odometer = models.FloatField(null=True, blank=True)  # miles
    state = models.TextField(null=True, blank=True)  # online / asleep / offline
    locked = models.BooleanField(null=True, blank=True)
    car_version = models.TextField(null=True, blank=True)
    is_user_present = models.BooleanField(null=True, blank=True)
    sentry_mode = models.BooleanField(null=True, blank=True)
    display_name = models.TextField(null=True, blank=True)

    # --- TeslaFi session counters (useful history; null from live Fleet) ---
    idle_number = models.IntegerField(null=True, blank=True)
    sleep_number = models.IntegerField(null=True, blank=True)
    drive_number = models.IntegerField(null=True, blank=True)
    charge_number = models.IntegerField(null=True, blank=True)

    # --- computed ---
    battery_degradation = models.FloatField(null=True, blank=True)
    NumberCycles = models.FloatField(null=True, blank=True)
    randomNr = models.FloatField(null=True, blank=True)

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

    def apply_vehicle_data_context(self, vin, context, when=None):
        """Fill fields from nested Fleet/Owner vehicle_data `response`."""
        when = when or timezone.now()
        # Keep Tesla API names as local vars so find-in-file matches the API 1:1
        charge_state = context.get("charge_state") or {}
        climate_state = context.get("climate_state") or {}
        drive_state = context.get("drive_state") or {}
        vehicle_state = context.get("vehicle_state") or {}

        self.vin = vin
        self.hashedVin = HashTheVin(vin)
        self.Date = when
        # Store calendar day in UTC (project TIME_ZONE is UTC)
        self.DateOnlyDay = (
            when.astimezone(dt_timezone.utc).date()
            if timezone.is_aware(when)
            else when.date()
        )

        self.display_name = _str(context.get("display_name"))
        self.state = _str(context.get("state"))

        self.battery_level = _float(charge_state.get("battery_level"))
        self.battery_range = _float(charge_state.get("battery_range"))
        self.charge_limit_soc = _float(charge_state.get("charge_limit_soc"))
        self.charge_rate = _float(charge_state.get("charge_rate"))
        self.charger_actual_current = _float(charge_state.get("charger_actual_current"))
        self.charger_phases = _float(charge_state.get("charger_phases"))
        self.charger_power = _float(charge_state.get("charger_power"))
        self.charger_voltage = _float(charge_state.get("charger_voltage"))
        self.charging_state = _str(charge_state.get("charging_state"), "Unknown")
        self.est_battery_range = _float(
            charge_state.get("est_battery_range") or charge_state.get("battery_range")
        )
        self.ideal_battery_range = _float(charge_state.get("ideal_battery_range"))
        self.fast_charger_brand = _str(charge_state.get("fast_charger_brand"))
        self.fast_charger_present = _bool(charge_state.get("fast_charger_present"))
        self.fast_charger_type = _str(charge_state.get("fast_charger_type"))
        self.max_range_charge_counter = _float(charge_state.get("max_range_charge_counter"))
        self.usable_battery_level = _float(
            charge_state.get("usable_battery_level"), self.battery_level
        )
        self.charge_energy_added = _float(charge_state.get("charge_energy_added"))
        self.charge_miles_added_rated = _float(charge_state.get("charge_miles_added_rated"))
        self.charge_miles_added_ideal = _float(charge_state.get("charge_miles_added_ideal"))
        self.time_to_full_charge = _float(charge_state.get("time_to_full_charge"))
        self.charge_current_request = _float(charge_state.get("charge_current_request"))
        self.charge_current_request_max = _float(
            charge_state.get("charge_current_request_max")
        )
        self.charge_port_door_open = _bool(charge_state.get("charge_port_door_open"))
        self.battery_heater_on = _bool(charge_state.get("battery_heater_on"))
        self.battery_current = _float(charge_state.get("battery_current"))
        self.energy_remaining = _float(charge_state.get("energy_remaining"))
        self.pack_voltage = _float(charge_state.get("pack_voltage"))

        climate_keeper_mode = climate_state.get("climate_keeper_mode")
        if climate_keeper_mode is None:
            self.climate_keeper_mode = None
        else:
            self.climate_keeper_mode = (
                bool(climate_keeper_mode) and climate_keeper_mode != "off"
            )
        self.driver_temp_setting = _float(climate_state.get("driver_temp_setting"))
        self.inside_temp = _float(climate_state.get("inside_temp"))
        self.is_climate_on = _bool(climate_state.get("is_climate_on"))
        self.outside_temp = _float(climate_state.get("outside_temp"))
        self.passenger_temp_setting = _float(climate_state.get("passenger_temp_setting"))
        self.is_auto_conditioning_on = _bool(climate_state.get("is_auto_conditioning_on"))
        self.fan_status = _float(climate_state.get("fan_status"))
        self.is_front_defroster_on = _bool(climate_state.get("is_front_defroster_on"))
        self.is_rear_defroster_on = _bool(climate_state.get("is_rear_defroster_on"))

        self.latitude = _float(drive_state.get("latitude"))
        self.longitude = _float(drive_state.get("longitude"))
        self.power = _float(drive_state.get("power"))
        self.speed = _float(drive_state.get("speed"))
        self.heading = _float(drive_state.get("heading"))
        self.shift_state = _str(drive_state.get("shift_state"))
        self.gps_as_of = _int(drive_state.get("gps_as_of"))
        self.elevation = _float(drive_state.get("elevation") or context.get("elevation"))

        self.odometer = _float(vehicle_state.get("odometer"))
        self.locked = _bool(vehicle_state.get("locked"))
        self.car_version = _str(vehicle_state.get("car_version"))
        self.is_user_present = _bool(vehicle_state.get("is_user_present"))
        self.sentry_mode = _bool(vehicle_state.get("sentry_mode"))

        self._recompute_derived()
        return self

    def apply_flat_row(self, vin, row, when):
        """Fill from a flat TeslaFi CSV row (keys = column names)."""
        self.vin = vin
        self.hashedVin = HashTheVin(vin)
        self.Date = when
        self.DateOnlyDay = (
            when.astimezone(dt_timezone.utc).date()
            if timezone.is_aware(when)
            else when.date()
        )

        self.display_name = _str(row.get("display_name") or row.get("vehicle_name"))
        self.state = _str(row.get("state"))

        self.battery_level = _float(row.get("battery_level"))
        self.battery_range = _float(row.get("battery_range"))
        self.charge_limit_soc = _float(row.get("charge_limit_soc"))
        self.charge_rate = _float(row.get("charge_rate"))
        self.charger_actual_current = _float(row.get("charger_actual_current"))
        self.charger_phases = _float(row.get("charger_phases"))
        self.charger_power = _float(row.get("charger_power"))
        self.charger_voltage = _float(row.get("charger_voltage"))
        self.charging_state = _str(row.get("charging_state"), "Unknown")
        self.est_battery_range = _float(
            row.get("est_battery_range") or row.get("battery_range")
        )
        self.ideal_battery_range = _float(row.get("ideal_battery_range"))
        self.fast_charger_brand = _str(row.get("fast_charger_brand"))
        self.fast_charger_present = _bool(row.get("fast_charger_present"))
        self.fast_charger_type = _str(row.get("fast_charger_type"))
        self.max_range_charge_counter = _float(row.get("max_range_charge_counter"))
        self.usable_battery_level = _float(
            row.get("usable_battery_level"), self.battery_level
        )
        self.charge_energy_added = _float(row.get("charge_energy_added"))
        self.charge_miles_added_rated = _float(row.get("charge_miles_added_rated"))
        self.charge_miles_added_ideal = _float(row.get("charge_miles_added_ideal"))
        self.time_to_full_charge = _float(row.get("time_to_full_charge"))
        self.charge_current_request = _float(row.get("charge_current_request"))
        self.charge_current_request_max = _float(row.get("charge_current_request_max"))
        self.charge_port_door_open = _bool(row.get("charge_port_door_open"))
        self.battery_heater_on = _bool(row.get("battery_heater_on"))
        self.battery_current = _float(row.get("battery_current"))
        self.energy_remaining = _float(row.get("energy_remaining"))
        self.pack_voltage = _float(row.get("pack_voltage"))

        self.climate_keeper_mode = _bool(row.get("climate_keeper_mode"))
        self.driver_temp_setting = _float(row.get("driver_temp_setting"))
        self.inside_temp = _float(row.get("inside_temp"))
        self.is_climate_on = _bool(row.get("is_climate_on"))
        self.outside_temp = _float(row.get("outside_temp"))
        self.passenger_temp_setting = _float(row.get("passenger_temp_setting"))
        self.is_auto_conditioning_on = _bool(row.get("is_auto_conditioning_on"))
        self.fan_status = _float(row.get("fan_status"))
        self.is_front_defroster_on = _bool(row.get("is_front_defroster_on"))
        self.is_rear_defroster_on = _bool(row.get("is_rear_defroster_on"))

        self.latitude = _float(row.get("latitude"))
        self.longitude = _float(row.get("longitude"))
        self.power = _float(row.get("power"))
        self.speed = _float(row.get("speed"))
        self.heading = _float(row.get("heading"))
        self.shift_state = _str(row.get("shift_state"))
        self.gps_as_of = _int(row.get("gps_as_of"))
        self.elevation = _float(row.get("elevation"))

        self.odometer = _float(row.get("odometer"))
        self.locked = _bool(row.get("locked"))
        self.car_version = _str(row.get("car_version"))
        self.is_user_present = _bool(row.get("is_user_present"))
        self.sentry_mode = _bool(row.get("sentry_mode"))

        self.idle_number = _int(row.get("idleNumber") or row.get("idle_number"))
        self.sleep_number = _int(row.get("sleepNumber") or row.get("sleep_number"))
        self.drive_number = _int(row.get("driveNumber") or row.get("drive_number"))
        self.charge_number = _int(row.get("chargeNumber") or row.get("charge_number"))

        self._recompute_derived()
        return self

    def merge_from_flat_row(self, row):
        """Complete empty fields on an existing row from TeslaFi data (nearest-minute merge)."""
        field_map = {
            "display_name": lambda: _str(row.get("display_name") or row.get("vehicle_name")),
            "state": lambda: _str(row.get("state")),
            "battery_level": lambda: _float(row.get("battery_level")),
            "battery_range": lambda: _float(row.get("battery_range")),
            "charge_limit_soc": lambda: _float(row.get("charge_limit_soc")),
            "charge_rate": lambda: _float(row.get("charge_rate")),
            "charger_actual_current": lambda: _float(row.get("charger_actual_current")),
            "charger_phases": lambda: _float(row.get("charger_phases")),
            "charger_power": lambda: _float(row.get("charger_power")),
            "charger_voltage": lambda: _float(row.get("charger_voltage")),
            "charging_state": lambda: _str(row.get("charging_state")),
            "est_battery_range": lambda: _float(row.get("est_battery_range")),
            "ideal_battery_range": lambda: _float(row.get("ideal_battery_range")),
            "fast_charger_brand": lambda: _str(row.get("fast_charger_brand")),
            "fast_charger_present": lambda: _bool(row.get("fast_charger_present")),
            "fast_charger_type": lambda: _str(row.get("fast_charger_type")),
            "max_range_charge_counter": lambda: _float(row.get("max_range_charge_counter")),
            "usable_battery_level": lambda: _float(row.get("usable_battery_level")),
            "charge_energy_added": lambda: _float(row.get("charge_energy_added")),
            "charge_miles_added_rated": lambda: _float(row.get("charge_miles_added_rated")),
            "charge_miles_added_ideal": lambda: _float(row.get("charge_miles_added_ideal")),
            "time_to_full_charge": lambda: _float(row.get("time_to_full_charge")),
            "charge_current_request": lambda: _float(row.get("charge_current_request")),
            "charge_current_request_max": lambda: _float(row.get("charge_current_request_max")),
            "charge_port_door_open": lambda: _bool(row.get("charge_port_door_open")),
            "battery_heater_on": lambda: _bool(row.get("battery_heater_on")),
            "battery_current": lambda: _float(row.get("battery_current")),
            "energy_remaining": lambda: _float(row.get("energy_remaining")),
            "pack_voltage": lambda: _float(row.get("pack_voltage")),
            "driver_temp_setting": lambda: _float(row.get("driver_temp_setting")),
            "inside_temp": lambda: _float(row.get("inside_temp")),
            "is_climate_on": lambda: _bool(row.get("is_climate_on")),
            "outside_temp": lambda: _float(row.get("outside_temp")),
            "passenger_temp_setting": lambda: _float(row.get("passenger_temp_setting")),
            "is_auto_conditioning_on": lambda: _bool(row.get("is_auto_conditioning_on")),
            "fan_status": lambda: _float(row.get("fan_status")),
            "is_front_defroster_on": lambda: _bool(row.get("is_front_defroster_on")),
            "is_rear_defroster_on": lambda: _bool(row.get("is_rear_defroster_on")),
            "latitude": lambda: _float(row.get("latitude")),
            "longitude": lambda: _float(row.get("longitude")),
            "power": lambda: _float(row.get("power")),
            "speed": lambda: _float(row.get("speed")),
            "heading": lambda: _float(row.get("heading")),
            "shift_state": lambda: _str(row.get("shift_state")),
            "gps_as_of": lambda: _int(row.get("gps_as_of")),
            "elevation": lambda: _float(row.get("elevation")),
            "odometer": lambda: _float(row.get("odometer")),
            "locked": lambda: _bool(row.get("locked")),
            "car_version": lambda: _str(row.get("car_version")),
            "is_user_present": lambda: _bool(row.get("is_user_present")),
            "sentry_mode": lambda: _bool(row.get("sentry_mode")),
            "idle_number": lambda: _int(row.get("idleNumber") or row.get("idle_number")),
            "sleep_number": lambda: _int(row.get("sleepNumber") or row.get("sleep_number")),
            "drive_number": lambda: _int(row.get("driveNumber") or row.get("drive_number")),
            "charge_number": lambda: _int(row.get("chargeNumber") or row.get("charge_number")),
        }
        changed = []
        for name, getter in field_map.items():
            cur = getattr(self, name, None)
            if cur is not None and cur != "":
                continue
            val = getter()
            if val is not None:
                setattr(self, name, val)
                changed.append(name)
        if changed:
            self._recompute_derived()
        return changed

    def _recompute_derived(self):
        if self.randomNr is None:
            self.randomNr = random()
        # Fleet API battery_level is whole %; refine from battery_range when possible
        # (TeslaFi rows are already fractional and are left unchanged).
        if self.vin and self.battery_range is not None and self.battery_level is not None:
            from matesla.soc_refine import apply_soc_refinement

            self.battery_level, self.usable_battery_level = apply_soc_refinement(
                self.battery_level,
                self.usable_battery_level,
                self.battery_range,
                self.vin,
            )
        if self.odometer is not None and self.vin:
            epa = GetEPARangeFromCache(self.vin)
            self.NumberCycles = ComputeNumCycles(epa, self.odometer)
            if self.battery_range is not None and self.usable_battery_level is not None:
                self.battery_degradation = ComputeBatteryDegradationFromEPARange(
                    self.battery_range, self.usable_battery_level, epa
                )

    def SaveSnapshot(self, vin, context, when=None):
        """Insert a new sample from live vehicle_data."""
        self.apply_vehicle_data_context(vin, context, when=when)
        # Defaults for NOT NULL legacy columns if somehow still required
        if self.charging_state is None:
            self.charging_state = "Unknown"
        self.save()
        return self

    def SaveIfDontExistsYet(self, vin, context):
        return self.SaveSnapshot(vin, context)
