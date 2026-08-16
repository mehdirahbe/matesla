"""
Single-flight first-paint data for the Stats grid.

The six default thumbs hit the same VIN/period at once. One builder thread
runs the SQL series sequentially and publishes each field as soon as it is
ready so PNG encode can overlap the remaining queries.

Series come from the same helpers as the solo paths (same values).
"""

from __future__ import annotations

import threading
import time

from django.utils.translation import get_language

_TTL_SECONDS = 45.0
_LOCK = threading.Lock()
_BUILDS: dict = {}

FIELDS = (
    "degrad_odometer",
    "efficiency_by_speed",
    "daily_odo_temp",
    "charger_power",
    "fleet_poll_cost",
)


class _Build:
    def __init__(self, hashed_vin, period, unit):
        self.hashed_vin = hashed_vin
        self.period = period
        self.unit = unit
        self.values = {}
        self.events = {name: threading.Event() for name in FIELDS}
        # derived views
        self.events["odometer"] = self.events["daily_odo_temp"]
        self.events["outside_temp"] = self.events["daily_odo_temp"]
        self.error = None
        self.expires_at = None  # None = in flight

    def publish(self, name, value):
        self.values[name] = value
        self.events[name].set()

    def get(self, name):
        if not self.events[name].wait(timeout=180):
            raise TimeoutError(f"stats bundle field {name} timed out")
        if self.error is not None:
            raise self.error
        if name == "odometer":
            return self.values["daily_odo_temp"][0]
        if name == "outside_temp":
            return self.values["daily_odo_temp"][1]
        return self.values[name]


def _key(hashed_vin, period, unit):
    return (hashed_vin, int(period or 0), unit or "km", get_language() or "en")


def get_stats_first_paint_field(hashed_vin, period, unit, name):
    """Wait for one first-paint series (starts the shared builder if needed)."""
    return _get_build(hashed_vin, period, unit).get(name)


def get_stats_first_paint_bundle(hashed_vin, period, unit):
    """Full dict (tests / callers that want every series)."""
    build = _get_build(hashed_vin, period, unit)
    return {
        "degrad_odometer": build.get("degrad_odometer"),
        "charger_power": build.get("charger_power"),
        "efficiency_by_speed": build.get("efficiency_by_speed"),
        "outside_temp": build.get("outside_temp"),
        "odometer": build.get("odometer"),
        "fleet_poll_cost": build.get("fleet_poll_cost"),
    }


def _get_build(hashed_vin, period, unit) -> _Build:
    """Reuse an in-flight/fresh build, or become the leader and compute here."""
    cache_key = _key(hashed_vin, period, unit)
    leader = False
    with _LOCK:
        build = _BUILDS.get(cache_key)
        if build is not None and (
            build.expires_at is None or build.expires_at > time.monotonic()
        ):
            pass
        else:
            build = _Build(hashed_vin, period, unit)
            _BUILDS[cache_key] = build
            leader = True
    if leader:
        _run_build(cache_key, build)
    return build


def _run_build(cache_key, build: _Build):
    try:
        # Run all SQL before publishing so encode does not fight the scans.
        _compute_in_order(build)
        build.expires_at = time.monotonic() + _TTL_SECONDS
    except Exception as exc:
        build.error = exc
        for event in build.events.values():
            event.set()
        with _LOCK:
            if _BUILDS.get(cache_key) is build:
                _BUILDS.pop(cache_key, None)


def _compute_in_order(build: _Build):
    """
    Publish slowest-first so encode of early fields overlaps later SQL.

    Returns a dict of the raw published names (including daily_odo_temp).
    """
    from matesla.degradation_graphs import load_degradation_scatter_xy
    from personalstats import views as v

    hashed_vin, period, unit = build.hashed_vin, build.period, build.unit
    days = v._fleet_poll_window_days(period)
    fleet = v._fleet_poll_buckets(hashed_vin, days=days)
    charge = v._charge_peak_histogram(hashed_vin, period, metric="charger_power")
    odo, temp = v._daily_odometer_and_monthly_outside_temp(hashed_vin, period)
    efficiency = v._efficiency_bins_for_car(
        hashed_vin, period, by_speed=True, unit=unit
    )
    degrad = load_degradation_scatter_xy(
        hashed_vin, "odometer", period, y_mode="battery_degradation"
    )
    # Publish only after every query so PNG encode does not contend with SQL.
    build.publish("fleet_poll_cost", fleet)
    build.publish("charger_power", charge)
    build.publish("daily_odo_temp", (odo, temp))
    build.publish("efficiency_by_speed", efficiency)
    build.publish("degrad_odometer", degrad)
    return {
        "fleet_poll_cost": fleet,
        "charger_power": charge,
        "daily_odo_temp": (odo, temp),
        "efficiency_by_speed": efficiency,
        "degrad_odometer": degrad,
    }


def _build_stats_first_paint_bundle(hashed_vin, period, unit):
    """Synchronous build for tests (no thread / cache)."""
    build = _Build(hashed_vin, period, unit)
    _compute_in_order(build)
    return {
        "degrad_odometer": build.values["degrad_odometer"],
        "charger_power": build.values["charger_power"],
        "efficiency_by_speed": build.values["efficiency_by_speed"],
        "outside_temp": build.values["daily_odo_temp"][1],
        "odometer": build.values["daily_odo_temp"][0],
        "fleet_poll_cost": build.values["fleet_poll_cost"],
    }
