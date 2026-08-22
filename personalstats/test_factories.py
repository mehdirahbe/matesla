"""
Fake telemetry builders for personalstats graph tests.

Used only by Django TestCase runs (isolated test database). Never import this
from production capture or TeslaFi import paths.

Why this exists: personal graph endpoints need hundreds/thousands of realistic
rows (drives, charges, climate) to exercise histograms, monthly ribbons, and
the lifetime map. The production db.sqlite3 must never be used for that.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as datetime_timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

# Valid hashedVin characters for IsValidHash (lowercase hex / a-z0-9.)
FAKE_VIN = "5YJ3E1EA0KFTEST0001"
FAKE_HASHED_VIN = "a" * 56


def assert_not_production_database():
    """
    Hard safety: abort if the active connection points at production SQLite.

    Django's test runner swaps NAME to TEST['NAME'] before tests run; this
    catches misconfiguration or accidental settings overrides that would write
    into the real fleet history database.
    """
    from pathlib import Path

    from django.conf import settings
    from django.db import connection

    active_database_path = Path(str(connection.settings_dict["NAME"])).resolve()
    production_database_path = (Path(settings.BASE_DIR) / "db.sqlite3").resolve()
    if active_database_path == production_database_path:
        raise RuntimeError(
            f"Refusing to run tests against production DB: {active_database_path}. "
            "Expected test_matesla.sqlite3 (or another non-production path)."
        )
    active_name_lower = str(active_database_path).lower()
    if "test" not in active_name_lower and ":memory:" not in active_name_lower:
        raise RuntimeError(
            f"Active DB name does not look like a test database: {active_database_path}"
        )


def seed_fake_car_telemetry(
    *,
    hashed_vin: str = FAKE_HASHED_VIN,
    vin: str = FAKE_VIN,
    days: int = 400,
    samples_per_day: int = 8,
    start: datetime | None = None,
) -> int:
    """
    Insert a multi-month fake history suitable for all personal graph types.

    Returns the number of rows created. Uses bulk_create for speed with unique
    (vin, Date) timestamps.

    Mix includes:
      - multi-point drive trips (speed, power incl. regen, GPS, odometer)
      - charge sessions (AC/DC peaks, limits, energy counters)
      - park / climate (seasonal outside temp, hot cabin peaks in summer)
      - battery_degradation for scatter / degradation charts
    """
    if days < 1 or samples_per_day < 1:
        return 0

    start_time = start or datetime(
        2024, 1, 1, 8, 0, 0, tzinfo=datetime_timezone.utc
    )
    odometer_miles = 5000.0
    state_of_charge_percent = 70.0
    charge_energy_added_kwh = 0.0
    pending_rows: list[TeslaCarDataSnapshot] = []
    sample_time = start_time
    rows_created = 0
    sequence_index = 0

    def flush_pending_rows():
        nonlocal pending_rows
        if pending_rows:
            TeslaCarDataSnapshot.objects.bulk_create(pending_rows, batch_size=500)
            pending_rows.clear()

    def append_snapshot(**field_overrides):
        """Append one snapshot at the current sample_time, then bulk when full."""
        nonlocal rows_created, sequence_index, sample_time
        day_of_year = sample_time.timetuple().tm_yday
        # Seasonal outside air: ~5–25 °C sine over the year
        outside_temp_celsius = 15.0 + 10.0 * math.sin(
            2 * math.pi * (day_of_year - 80) / 365.0
        )
        calendar_month = sample_time.month
        hour_of_day = sample_time.hour
        battery_degradation_percent = min(
            12.0, max(0.0, (odometer_miles - 5000.0) / 2000.0)
        )

        field_values = dict(
            vin=vin,
            hashedVin=hashed_vin,
            Date=sample_time,
            DateOnlyDay=sample_time.date(),
            odometer=odometer_miles,
            outside_temp=round(outside_temp_celsius, 2),
            inside_temp=round(outside_temp_celsius + 2.0, 2),
            driver_temp_setting=21.0,
            passenger_temp_setting=21.0,
            battery_degradation=battery_degradation_percent,
            state="online",
            NumberCycles=odometer_miles / 200.0,
            randomNr=float(sequence_index % 1000),
            charge_limit_soc=80.0,
            est_battery_range=0.0,
            ideal_battery_range=0.0,
        )
        field_values.update(field_overrides)

        # Keep rated range consistent with SoC when the caller did not override it
        battery_level = field_values.get("battery_level")
        if battery_level is not None and not field_overrides.get("battery_range"):
            battery_range_miles = 180.0 * (float(battery_level) / 100.0)
            field_values["battery_range"] = battery_range_miles
            field_values["est_battery_range"] = battery_range_miles * 0.95
            field_values["ideal_battery_range"] = battery_range_miles * 1.02
            field_values.setdefault("usable_battery_level", battery_level)

        # Summer cabin heat spikes on park samples (reproduces ~70 °C habitacle)
        if (
            field_values.get("shift_state") == "P"
            and calendar_month in (6, 7, 8)
            and 11 <= hour_of_day <= 16
            and "inside_temp" not in field_overrides
        ):
            field_values["inside_temp"] = round(55.0 + (sequence_index % 20), 2)

        pending_rows.append(TeslaCarDataSnapshot(**field_values))
        rows_created += 1
        sequence_index += 1
        if len(pending_rows) >= 500:
            flush_pending_rows()

    for day_offset in range(days):
        day_start = start_time + timedelta(days=day_offset)

        # --- Morning trip: several samples ≤ 2 min apart ---
        # Close spacing is required for lifetime-map / efficiency trip segmentation.
        # ~3.5 mi × 6 ≈ 21 mi (≥ 20 km) so Drives leaderboard has ranked trips.
        sample_time = day_start.replace(hour=8, minute=0, second=0, microsecond=0)
        trip_sample_count = 6
        for trip_index in range(trip_sample_count):
            odometer_miles += 3.5
            state_of_charge_percent = max(20.0, state_of_charge_percent - 0.9)
            # Positive power for traction; last samples simulate regen braking
            if trip_index < trip_sample_count - 2:
                power_kw = 20.0 + (trip_index * 5)
            else:
                power_kw = -12.0
            # Gentle climb then descent — feeds elev_up / elev_down rankings
            elevation_m = 80.0 + 40.0 * math.sin(
                math.pi * trip_index / max(1, trip_sample_count - 1)
            )
            append_snapshot(
                speed=30.0 + trip_index * 5,
                power=power_kw,
                shift_state="D",
                charging_state="Disconnected",
                charger_power=0.0,
                charge_rate=0.0,
                battery_level=state_of_charge_percent,
                latitude=50.85 + 0.02 * math.sin((day_offset + trip_index) / 11.0),
                longitude=4.35 + 0.02 * math.cos((day_offset + trip_index) / 13.0),
                elevation=round(elevation_m, 1),
            )
            sample_time = sample_time + timedelta(seconds=90)

        # --- Park midday ---
        sample_time = day_start.replace(hour=12, minute=0, second=0, microsecond=0)
        append_snapshot(
            speed=0.0,
            power=0.0,
            shift_state="P",
            charging_state="Disconnected",
            charger_power=0.0,
            charge_rate=0.0,
            battery_level=state_of_charge_percent,
            latitude=50.85,
            longitude=4.35,
        )

        # --- Charge session (3 samples, AC or DC peak) ---
        sample_time = day_start.replace(hour=18, minute=0, second=0, microsecond=0)
        is_direct_current_session = day_offset % 5 == 0
        for charge_sample_index in range(3):
            state_of_charge_percent = min(100.0, state_of_charge_percent + 4.0)
            charge_energy_added_kwh += 1.5
            append_snapshot(
                speed=0.0,
                power=0.0,
                shift_state="P",
                charging_state="Charging",
                charger_power=150.0 if is_direct_current_session else 7.0 + charge_sample_index,
                charge_rate=500.0 if is_direct_current_session else 30.0,
                charge_limit_soc=100.0 if day_offset % 3 == 0 else 80.0,
                charge_energy_added=charge_energy_added_kwh,
                charge_miles_added_rated=charge_energy_added_kwh * 3.5,
                charger_voltage=400.0 if is_direct_current_session else 230.0,
                charger_actual_current=16.0,
                charger_phases=3.0,
                battery_level=state_of_charge_percent,
                latitude=50.85,
                longitude=4.35,
            )
            sample_time = sample_time + timedelta(minutes=10)

        # --- High-SoC parked sample (feeds degradation scatter SoC filter) ---
        sample_time = day_start.replace(hour=21, minute=0, second=0, microsecond=0)
        if state_of_charge_percent < 80:
            state_of_charge_percent = 82.0
        append_snapshot(
            speed=0.0,
            power=0.0,
            shift_state="P",
            charging_state="Complete",
            charger_power=0.0,
            charge_rate=0.0,
            battery_level=state_of_charge_percent,
            latitude=50.85,
            longitude=4.35,
        )

        # Extra evenly spaced park samples for denser climate / odometer series
        extra_samples = max(0, samples_per_day - (trip_sample_count + 1 + 3 + 1))
        for extra_index in range(extra_samples):
            sample_time = day_start.replace(
                hour=min(23, 9 + extra_index),
                minute=30,
                second=0,
                microsecond=0,
            )
            append_snapshot(
                speed=0.0,
                power=0.0,
                shift_state="P",
                charging_state="Disconnected",
                charger_power=0.0,
                charge_rate=0.0,
                battery_level=state_of_charge_percent,
                latitude=50.85,
                longitude=4.35,
            )

    flush_pending_rows()
    return rows_created


def seed_known_empty_vehicle(
    *,
    vin: str = "5YJ3E7EB1KF000077",
    username: str = "emptyvin",
    api_id: str = "77",
    display_name: str = "Empty",
) -> tuple[str, str]:
    """
    Link a TeslaVehicle with no snapshots / firmware / car-info rows.

    Returns (hashed_vin, vin). The hash is known (empty-state 2xx), not 404.
    """
    from django.contrib.auth import get_user_model

    from matesla.models.TeslaToken import TeslaVehicle
    from matesla.models.VinHash import HashTheVin

    hashed_vin = HashTheVin(vin)
    user = get_user_model().objects.create_user(username, password="x")
    TeslaVehicle.objects.create(
        user=user,
        api_id=api_id,
        vin=vin,
        display_name=display_name,
    )
    return hashed_vin, vin
