"""Long parked stretch raises idle poll floor (24h→10 min, 48h→15 min)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from matesla.capture import (
    INTERVAL_LONG_IDLE_HARD_MIN,
    INTERVAL_LONG_IDLE_SOFT_MIN,
    INTERVAL_ONLINE_IDLE_MIN,
    long_idle_poll_floor_minutes,
    poll_interval_minutes,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaToken import TeslaVehicle
from matesla.models.VinHash import HashTheVin


def _daytime(now=None):
    """Force is_night=False so day baseline (5 min) applies."""
    return patch("matesla.capture.is_night", return_value=False)


class LongIdlePollFloorTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("long_idle_u", password="x")
        self.vin = "5YJ3E7EB1KF000099"
        self.vehicle = TeslaVehicle.objects.create(
            user=self.user,
            api_id="99099",
            vin=self.vin,
            display_name="Aram",
            state="asleep",
            is_primary=True,
        )
        self.now = timezone.now()

    def _snap(self, *, hours_ago: float, **fields):
        when = self.now - timedelta(hours=hours_ago)
        defaults = {
            "vin": self.vin,
            "hashedVin": HashTheVin(self.vin),
            "Date": when,
            "DateOnlyDay": when.date(),
            "randomNr": 0.1,
            "charging_state": "Disconnected",
            "shift_state": "P",
            "speed": 0.0,
        }
        defaults.update(fields)
        return TeslaCarDataSnapshot.objects.create(**defaults)

    def test_no_drive_or_charge_history_no_floor(self):
        self._snap(hours_ago=1, shift_state="P", speed=0)
        self.assertIsNone(long_idle_poll_floor_minutes(self.vehicle, now=self.now))

    def test_recent_drive_no_floor(self):
        self._snap(hours_ago=2, shift_state="D", speed=30.0)
        self._snap(hours_ago=0.1, shift_state="P", speed=0)
        self.assertIsNone(long_idle_poll_floor_minutes(self.vehicle, now=self.now))

    def test_soft_floor_after_24h(self):
        self._snap(hours_ago=30, shift_state="D", speed=40.0)
        self._snap(hours_ago=1, shift_state="P", speed=0)
        self.assertEqual(
            long_idle_poll_floor_minutes(self.vehicle, now=self.now),
            INTERVAL_LONG_IDLE_SOFT_MIN,
        )

    def test_hard_floor_after_48h(self):
        self._snap(hours_ago=60, charging_state="Charging", shift_state="P")
        self._snap(hours_ago=1, charging_state="Disconnected", shift_state="P")
        self.assertEqual(
            long_idle_poll_floor_minutes(self.vehicle, now=self.now),
            INTERVAL_LONG_IDLE_HARD_MIN,
        )

    def test_poll_interval_applies_floor_over_busy_habit(self):
        self._snap(hours_ago=72, shift_state="D", speed=50.0)
        self._snap(hours_ago=0.5, shift_state="P", speed=0)
        # Fresh idle snap + asleep list → not LIVE; habit may say 5.
        with _daytime():
            with patch(
                "matesla.poll_habits.get_habit_model"
            ) as mock_model:
                model = mock_model.return_value
                model.suggested_idle_interval_minutes.return_value = 5
                minutes = poll_interval_minutes(
                    self.vehicle, now=self.now
                )
        self.assertEqual(minutes, INTERVAL_LONG_IDLE_HARD_MIN)
        self.assertGreater(minutes, INTERVAL_ONLINE_IDLE_MIN)

    def test_live_charge_ignores_long_idle_floor(self):
        # Last charge long ago would set floor, but *fresh* charging wins.
        self._snap(hours_ago=100, shift_state="D", speed=40.0)
        self._snap(
            hours_ago=0.05,
            charging_state="Charging",
            charger_power=7.0,
            shift_state="P",
            speed=0,
        )
        self.vehicle.state = "online"
        self.vehicle.save(update_fields=["state"])
        with _daytime():
            minutes = poll_interval_minutes(self.vehicle, now=self.now)
        # AC charge day baseline is 15 — live path, not long-idle-only
        self.assertEqual(minutes, 15)

    def test_idle_forecast_shows_long_idle_floor(self):
        from matesla.poll_diagnostics import build_idle_forecast
        from matesla.poll_habits import HabitModel

        self._snap(hours_ago=72, shift_state="D", speed=40.0)
        model = HabitModel(
            vin=self.vin,
            trusted=False,
            reason="test",
            weeks_in_window=0,
            computed_at=self.now,
        )
        local = self.now.astimezone()
        days = build_idle_forecast(
            model, now_local=local, days=1, idle_hours_so_far=72.0
        )
        self.assertTrue(days)
        day_minutes = {seg.idle_interval_minutes for seg in days[0]}
        # Day baseline 5 is raised to 15; night 30 stays 30.
        self.assertIn(INTERVAL_LONG_IDLE_HARD_MIN, day_minutes)
        self.assertNotIn(INTERVAL_ONLINE_IDLE_MIN, day_minutes)
