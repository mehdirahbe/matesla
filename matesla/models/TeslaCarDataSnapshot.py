from datetime import timezone as dt_timezone
from random import random

from django.db import models
from django.db.models import Q
from django.utils import timezone

from matesla.BatteryDegradation import (
    ComputeBatteryDegradationFromEPARange,
    GetEPARangeFromCache,
    ComputeNumCycles,
)
from matesla.models.VinHash import HashTheVin


def parse_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_bool(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("0", "false", "no", "off", "none"):
            return False
        if text in ("1", "true", "yes", "on"):
            return True
    return bool(value)


def parse_str(value, default=None):
    if value is None or value == "":
        return default
    text = str(value).strip()
    if text in ("<invalid>", "None", "null"):
        return default
    return text


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
    #Mehdi 2/8/2026: tesla does not return a bool but a more detailed info.
    #"climate_keeper_mode": "camp","climate_keeper_mode": "dog"
    #"climate_keeper_mode": "off" which can be combined with "is_climate_on": true during preconditioning
    climate_keeper_mode = models.BooleanField(null=True, blank=True)
    #so let's add a raw data field with real value
    climate_keeper_modeRaw = models.TextField(null=True, blank=True)
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
    # Navigation destination (present only while a route is active in the car).
    # Used to stretch drive polling far from arrival; null when no nav / unknown.
    active_route_minutes_to_arrival = models.FloatField(null=True, blank=True)
    active_route_miles_to_arrival = models.FloatField(null=True, blank=True)
    active_route_destination = models.TextField(null=True, blank=True)

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
            # Personal stats period graphs + efficiency drive filter
            models.Index(fields=["hashedVin", "Date"]),
            models.Index(fields=["hashedVin", "DateOnlyDay"]),
            # Drive-only leaderboard / efficiency / lifetime map: ~¼ of rows.
            # Writes are sparse (Fleet poll ≤ every 2 min) so index cost is fine.
            models.Index(
                fields=["hashedVin", "Date"],
                name="matesla_snapshot_drive_hv_date",
                condition=Q(shift_state__in=["D", "R", "N"]) | Q(speed__gt=1),
            ),
            # Geo elevation backfill / lat-lon range lookups (Open-Meteo DEM).
            models.Index(
                fields=["latitude", "longitude"],
                name="matesla_snap_lat_lon_idx",
            ),
            # TeslaFi charge_number session histograms (charger_power, etc.).
            models.Index(
                fields=["hashedVin", "charge_number"],
                name="matesla_snap_charge_sess_idx",
                condition=Q(charging_state__in=["Charging", "Starting"])
                & Q(charge_number__isnull=False),
            ),
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

        self.display_name = parse_str(context.get("display_name"))
        self.state = parse_str(context.get("state"))

        self.battery_level = parse_float(charge_state.get("battery_level"))
        self.battery_range = parse_float(charge_state.get("battery_range"))
        self.charge_limit_soc = parse_float(charge_state.get("charge_limit_soc"))
        self.charge_rate = parse_float(charge_state.get("charge_rate"))
        self.charger_actual_current = parse_float(charge_state.get("charger_actual_current"))
        self.charger_phases = parse_float(charge_state.get("charger_phases"))
        self.charger_power = parse_float(charge_state.get("charger_power"))
        self.charger_voltage = parse_float(charge_state.get("charger_voltage"))
        self.charging_state = parse_str(charge_state.get("charging_state"), "Unknown")
        self.est_battery_range = parse_float(
            charge_state.get("est_battery_range") or charge_state.get("battery_range")
        )
        self.ideal_battery_range = parse_float(charge_state.get("ideal_battery_range"))
        self.fast_charger_brand = parse_str(charge_state.get("fast_charger_brand"))
        self.fast_charger_present = parse_bool(charge_state.get("fast_charger_present"))
        self.fast_charger_type = parse_str(charge_state.get("fast_charger_type"))
        self.max_range_charge_counter = parse_float(charge_state.get("max_range_charge_counter"))
        self.usable_battery_level = parse_float(
            charge_state.get("usable_battery_level"), self.battery_level
        )
        self.charge_energy_added = parse_float(charge_state.get("charge_energy_added"))
        self.charge_miles_added_rated = parse_float(charge_state.get("charge_miles_added_rated"))
        self.charge_miles_added_ideal = parse_float(charge_state.get("charge_miles_added_ideal"))
        self.time_to_full_charge = parse_float(charge_state.get("time_to_full_charge"))
        self.charge_current_request = parse_float(charge_state.get("charge_current_request"))
        self.charge_current_request_max = parse_float(
            charge_state.get("charge_current_request_max")
        )
        self.charge_port_door_open = parse_bool(charge_state.get("charge_port_door_open"))
        self.battery_heater_on = parse_bool(charge_state.get("battery_heater_on"))
        self.battery_current = parse_float(charge_state.get("battery_current"))
        self.energy_remaining = parse_float(charge_state.get("energy_remaining"))
        self.pack_voltage = parse_float(charge_state.get("pack_voltage"))

        self.climate_keeper_modeRaw = climate_state.get("climate_keeper_mode")
        if self.climate_keeper_modeRaw is None:
            self.climate_keeper_mode = None
        else:
            self.climate_keeper_mode = (
                bool(self.climate_keeper_modeRaw) and self.climate_keeper_modeRaw != "off"
            )
        self.driver_temp_setting = parse_float(climate_state.get("driver_temp_setting"))
        self.inside_temp = parse_float(climate_state.get("inside_temp"))
        self.is_climate_on = parse_bool(climate_state.get("is_climate_on"))
        self.outside_temp = parse_float(climate_state.get("outside_temp"))
        self.passenger_temp_setting = parse_float(climate_state.get("passenger_temp_setting"))
        self.is_auto_conditioning_on = parse_bool(climate_state.get("is_auto_conditioning_on"))
        self.fan_status = parse_float(climate_state.get("fan_status"))
        self.is_front_defroster_on = parse_bool(climate_state.get("is_front_defroster_on"))
        self.is_rear_defroster_on = parse_bool(climate_state.get("is_rear_defroster_on"))

        self.latitude = parse_float(drive_state.get("latitude"))
        self.longitude = parse_float(drive_state.get("longitude"))
        self.power = parse_float(drive_state.get("power"))
        self.speed = parse_float(drive_state.get("speed"))
        self.heading = parse_float(drive_state.get("heading"))
        self.shift_state = parse_str(drive_state.get("shift_state"))
        self.gps_as_of = parse_int(drive_state.get("gps_as_of"))
        self.elevation = parse_float(drive_state.get("elevation") or context.get("elevation"))
        # Fleet may omit these when no navigation destination is set.
        self.active_route_minutes_to_arrival = parse_float(
            drive_state.get("active_route_minutes_to_arrival")
        )
        self.active_route_miles_to_arrival = parse_float(
            drive_state.get("active_route_miles_to_arrival")
        )
        self.active_route_destination = parse_str(
            drive_state.get("active_route_destination")
        )

        self.odometer = parse_float(vehicle_state.get("odometer"))
        self.locked = parse_bool(vehicle_state.get("locked"))
        self.car_version = parse_str(vehicle_state.get("car_version"))
        self.is_user_present = parse_bool(vehicle_state.get("is_user_present"))
        self.sentry_mode = parse_bool(vehicle_state.get("sentry_mode"))

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

        self.display_name = parse_str(row.get("display_name") or row.get("vehicle_name"))
        self.state = parse_str(row.get("state"))

        self.battery_level = parse_float(row.get("battery_level"))
        self.battery_range = parse_float(row.get("battery_range"))
        self.charge_limit_soc = parse_float(row.get("charge_limit_soc"))
        self.charge_rate = parse_float(row.get("charge_rate"))
        self.charger_actual_current = parse_float(row.get("charger_actual_current"))
        self.charger_phases = parse_float(row.get("charger_phases"))
        self.charger_power = parse_float(row.get("charger_power"))
        self.charger_voltage = parse_float(row.get("charger_voltage"))
        self.charging_state = parse_str(row.get("charging_state"), "Unknown")
        self.est_battery_range = parse_float(
            row.get("est_battery_range") or row.get("battery_range")
        )
        self.ideal_battery_range = parse_float(row.get("ideal_battery_range"))
        self.fast_charger_brand = parse_str(row.get("fast_charger_brand"))
        self.fast_charger_present = parse_bool(row.get("fast_charger_present"))
        self.fast_charger_type = parse_str(row.get("fast_charger_type"))
        self.max_range_charge_counter = parse_float(row.get("max_range_charge_counter"))
        self.usable_battery_level = parse_float(
            row.get("usable_battery_level"), self.battery_level
        )
        self.charge_energy_added = parse_float(row.get("charge_energy_added"))
        self.charge_miles_added_rated = parse_float(row.get("charge_miles_added_rated"))
        self.charge_miles_added_ideal = parse_float(row.get("charge_miles_added_ideal"))
        self.time_to_full_charge = parse_float(row.get("time_to_full_charge"))
        self.charge_current_request = parse_float(row.get("charge_current_request"))
        self.charge_current_request_max = parse_float(row.get("charge_current_request_max"))
        self.charge_port_door_open = parse_bool(row.get("charge_port_door_open"))
        self.battery_heater_on = parse_bool(row.get("battery_heater_on"))
        self.battery_current = parse_float(row.get("battery_current"))
        self.energy_remaining = parse_float(row.get("energy_remaining"))
        self.pack_voltage = parse_float(row.get("pack_voltage"))

        self.climate_keeper_mode = parse_bool(row.get("climate_keeper_mode"))
        self.driver_temp_setting = parse_float(row.get("driver_temp_setting"))
        self.inside_temp = parse_float(row.get("inside_temp"))
        self.is_climate_on = parse_bool(row.get("is_climate_on"))
        self.outside_temp = parse_float(row.get("outside_temp"))
        self.passenger_temp_setting = parse_float(row.get("passenger_temp_setting"))
        self.is_auto_conditioning_on = parse_bool(row.get("is_auto_conditioning_on"))
        self.fan_status = parse_float(row.get("fan_status"))
        self.is_front_defroster_on = parse_bool(row.get("is_front_defroster_on"))
        self.is_rear_defroster_on = parse_bool(row.get("is_rear_defroster_on"))

        self.latitude = parse_float(row.get("latitude"))
        self.longitude = parse_float(row.get("longitude"))
        self.power = parse_float(row.get("power"))
        self.speed = parse_float(row.get("speed"))
        self.heading = parse_float(row.get("heading"))
        self.shift_state = parse_str(row.get("shift_state"))
        self.gps_as_of = parse_int(row.get("gps_as_of"))
        self.elevation = parse_float(row.get("elevation"))

        self.odometer = parse_float(row.get("odometer"))
        self.locked = parse_bool(row.get("locked"))
        self.car_version = parse_str(row.get("car_version"))
        self.is_user_present = parse_bool(row.get("is_user_present"))
        self.sentry_mode = parse_bool(row.get("sentry_mode"))

        self.idle_number = parse_int(row.get("idleNumber") or row.get("idle_number"))
        self.sleep_number = parse_int(row.get("sleepNumber") or row.get("sleep_number"))
        self.drive_number = parse_int(row.get("driveNumber") or row.get("drive_number"))
        self.charge_number = parse_int(row.get("chargeNumber") or row.get("charge_number"))

        self._recompute_derived()
        return self

    def merge_from_flat_row(self, row):
        """Complete empty fields on an existing row from TeslaFi data (nearest-minute merge)."""
        field_map = {
            "display_name": lambda: parse_str(row.get("display_name") or row.get("vehicle_name")),
            "state": lambda: parse_str(row.get("state")),
            "battery_level": lambda: parse_float(row.get("battery_level")),
            "battery_range": lambda: parse_float(row.get("battery_range")),
            "charge_limit_soc": lambda: parse_float(row.get("charge_limit_soc")),
            "charge_rate": lambda: parse_float(row.get("charge_rate")),
            "charger_actual_current": lambda: parse_float(row.get("charger_actual_current")),
            "charger_phases": lambda: parse_float(row.get("charger_phases")),
            "charger_power": lambda: parse_float(row.get("charger_power")),
            "charger_voltage": lambda: parse_float(row.get("charger_voltage")),
            "charging_state": lambda: parse_str(row.get("charging_state")),
            "est_battery_range": lambda: parse_float(row.get("est_battery_range")),
            "ideal_battery_range": lambda: parse_float(row.get("ideal_battery_range")),
            "fast_charger_brand": lambda: parse_str(row.get("fast_charger_brand")),
            "fast_charger_present": lambda: parse_bool(row.get("fast_charger_present")),
            "fast_charger_type": lambda: parse_str(row.get("fast_charger_type")),
            "max_range_charge_counter": lambda: parse_float(row.get("max_range_charge_counter")),
            "usable_battery_level": lambda: parse_float(row.get("usable_battery_level")),
            "charge_energy_added": lambda: parse_float(row.get("charge_energy_added")),
            "charge_miles_added_rated": lambda: parse_float(row.get("charge_miles_added_rated")),
            "charge_miles_added_ideal": lambda: parse_float(row.get("charge_miles_added_ideal")),
            "time_to_full_charge": lambda: parse_float(row.get("time_to_full_charge")),
            "charge_current_request": lambda: parse_float(row.get("charge_current_request")),
            "charge_current_request_max": lambda: parse_float(row.get("charge_current_request_max")),
            "charge_port_door_open": lambda: parse_bool(row.get("charge_port_door_open")),
            "battery_heater_on": lambda: parse_bool(row.get("battery_heater_on")),
            "battery_current": lambda: parse_float(row.get("battery_current")),
            "energy_remaining": lambda: parse_float(row.get("energy_remaining")),
            "pack_voltage": lambda: parse_float(row.get("pack_voltage")),
            "driver_temp_setting": lambda: parse_float(row.get("driver_temp_setting")),
            "inside_temp": lambda: parse_float(row.get("inside_temp")),
            "is_climate_on": lambda: parse_bool(row.get("is_climate_on")),
            "outside_temp": lambda: parse_float(row.get("outside_temp")),
            "passenger_temp_setting": lambda: parse_float(row.get("passenger_temp_setting")),
            "is_auto_conditioning_on": lambda: parse_bool(row.get("is_auto_conditioning_on")),
            "fan_status": lambda: parse_float(row.get("fan_status")),
            "is_front_defroster_on": lambda: parse_bool(row.get("is_front_defroster_on")),
            "is_rear_defroster_on": lambda: parse_bool(row.get("is_rear_defroster_on")),
            "latitude": lambda: parse_float(row.get("latitude")),
            "longitude": lambda: parse_float(row.get("longitude")),
            "power": lambda: parse_float(row.get("power")),
            "speed": lambda: parse_float(row.get("speed")),
            "heading": lambda: parse_float(row.get("heading")),
            "shift_state": lambda: parse_str(row.get("shift_state")),
            "gps_as_of": lambda: parse_int(row.get("gps_as_of")),
            "elevation": lambda: parse_float(row.get("elevation")),
            "odometer": lambda: parse_float(row.get("odometer")),
            "locked": lambda: parse_bool(row.get("locked")),
            "car_version": lambda: parse_str(row.get("car_version")),
            "is_user_present": lambda: parse_bool(row.get("is_user_present")),
            "sentry_mode": lambda: parse_bool(row.get("sentry_mode")),
            "idle_number": lambda: parse_int(row.get("idleNumber") or row.get("idle_number")),
            "sleep_number": lambda: parse_int(row.get("sleepNumber") or row.get("sleep_number")),
            "drive_number": lambda: parse_int(row.get("driveNumber") or row.get("drive_number")),
            "charge_number": lambda: parse_int(row.get("chargeNumber") or row.get("charge_number")),
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
        # Fleet rarely sends altitude; reuse DEM/TeslaFi grid cache when present.
        if self.elevation is None and self.latitude is not None and self.longitude is not None:
            try:
                from matesla.geo_enrich import apply_cached_elevation_to_snapshot

                apply_cached_elevation_to_snapshot(self)
            except Exception:
                pass
        self.save()
        return self

    def SaveIfDontExistsYet(self, vin, context):
        return self.SaveSnapshot(vin, context)
