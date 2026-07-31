"""
Fake telemetry builders for personalstats graph tests.

Used only by Django TestCase runs (test DB). Never import this from
production capture/import paths.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

# Valid hashedVin chars for IsValidHash (a-z0-9.)
FAKE_VIN = "5YJ3E1EA0KFTEST0001"
FAKE_HASHED_VIN = "a" * 56


def assert_not_production_database():
    """
    Hard safety: abort if the active connection points at production SQLite.

    Django's test runner swaps NAME to TEST['NAME'] before tests run; this
    catches misconfiguration / accidental settings overrides.
    """
    from pathlib import Path

    from django.conf import settings
    from django.db import connection

    active = Path(str(connection.settings_dict["NAME"])).resolve()
    prod = (Path(settings.BASE_DIR) / "db.sqlite3").resolve()
    if active == prod:
        raise RuntimeError(
            f"Refusing to run tests against production DB: {active}. "
            "Expected test_matesla.sqlite3 (or another non-prod path)."
        )
    name_l = str(active).lower()
    if "test" not in name_l and ":memory:" not in name_l:
        raise RuntimeError(
            f"Active DB name does not look like a test database: {active}"
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

    Returns the number of rows created. Uses bulk_create (fast) with unique
    (vin, Date) timestamps.

    Mix includes:
      - drive samples (speed, power, GPS, odometer progression)
      - charge sessions (limit, power, rate, energy)
      - park / climate (seasonal outside_temp, hot cabin peaks in summer)
      - battery_degradation + range fields for scatter/degradation charts
    """
    if days < 1 or samples_per_day < 1:
        return 0

    start = start or datetime(2024, 1, 1, 8, 0, 0, tzinfo=dt_timezone.utc)
    odo = 5000.0  # miles
    soc = 70.0
    charge_energy = 0.0
    rows: list[TeslaCarDataSnapshot] = []
    t = start
    n_created = 0
    seq = 0

    def _flush():
        nonlocal rows
        if rows:
            TeslaCarDataSnapshot.objects.bulk_create(rows, batch_size=500)
            rows.clear()

    def _add(**kwargs):
        nonlocal n_created, seq, t
        day_of_year = t.timetuple().tm_yday
        outside = 15.0 + 10.0 * math.sin(2 * math.pi * (day_of_year - 80) / 365.0)
        month = t.month
        hour = t.hour
        degradation = min(12.0, max(0.0, (odo - 5000.0) / 2000.0))

        defaults = dict(
            vin=vin,
            hashedVin=hashed_vin,
            Date=t,
            DateOnlyDay=t.date(),
            odometer=odo,
            outside_temp=round(outside, 2),
            inside_temp=round(outside + 2.0, 2),
            driver_temp_setting=21.0,
            passenger_temp_setting=21.0,
            battery_degradation=degradation,
            state="online",
            NumberCycles=odo / 200.0,
            randomNr=float(seq % 1000),
            charge_limit_soc=80.0,
            est_battery_range=0.0,
            ideal_battery_range=0.0,
        )
        defaults.update(kwargs)
        # Keep range consistent with SoC when not overridden
        bl = defaults.get("battery_level")
        if bl is not None and not kwargs.get("battery_range"):
            br = 180.0 * (float(bl) / 100.0)
            defaults["battery_range"] = br
            defaults["est_battery_range"] = br * 0.95
            defaults["ideal_battery_range"] = br * 1.02
            defaults.setdefault("usable_battery_level", bl)
        # Summer cabin heat on park samples
        if defaults.get("shift_state") == "P" and month in (6, 7, 8) and 11 <= hour <= 16:
            if "inside_temp" not in kwargs:
                defaults["inside_temp"] = round(55.0 + (seq % 20), 2)

        rows.append(TeslaCarDataSnapshot(**defaults))
        n_created += 1
        seq += 1
        if len(rows) >= 500:
            _flush()

    for day in range(days):
        day_start = start + timedelta(days=day)
        # --- Morning trip: several samples ≤ 2 min apart (lifetime/efficiency) ---
        t = day_start.replace(hour=8, minute=0, second=0, microsecond=0)
        trip_points = 6
        for j in range(trip_points):
            odo += 2.2  # ~13 mi trip
            soc = max(20.0, soc - 0.6)
            power = 20.0 + (j * 5) if j < trip_points - 2 else -12.0  # regen near end
            _add(
                speed=30.0 + j * 5,
                power=power,
                shift_state="D",
                charging_state="Disconnected",
                charger_power=0.0,
                charge_rate=0.0,
                battery_level=soc,
                latitude=50.85 + 0.02 * math.sin((day + j) / 11.0),
                longitude=4.35 + 0.02 * math.cos((day + j) / 13.0),
            )
            t = t + timedelta(seconds=90)

        # --- Park midday ---
        t = day_start.replace(hour=12, minute=0, second=0, microsecond=0)
        _add(
            speed=0.0,
            power=0.0,
            shift_state="P",
            charging_state="Disconnected",
            charger_power=0.0,
            charge_rate=0.0,
            battery_level=soc,
            latitude=50.85,
            longitude=4.35,
        )

        # --- Charge session (3 samples) ---
        t = day_start.replace(hour=18, minute=0, second=0, microsecond=0)
        dc = day % 5 == 0
        for j in range(3):
            soc = min(100.0, soc + 4.0)
            charge_energy += 1.5
            _add(
                speed=0.0,
                power=0.0,
                shift_state="P",
                charging_state="Charging",
                charger_power=150.0 if dc else 7.0 + j,
                charge_rate=500.0 if dc else 30.0,
                charge_limit_soc=100.0 if day % 3 == 0 else 80.0,
                charge_energy_added=charge_energy,
                charge_miles_added_rated=charge_energy * 3.5,
                charger_voltage=400.0 if dc else 230.0,
                charger_actual_current=16.0,
                charger_phases=3.0,
                battery_level=soc,
                latitude=50.85,
                longitude=4.35,
            )
            t = t + timedelta(minutes=10)

        # --- High-SoC parked sample (degradation scatter filter) ---
        t = day_start.replace(hour=21, minute=0, second=0, microsecond=0)
        if soc < 80:
            soc = 82.0
        _add(
            speed=0.0,
            power=0.0,
            shift_state="P",
            charging_state="Complete",
            charger_power=0.0,
            charge_rate=0.0,
            battery_level=soc,
            latitude=50.85,
            longitude=4.35,
        )

        # Extra evenly spaced samples for dense climate/odometer series
        extra = max(0, samples_per_day - (trip_points + 1 + 3 + 1))
        for j in range(extra):
            t = day_start.replace(hour=min(23, 9 + j), minute=30, second=0, microsecond=0)
            _add(
                speed=0.0,
                power=0.0,
                shift_state="P",
                charging_state="Disconnected",
                charger_power=0.0,
                charge_rate=0.0,
                battery_level=soc,
                latitude=50.85,
                longitude=4.35,
            )

    _flush()
    return n_created
