"""AC wall charge must not use the dense DC poll interval.

Tesla Fleet sets fast_charger_type to MCSingleWireCAN / ACSingleWireCAN on
the Tesla AC connector. Those are not Superchargers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from matesla.capture import (
    CAPTURE_TZ,
    INTERVAL_AC_CHARGE_MIN,
    INTERVAL_DC_CHARGE_MIN,
    INTERVAL_NIGHT_DEFAULT_MIN,
    activity_kind,
    poll_interval_minutes,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaToken import TeslaToken, TeslaVehicle
from matesla.models.VinHash import HashTheVin


class AcVsDcChargeIntervalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("ac_dc_u", password="x")
        self.vin = "5YJ3E7EA5KF000042"
        self.vehicle = TeslaVehicle.objects.create(
            user=self.user,
            api_id="42042",
            vin=self.vin,
            display_name="Aram",
            state="online",
            is_primary=True,
        )

    def _at(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 8, 24, hour, minute, tzinfo=CAPTURE_TZ)

    def _snap(self, now: datetime, **fields):
        when = now - timedelta(minutes=1)
        defaults = {
            "vin": self.vin,
            "hashedVin": HashTheVin(self.vin),
            "Date": when,
            "DateOnlyDay": when.date(),
            "randomNr": 0.1,
            "charging_state": "Charging",
            "shift_state": "P",
            "speed": 0.0,
            "charger_power": 3.0,
            "fast_charger_present": False,
            "fast_charger_type": "MCSingleWireCAN",
            "is_user_present": False,
            "sentry_mode": False,
            "is_climate_on": False,
            "climate_keeper_mode": False,
            "climate_keeper_modeRaw": "off",
        }
        defaults.update(fields)
        return TeslaCarDataSnapshot.objects.create(**defaults)

    def test_mcsinglewirecan_day_is_ac_15_min(self):
        now = self._at(10)
        self._snap(now)
        self.assertEqual(activity_kind(self.vehicle, now=now), "ac_charge")
        self.assertEqual(poll_interval_minutes(self.vehicle, now=now), INTERVAL_AC_CHARGE_MIN)
        self.assertEqual(INTERVAL_AC_CHARGE_MIN, 15)

    def test_mcsinglewirecan_night_is_30_min(self):
        now = self._at(3)
        self._snap(now)
        self.assertEqual(activity_kind(self.vehicle, now=now), "ac_charge")
        self.assertEqual(
            poll_interval_minutes(self.vehicle, now=now), INTERVAL_NIGHT_DEFAULT_MIN
        )
        self.assertEqual(INTERVAL_NIGHT_DEFAULT_MIN, 30)

    def test_acsinglewirecan_is_ac_not_dc(self):
        now = self._at(14)
        self._snap(
            now,
            fast_charger_type="ACSingleWireCAN",
            charger_power=11.0,
        )
        self.assertEqual(activity_kind(self.vehicle, now=now), "ac_charge")
        self.assertEqual(poll_interval_minutes(self.vehicle, now=now), INTERVAL_AC_CHARGE_MIN)

    def test_combo_supercharge_stays_one_minute(self):
        now = self._at(10)
        self._snap(
            now,
            fast_charger_type="Combo",
            fast_charger_present=True,
            charger_power=80.0,
        )
        self.assertEqual(activity_kind(self.vehicle, now=now), "dc_charge")
        self.assertEqual(
            poll_interval_minutes(self.vehicle, now=now), INTERVAL_DC_CHARGE_MIN
        )

    def test_combo_night_stays_one_minute(self):
        now = self._at(23)
        self._snap(
            now,
            fast_charger_type="Tesla",
            fast_charger_present=True,
            charger_power=40.0,
        )
        self.assertEqual(activity_kind(self.vehicle, now=now), "dc_charge")
        self.assertEqual(
            poll_interval_minutes(self.vehicle, now=now), INTERVAL_DC_CHARGE_MIN
        )

    def test_high_power_without_type_is_dc(self):
        now = self._at(10)
        self._snap(
            now,
            fast_charger_type=None,
            fast_charger_present=False,
            charger_power=90.0,
        )
        self.assertEqual(activity_kind(self.vehicle, now=now), "dc_charge")
        self.assertEqual(
            poll_interval_minutes(self.vehicle, now=now), INTERVAL_DC_CHARGE_MIN
        )


class CaptureSuperchargerInvoiceHookTests(TestCase):
    def test_logs_billed_euros_when_invoice_arrives(self):
        User = get_user_model()
        user = User.objects.create_user("cap_sc_u", password="x")
        TeslaVehicle.objects.create(
            user=user,
            api_id="88001",
            vin="5YJTESTVIN0000001",
            display_name="RobotBleu",
        )
        messages = []
        with patch(
            "matesla.tesla_charging_history.maybe_refresh_invoice_after_supercharge",
            return_value={"status": "fetched", "cost_eur": 12.79},
        ):
            from matesla.capture import _refresh_supercharger_invoices_after_capture

            _refresh_supercharger_invoices_after_capture(messages)
        self.assertTrue(
            any("12.79" in line and "Superchargeur" in line for line in messages)
        )

    def test_capture_tick_tries_invoice_even_when_car_is_not_due(self):
        User = get_user_model()
        user = User.objects.create_user("cap_sc_due", password="x")
        TeslaVehicle.objects.create(
            user=user,
            api_id="88002",
            vin="5YJTESTVIN0000002",
            display_name="HookCar",
            last_polled_at=timezone.now(),
        )
        TeslaToken.objects.create(
            user_id=user, access_token="tok", refresh_token="ref"
        )
        with patch(
            "matesla.capture._refresh_supercharger_invoices_after_capture"
        ) as hook, patch("matesla.capture._run_geo_enrichment"):
            from matesla.capture import capture_all_online_vehicles

            capture_all_online_vehicles()
        hook.assert_called_once()
