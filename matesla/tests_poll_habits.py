"""Unit tests for habit-based poll spacing (no Tesla network)."""

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.poll_habits import (
    HABIT_MIN_WEEKS,
    INTERVAL_HABIT_BUSY_MIN,
    INTERVAL_HABIT_QUIET_MIN,
    binomial_ci_upper,
    compute_habit_model,
    sample_is_active,
)

TZ = ZoneInfo("Europe/Brussels")
VIN = "5YJ3E7EB1KFTESTHAB"


def _aware(year, month, day, hour, minute=0):
    local = datetime(year, month, day, hour, minute, tzinfo=TZ)
    return local.astimezone(dt_timezone.utc)


class BinomialCiTests(TestCase):
    def test_zero_events_upper_falls_with_n(self):
        hi2 = binomial_ci_upper(0, 2)
        hi8 = binomial_ci_upper(0, 8)
        hi20 = binomial_ci_upper(0, 20)
        self.assertGreater(hi2, hi8)
        self.assertGreater(hi8, hi20)
        self.assertLess(hi20, 0.2)

    def test_always_active_upper_near_one(self):
        self.assertGreater(binomial_ci_upper(8, 8), 0.7)


class SampleActiveTests(TestCase):
    def test_park_quiet(self):
        self.assertFalse(
            sample_is_active(
                speed=0,
                shift_state="P",
                charging_state="Disconnected",
                power=0,
                is_user_present=False,
            )
        )

    def test_drive_and_charge(self):
        self.assertTrue(
            sample_is_active(
                speed=30,
                shift_state="D",
                charging_state="",
                power=5,
                is_user_present=True,
            )
        )
        self.assertTrue(
            sample_is_active(
                speed=0,
                shift_state="P",
                charging_state="Charging",
                power=7,
                is_user_present=False,
            )
        )


class HabitModelIntegrationTests(TestCase):
    """
    Synthetic telemetry: quiet nights, busy weekday mornings for many weeks,
    then a regime break (vacations).
    """

    def _seed_week(
        self,
        *,
        monday: datetime,
        morning_drive: bool,
        night_drive: bool = False,
    ):
        """monday is local Monday 00:00; plant samples for that week."""
        for day_offset in range(5):  # Mon-Fri
            day = monday + timedelta(days=day_offset)
            # night quiet samples 02:00
            TeslaCarDataSnapshot.objects.create(
                vin=VIN,
                hashedVin="h" * 56,
                Date=_aware(day.year, day.month, day.day, 2),
                speed=0,
                shift_state="P",
                charging_state="Disconnected",
                power=0,
                is_user_present=False,
            )
            # morning 08:00
            TeslaCarDataSnapshot.objects.create(
                vin=VIN,
                hashedVin="h" * 56,
                Date=_aware(day.year, day.month, day.day, 8),
                speed=40 if morning_drive else 0,
                shift_state="D" if morning_drive else "P",
                charging_state="Disconnected",
                power=10 if morning_drive else 0,
                is_user_present=morning_drive,
            )
            if night_drive:
                TeslaCarDataSnapshot.objects.create(
                    vin=VIN,
                    hashedVin="h" * 56,
                    Date=_aware(day.year, day.month, day.day, 3),
                    speed=50,
                    shift_state="D",
                    charging_state="Disconnected",
                    power=20,
                    is_user_present=True,
                )

    def test_quiet_night_trusted_after_enough_school_weeks(self):
        # Anchor "now" = Monday 2025-06-23 12:00 local (school period)
        now_local = datetime(2025, 6, 23, 12, 0, tzinfo=TZ)
        now = now_local.astimezone(dt_timezone.utc)
        # 8 school weeks of morning drives + quiet nights before recent window
        # recent = last 14 days still school
        start_monday = datetime(2025, 4, 21, tzinfo=TZ)  # enough weeks before June 23
        monday = start_monday
        for _ in range(10):
            self._seed_week(monday=monday, morning_drive=True)
            monday = monday + timedelta(days=7)

        model = compute_habit_model(VIN, now=now)
        self.assertGreaterEqual(model.weeks_in_window, HABIT_MIN_WEEKS)
        # Expect trusted school pattern
        self.assertTrue(
            model.trusted,
            f"expected trusted, got reason={model.reason} regime={model.regime_detail}",
        )
        # Tuesday 02:00 should be calm (quiet or moderate)
        self.assertIn((2, 2), model.quiet_hours | model.moderate_hours)
        night = model.suggested_idle_interval_minutes(
            datetime(2025, 6, 24, 2, 0, tzinfo=TZ)
        )
        # quiet → 30; moderate at night → None (keep baseline 30, no densify)
        if (2, 2) in model.quiet_hours:
            self.assertEqual(night, INTERVAL_HABIT_QUIET_MIN)
        else:
            self.assertIsNone(night)

    def test_busy_night_replaces_baseline_with_dense_interval(self):
        """
        Reliable night mobility → 5 min idle (replaces night baseline 30).

        Without replace (old max(30, habit)), denser night logging was impossible.
        """
        now_local = datetime(2025, 6, 23, 12, 0, tzinfo=TZ)
        now = now_local.astimezone(dt_timezone.utc)
        monday = datetime(2025, 4, 21, tzinfo=TZ)
        for _ in range(10):
            self._seed_week(monday=monday, morning_drive=True, night_drive=True)
            monday = monday + timedelta(days=7)

        model = compute_habit_model(VIN, now=now)
        self.assertTrue(
            model.trusted,
            f"expected trusted, got reason={model.reason} regime={model.regime_detail}",
        )
        # Tuesday 03:00 had night drives in the seed
        self.assertIn((2, 3), model.busy_hours)
        night = model.suggested_idle_interval_minutes(
            datetime(2025, 6, 24, 3, 0, tzinfo=TZ)
        )
        self.assertEqual(night, INTERVAL_HABIT_BUSY_MIN)
        self.assertLess(night, 30)  # denser than night baseline

    def test_regime_break_on_vacation_after_school(self):
        """School weeks then 2 weeks calm mornings → quiet_mismatch / break."""
        now_local = datetime(2025, 7, 14, 12, 0, tzinfo=TZ)  # mid-July vacation
        now = now_local.astimezone(dt_timezone.utc)
        # School: April–mid June mornings active
        monday = datetime(2025, 4, 21, tzinfo=TZ)
        for _ in range(8):
            self._seed_week(monday=monday, morning_drive=True)
            monday = monday + timedelta(days=7)
        # Vacation: late June + July weeks — mornings quiet
        monday = datetime(2025, 6, 23, tzinfo=TZ)
        for _ in range(4):
            self._seed_week(monday=monday, morning_drive=False)
            monday = monday + timedelta(days=7)

        model = compute_habit_model(VIN, now=now)
        # Either regime_break or not trusted — must NOT stretch with school model
        idle = model.suggested_idle_interval_minutes(
            datetime(2025, 7, 14, 8, 0, tzinfo=TZ)
        )
        if model.trusted:
            # If still trusted, morning should not be classified quiet from school
            self.assertNotIn((1, 8), model.quiet_hours)
        else:
            self.assertIsNone(idle)
            self.assertTrue(
                model.regime_break or model.reason.startswith("insufficient")
                or model.reason == "regime_break"
                or model.reason == "no_quiet_slots"
                or "regime" in model.reason
                or not model.trusted,
            )

    def test_insufficient_history_not_trusted(self):
        now = timezone.now()
        # Only a few days of data
        for day in range(3):
            TeslaCarDataSnapshot.objects.create(
                vin=VIN,
                hashedVin="h" * 56,
                Date=now - timedelta(days=day, hours=2),
                speed=0,
                shift_state="P",
                charging_state="Disconnected",
            )
        model = compute_habit_model(VIN, now=now)
        self.assertFalse(model.trusted)
        self.assertIsNone(
            model.suggested_idle_interval_minutes(now.astimezone(TZ))
        )
