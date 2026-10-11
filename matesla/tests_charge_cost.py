"""Charge-cost engine, Elia cache, and rule priority (no live HTTP / no real VINs)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from matesla.charge_cost import (
    CHARGE_COST_TZ,
    ChargeSession,
    annotate_daymap_charges,
    apply_daily_place_fees,
    expand_session_from_snapshots,
    is_active_charge,
    iter_sessions_starting_in,
    kwh_from_plug_counter,
    plug_counter_reset,
    price_and_persist,
    price_session,
    price_sessions_starting_in,
)
from matesla.elia_dayahead import fetch_elia_day_ahead, store_spot_rows
from personalstats.charge_pages import (
    _month_is_complete,
    _previous_month_window,
    _rate_mean_stdev,
    _rate_tone,
    _session_eur_per_kwh,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.ChargeCost import (
    COST_PARTIAL,
    COST_PRICED,
    COST_UNPRICED,
    ChargeCostSettings,
    ChargePlace,
    ChargeSessionCost,
    DayAheadSpotPrice,
    PlaceTariffPeriod,
    TeslaChargingHistorySync,
    TeslaChargingInvoice,
    ROLE_HOME,
    ROLE_WORK,
    RULE_HOME_DAY_NIGHT,
    RULE_HOME_DYNAMIC,
    RULE_HOME_FLAT,
    RULE_OTHER,
    RULE_PER_DAY,
    RULE_PLACE,
    RULE_SUPERCHARGER_INVOICE,
    RULE_SUPERCHARGER_RATE,
    RULE_UNPRICED,
    RULE_WORK,
    TARIFF_DAY_NIGHT,
    TARIFF_DYNAMIC,
    TARIFF_FLAT,
    TARIFF_PER_DAY,
    VehiclePlaceRole,
)

UTC = ZoneInfo("UTC")
HV_A = "b" * 56
HV_B = "c" * 56
# Arbitrary test coordinates — not a real address.
HOME_LAT, HOME_LON = 50.0000, 4.0000
WORK_LAT, WORK_LON = 50.1000, 4.2000
ELSE_LAT, ELSE_LON = 51.0000, 5.0000


def _at(year, month, day, hour, minute=0):
    """Aware datetime in Europe/Brussels."""
    return datetime(year, month, day, hour, minute, tzinfo=CHARGE_COST_TZ)


class PlugCounterKwhTests(TestCase):
    def test_new_plug_uses_session_total(self):
        self.assertAlmostEqual(kwh_from_plug_counter([0.64, 20.6], 53.64), 20.6)
        self.assertAlmostEqual(kwh_from_plug_counter([1.0, 30.0], 0.0), 30.0)
        self.assertAlmostEqual(kwh_from_plug_counter([5.1, 14.54], None), 14.54)

    def test_same_plug_later_burst_is_increment(self):
        self.assertAlmostEqual(kwh_from_plug_counter([22.1, 29.5], 22.0), 7.5)
        self.assertAlmostEqual(kwh_from_plug_counter([22.3], 22.34), 0.0)

    def test_complete_with_cabin_kw_is_not_active_charge(self):
        self.assertFalse(is_active_charge("Complete", 2.0))
        self.assertFalse(is_active_charge("Disconnected", 1.0))
        self.assertTrue(is_active_charge("Charging", 7.0))
        self.assertTrue(is_active_charge("", 7.0))
        self.assertTrue(is_active_charge(None, 7.0))

    def test_plug_counter_reset_detects_new_cable(self):
        self.assertTrue(plug_counter_reset(14.9, 5.28))
        self.assertFalse(plug_counter_reset(22.0, 22.1))
        self.assertFalse(plug_counter_reset(None, 5.28))
        self.assertFalse(plug_counter_reset(14.9, None))


class October10CabinHeatSessionTests(TestCase):
    """
    RobotBleu 2026-10-10: leftover 53.64 kWh, overnight AC, then plugged
    Complete + 1–2 kW cabin heat at 90 %. Charges and Stats must not emit a
    second 22.3 kWh session at 07:31.
    """

    VIN = "5YJ3E1EA0KFCOSTUI01"

    def _snap(self, when, **fields):
        defaults = dict(
            vin=self.VIN,
            hashedVin=HV_A,
            Date=when,
            DateOnlyDay=when.date(),
            latitude=HOME_LAT,
            longitude=HOME_LON,
            battery_level=90.0,
            charger_power=0.0,
            fast_charger_present=False,
            fast_charger_type="MCSingleWireCAN",
        )
        defaults.update(fields)
        return TeslaCarDataSnapshot.objects.create(**defaults)

    def setUp(self):
        self._snap(
            _at(2026, 10, 10, 0, 29),
            charging_state="Disconnected",
            charge_energy_added=53.64,
            battery_level=61.0,
        )
        # Night AC is polled every 30 min; keep gaps under the 30 min session split.
        overnight = (
            (0, 50, 0.64, 61.0),
            (1, 20, 4.0, 66.0),
            (1, 50, 8.0, 71.0),
            (2, 20, 12.0, 76.0),
            (2, 50, 16.0, 81.0),
            (3, 20, 18.5, 85.0),
            (3, 40, 19.6, 87.0),
            (3, 58, 20.6, 88.0),
        )
        for hour, minute, energy, soc in overnight:
            self._snap(
                _at(2026, 10, 10, hour, minute),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=energy,
                battery_level=soc,
            )
        self._snap(
            _at(2026, 10, 10, 4, 19),
            charging_state="Complete",
            charger_power=0.0,
            charge_energy_added=22.34,
            battery_level=90.0,
        )
        self._snap(
            _at(2026, 10, 10, 7, 31),
            charging_state="Complete",
            charger_power=2.0,
            charge_energy_added=22.3,
            battery_level=90.0,
            is_climate_on=True,
        )
        self._snap(
            _at(2026, 10, 10, 7, 43),
            charging_state="Complete",
            charger_power=1.0,
            charge_energy_added=22.3,
            battery_level=90.0,
            is_climate_on=True,
        )
        self._snap(
            _at(2026, 10, 10, 7, 46),
            charging_state="Disconnected",
            charger_power=0.0,
            charge_energy_added=22.3,
            battery_level=90.0,
        )
        self._snap(
            _at(2026, 10, 10, 11, 48),
            charging_state="Charging",
            charger_power=7.0,
            charge_energy_added=0.10,
            battery_level=70.0,
        )
        self._snap(
            _at(2026, 10, 10, 12, 10),
            charging_state="Charging",
            charger_power=7.0,
            charge_energy_added=0.40,
            battery_level=71.0,
        )
        self._snap(
            _at(2026, 10, 10, 12, 35),
            charging_state="Charging",
            charger_power=7.0,
            charge_energy_added=0.72,
            battery_level=72.0,
        )

    def test_charges_does_not_price_cabin_heat_as_night_kwh(self):
        sessions = iter_sessions_starting_in(
            HV_A, _at(2026, 10, 10, 0, 0), _at(2026, 10, 11, 0, 0)
        )
        self.assertEqual(len(sessions), 2)
        self.assertAlmostEqual(sessions[0].kwh, 20.6, places=1)
        self.assertAlmostEqual(sessions[1].kwh, 0.72, places=2)
        heat = _at(2026, 10, 10, 7, 31)
        for session in sessions:
            self.assertLess(session.kwh, 22.0)
            self.assertGreater(abs((session.start - heat).total_seconds()), 60)

    def test_stats_histograms_skip_complete_cabin_heat(self):
        from personalstats.views import _iter_charge_sessions

        sessions = list(
            _iter_charge_sessions(
                TeslaCarDataSnapshot.objects.filter(hashedVin=HV_A)
            )
        )
        self.assertEqual(len(sessions), 2)
        heat = _at(2026, 10, 10, 7, 31)
        for session in sessions:
            self.assertGreater(abs((session[0]["t"] - heat).total_seconds()), 60)
            energies = [
                float(point["charge_energy_added"])
                for point in session
                if point.get("charge_energy_added")
            ]
            self.assertLess(max(energies), 22.0)

    def test_cabin_heat_is_not_a_live_dc_snapshot(self):
        from matesla.tesla_charging_history import _recent_dc_snapshot

        self.assertIsNone(_recent_dc_snapshot(HV_A, _at(2026, 10, 10, 0, 0)))


class September13TwoSuperchargeTests(TestCase):
    """
    RobotBleu 2026-09-13 Anderlecht: two Supercharge plugs 16 min apart
    (Stopped in between, charge_energy_added reset). DayMap split them on
    park but expand+invoice matching used to glue both Tesla invoices
    (4.00 + 7.69) onto each row.
    """

    VIN = "5YJ3E1EA0KFCOSTSC13"
    SC_LAT, SC_LON = 50.8379, 4.2882

    def _snap(self, when, **fields):
        defaults = dict(
            vin=self.VIN,
            hashedVin=HV_A,
            Date=when,
            DateOnlyDay=when.astimezone(UTC).date(),
            latitude=self.SC_LAT,
            longitude=self.SC_LON,
            battery_level=40.0,
            charger_power=0.0,
            fast_charger_present=True,
            fast_charger_type="Tesla",
        )
        defaults.update(fields)
        return TeslaCarDataSnapshot.objects.create(**defaults)

    def setUp(self):
        User = get_user_model()
        user = User.objects.create_user("sc_sep13", password="x")
        ChargeCostSettings.objects.create(
            user=user, other_eur_per_kwh=0.40, supercharger_eur_per_kwh=0.55
        )
        self._snap(
            _at(2026, 9, 13, 9, 26),
            charging_state="Starting",
            charger_power=0.0,
            charge_energy_added=0.0,
            battery_level=19.0,
        )
        self._snap(
            _at(2026, 9, 13, 9, 28),
            charging_state="Charging",
            charger_power=160.0,
            charge_energy_added=4.42,
            battery_level=25.0,
        )
        self._snap(
            _at(2026, 9, 13, 9, 32),
            charging_state="Charging",
            charger_power=129.0,
            charge_energy_added=13.3,
            battery_level=37.0,
        )
        self._snap(
            _at(2026, 9, 13, 9, 34),
            charging_state="Stopped",
            charger_power=0.0,
            charge_energy_added=14.9,
            battery_level=40.0,
        )
        self._snap(
            _at(2026, 9, 13, 9, 50),
            charging_state="Charging",
            charger_power=110.0,
            charge_energy_added=5.28,
            battery_level=46.0,
        )
        self._snap(
            _at(2026, 9, 13, 10, 11),
            charging_state="Charging",
            charger_power=43.0,
            charge_energy_added=29.58,
            battery_level=80.0,
        )
        self._snap(
            _at(2026, 9, 13, 10, 13),
            charging_state="Disconnected",
            charger_power=0.0,
            charge_energy_added=30.86,
            battery_level=81.0,
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="762271391",
            start=_at(2026, 9, 13, 9, 26),
            end=_at(2026, 9, 13, 9, 32),
            site_name="Anderlecht, Belgium",
            total_eur=4.00,
            kwh=16.6895,
            currency="EUR",
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="762280516",
            start=_at(2026, 9, 13, 9, 47),
            end=_at(2026, 9, 13, 10, 12),
            site_name="Anderlecht, Belgium",
            total_eur=7.69,
            kwh=32.0538,
            currency="EUR",
        )
        # Stale merged row as production had before the split.
        ChargeSessionCost.objects.create(
            hashed_vin=HV_A,
            start=_at(2026, 9, 13, 9, 26).astimezone(UTC),
            end=_at(2026, 9, 13, 10, 11).astimezone(UTC),
            kwh=29.58,
            latitude=self.SC_LAT,
            longitude=self.SC_LON,
            rule=RULE_SUPERCHARGER_INVOICE,
            status=COST_PRICED,
            cost_eur=11.69,
            tesla_invoice_eur=11.69,
            tesla_session_id="762271391",
        )

    def test_iter_sessions_splits_on_counter_reset(self):
        sessions = iter_sessions_starting_in(
            HV_A, _at(2026, 9, 13, 0, 0), _at(2026, 9, 14, 0, 0)
        )
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].start, _at(2026, 9, 13, 9, 26))
        self.assertEqual(sessions[0].end, _at(2026, 9, 13, 9, 32))
        self.assertAlmostEqual(sessions[0].kwh, 13.3, places=1)
        self.assertEqual(sessions[1].start, _at(2026, 9, 13, 9, 50))
        self.assertEqual(sessions[1].end, _at(2026, 9, 13, 10, 11))
        self.assertAlmostEqual(sessions[1].kwh, 29.58, places=2)

    def test_expand_does_not_cross_the_new_plug(self):
        first = expand_session_from_snapshots(
            HV_A,
            _at(2026, 9, 13, 9, 26),
            _at(2026, 9, 13, 9, 34),
            14.9,
            self.SC_LAT,
            self.SC_LON,
        )
        second = expand_session_from_snapshots(
            HV_A,
            _at(2026, 9, 13, 9, 50),
            _at(2026, 9, 13, 10, 13),
            30.86,
            self.SC_LAT,
            self.SC_LON,
        )
        self.assertEqual(first.start, _at(2026, 9, 13, 9, 26))
        self.assertEqual(first.end, _at(2026, 9, 13, 9, 32))
        self.assertEqual(second.start, _at(2026, 9, 13, 9, 50))
        self.assertEqual(second.end, _at(2026, 9, 13, 10, 11))

    def test_daymap_rows_keep_each_tesla_invoice(self):
        charges = [
            {
                "start": _at(2026, 9, 13, 9, 26),
                "end": _at(2026, 9, 13, 9, 34),
                "kwh_added": 14.9,
                "lat": self.SC_LAT,
                "lon": self.SC_LON,
                "is_supercharger": True,
            },
            {
                "start": _at(2026, 9, 13, 9, 50),
                "end": _at(2026, 9, 13, 10, 13),
                "kwh_added": 30.86,
                "lat": self.SC_LAT,
                "lon": self.SC_LON,
                "is_supercharger": True,
            },
        ]
        annotate_daymap_charges(HV_A, charges)
        self.assertAlmostEqual(charges[0]["cost_eur"], 4.00, places=2)
        self.assertAlmostEqual(charges[1]["cost_eur"], 7.69, places=2)
        self.assertEqual(charges[0]["cost_rule"], RULE_SUPERCHARGER_INVOICE)
        self.assertEqual(charges[1]["cost_rule"], RULE_SUPERCHARGER_INVOICE)
        self.assertNotAlmostEqual(charges[0]["cost_eur"], 11.69, places=2)
        self.assertNotAlmostEqual(charges[1]["cost_eur"], 11.69, places=2)
        stored = list(
            ChargeSessionCost.objects.filter(hashed_vin=HV_A).order_by("start")
        )
        self.assertEqual(len(stored), 2)
        self.assertAlmostEqual(stored[0].cost_eur, 4.00, places=2)
        self.assertAlmostEqual(stored[1].cost_eur, 7.69, places=2)


class ChargeCostEngineTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("cost_user", password="x")
        self.home = ChargePlace.objects.create(
            user=self.user,
            name="Test home",
            latitude=HOME_LAT,
            longitude=HOME_LON,
            radius_m=150,
        )
        self.work = ChargePlace.objects.create(
            user=self.user,
            name="Test work",
            latitude=WORK_LAT,
            longitude=WORK_LON,
            radius_m=150,
        )
        self.settings = ChargeCostSettings.objects.create(
            user=self.user,
            other_eur_per_kwh=0.40,
            supercharger_eur_per_kwh=0.55,
        )

    def _session(self, hashed_vin, start, end, kwh, lat, lon, **kwargs):
        return ChargeSession(
            hashed_vin=hashed_vin,
            start=start,
            end=end,
            kwh=kwh,
            lat=lat,
            lon=lon,
            **kwargs,
        )

    def test_nothing_configured_is_unpriced(self):
        self.settings.other_eur_per_kwh = None
        self.settings.supercharger_eur_per_kwh = None
        self.settings.save()
        session = self._session(
            HV_A, _at(2025, 11, 15, 12), _at(2025, 11, 15, 14), 10.0, ELSE_LAT, ELSE_LON
        )
        result = price_session(session)
        self.assertEqual(result.status, COST_UNPRICED)
        self.assertIsNone(result.cost_eur)
        self.assertEqual(result.rule, RULE_UNPRICED)

    def test_other_chargers_use_user_rate(self):
        session = self._session(
            HV_A, _at(2025, 11, 15, 12), _at(2025, 11, 15, 14), 10.0, ELSE_LAT, ELSE_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_OTHER)
        self.assertEqual(result.status, COST_PRICED)
        self.assertAlmostEqual(result.cost_eur, 4.0, places=4)

    def test_home_flat_and_role_period_per_vehicle(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2020, 1, 1),
            valid_to=None,
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.20,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 6, 30),
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_B,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2025, 7, 1),
            valid_to=None,
        )
        june = self._session(
            HV_A, _at(2025, 6, 15, 3), _at(2025, 6, 15, 5), 10.0, HOME_LAT, HOME_LON
        )
        june_b = self._session(
            HV_B, _at(2025, 6, 15, 3), _at(2025, 6, 15, 5), 10.0, HOME_LAT, HOME_LON
        )
        july_a = self._session(
            HV_A, _at(2025, 7, 15, 3), _at(2025, 7, 15, 5), 10.0, HOME_LAT, HOME_LON
        )
        july_b = self._session(
            HV_B, _at(2025, 7, 15, 3), _at(2025, 7, 15, 5), 10.0, HOME_LAT, HOME_LON
        )
        self.assertEqual(price_session(june).rule, RULE_HOME_FLAT)
        self.assertAlmostEqual(price_session(june).cost_eur, 2.0, places=4)
        # Same GPS, not this car's home yet → still the named place tariff
        self.assertEqual(price_session(june_b).rule, RULE_PLACE)
        self.assertAlmostEqual(price_session(june_b).cost_eur, 2.0, places=4)
        self.assertEqual(price_session(july_a).rule, RULE_PLACE)
        self.assertEqual(price_session(july_b).rule, RULE_HOME_FLAT)

    def test_outside_geofence_is_not_home(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.20,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        # ~1 km away at this latitude
        session = self._session(
            HV_A,
            _at(2025, 11, 15, 3),
            _at(2025, 11, 15, 5),
            10.0,
            HOME_LAT + 0.01,
            HOME_LON,
        )
        self.assertEqual(price_session(session).rule, RULE_OTHER)

    def test_home_without_tariff_for_that_date_is_not_other(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2025, 11, 1),
            mode=TARIFF_DYNAMIC,
            dynamic_surcharge_cents=19,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        session = self._session(
            HV_A,
            _at(2024, 1, 6, 19, 14),
            _at(2024, 1, 6, 22, 15),
            21.19,
            HOME_LAT + 0.0002,
            HOME_LON + 0.0002,
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_HOME_DYNAMIC)
        self.assertEqual(result.status, COST_UNPRICED)
        self.assertIsNone(result.cost_eur)
        self.assertEqual(result.place_id, self.home.id)

    def test_known_home_rate_with_no_kwh_is_zero_not_unpriced(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2019, 2, 1),
            valid_to=date(2022, 2, 1),
            mode=TARIFF_DAY_NIGHT,
            day_eur_per_kwh=0.22,
            night_eur_per_kwh=0.18,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 5, 2),
        )
        session = self._session(
            HV_A,
            _at(2021, 5, 27, 20, 50),
            _at(2021, 5, 27, 20, 52),
            None,
            HOME_LAT,
            HOME_LON,
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_HOME_DAY_NIGHT)
        self.assertEqual(result.status, COST_PRICED)
        self.assertAlmostEqual(result.cost_eur, 0.0, places=4)
        self.assertEqual(result.place_id, self.home.id)

    def test_named_place_beats_other_chargers_rate(self):
        camping = ChargePlace.objects.create(
            user=self.user,
            name="Camping",
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=camping,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.42,
        )
        session = self._session(
            HV_A, _at(2025, 11, 15, 12), _at(2025, 11, 15, 14), 10.0, ELSE_LAT, ELSE_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_PLACE)
        self.assertAlmostEqual(result.cost_eur, 4.2, places=4)

    def test_campsite_per_day_once_per_civil_day(self):
        camping = ChargePlace.objects.create(
            user=self.user,
            name="camping cham",
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=camping,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_PER_DAY,
            eur_per_day=15.0,
        )
        morning = self._session(
            HV_A, _at(2021, 7, 11, 8), _at(2021, 7, 11, 9), 6.8, ELSE_LAT, ELSE_LON
        )
        evening = self._session(
            HV_A, _at(2021, 7, 11, 19), _at(2021, 7, 11, 21), 16.0, ELSE_LAT, ELSE_LON
        )
        next_day = self._session(
            HV_A, _at(2021, 7, 12, 20), _at(2021, 7, 12, 22), 12.0, ELSE_LAT, ELSE_LON
        )
        first = price_session(morning)
        second = price_session(evening)
        third = price_session(next_day)
        self.assertEqual(first.rule, RULE_PER_DAY)
        apply_daily_place_fees(
            [(morning, first), (evening, second), (next_day, third)]
        )
        self.assertAlmostEqual(first.cost_eur, 15.0, places=4)
        self.assertAlmostEqual(second.cost_eur, 0.0, places=4)
        self.assertAlmostEqual(third.cost_eur, 15.0, places=4)
        price_and_persist(morning)
        later = price_and_persist(evening)
        self.assertAlmostEqual(later.cost_eur, 0.0, places=4)

    def test_work_rate(self):
        PlaceTariffPeriod.objects.create(
            place=self.work,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.0,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.work,
            role=ROLE_WORK,
            valid_from=date(2020, 1, 1),
        )
        session = self._session(
            HV_A, _at(2025, 11, 15, 10), _at(2025, 11, 15, 12), 20.0, WORK_LAT, WORK_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_WORK)
        self.assertAlmostEqual(result.cost_eur, 0.0, places=4)

    def test_home_beats_supercharger_flag(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.20,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        session = self._session(
            HV_A,
            _at(2025, 11, 15, 3),
            _at(2025, 11, 15, 5),
            10.0,
            HOME_LAT,
            HOME_LON,
            is_supercharger=True,
        )
        self.assertEqual(price_session(session).rule, RULE_HOME_FLAT)

    def test_supercharger_rate_and_invoice(self):
        sc = self._session(
            HV_A,
            _at(2025, 11, 15, 12),
            _at(2025, 11, 15, 13),
            40.0,
            ELSE_LAT,
            ELSE_LON,
            is_supercharger=True,
        )
        rated = price_session(sc)
        self.assertEqual(rated.rule, RULE_SUPERCHARGER_RATE)
        self.assertAlmostEqual(rated.cost_eur, 22.0, places=4)
        sc.tesla_invoice_eur = 18.5
        invoiced = price_session(sc)
        self.assertEqual(invoiced.rule, RULE_SUPERCHARGER_INVOICE)
        self.assertAlmostEqual(invoiced.cost_eur, 18.5, places=4)

    def test_day_night_splits_at_22h(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_DAY_NIGHT,
            day_eur_per_kwh=0.30,
            night_eur_per_kwh=0.10,
            night_start=time(22, 0),
            night_end=time(7, 0),
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        # 21:00–23:00, 2 kWh uniform → 1 kWh day + 1 kWh night
        session = self._session(
            HV_A, _at(2025, 11, 15, 21), _at(2025, 11, 15, 23), 2.0, HOME_LAT, HOME_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_HOME_DAY_NIGHT)
        self.assertAlmostEqual(result.cost_eur, 0.30 + 0.10, places=4)

    def test_dynamic_follows_spot_per_mtu_plus_cents(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2025, 11, 1),
            mode=TARIFF_DYNAMIC,
            dynamic_surcharge_cents=10,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        start = _at(2025, 11, 15, 10, 0)
        # Four 15-min MTUs covering 10:00–11:00 Brussels (09:00–10:00 UTC in November)
        utc0 = start.astimezone(UTC)
        for index, eur_mwh in enumerate((100.0, 200.0, 100.0, 100.0)):
            DayAheadSpotPrice.objects.create(
                mtu_start=utc0 + timedelta(minutes=15 * index),
                resolution_minutes=15,
                price_eur_mwh=eur_mwh,
            )
        session = self._session(
            HV_A, start, start + timedelta(hours=1), 4.0, HOME_LAT, HOME_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_HOME_DYNAMIC)
        self.assertEqual(result.status, COST_PRICED)
        # 1 kWh × (0.100+0.10) + 1×(0.200+0.10) + 2×(0.100+0.10) = 0.90
        self.assertAlmostEqual(result.cost_eur, 0.90, places=4)

    def test_dynamic_missing_mtu_is_partial(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2025, 11, 1),
            mode=TARIFF_DYNAMIC,
            dynamic_surcharge_cents=0,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        start = _at(2025, 11, 15, 10, 0)
        utc0 = start.astimezone(UTC)
        DayAheadSpotPrice.objects.create(
            mtu_start=utc0,
            resolution_minutes=60,
            price_eur_mwh=100.0,
        )
        # 10:00–12:00 but only one hourly spot → second hour missing
        session = self._session(
            HV_A, start, start + timedelta(hours=2), 2.0, HOME_LAT, HOME_LON
        )
        result = price_session(session)
        self.assertEqual(result.rule, RULE_HOME_DYNAMIC)
        self.assertEqual(result.status, COST_PARTIAL)
        self.assertAlmostEqual(result.cost_eur, 0.10, places=4)
        self.assertAlmostEqual(result.missing_kwh, 1.0, places=4)

    def test_pricing_window_fetches_elia_when_dynamic_overlaps(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2025, 10, 31),
            mode=TARIFF_DYNAMIC,
            dynamic_surcharge_cents=19,
        )
        start = datetime(2026, 9, 1, tzinfo=CHARGE_COST_TZ)
        end = datetime(2026, 10, 1, tzinfo=CHARGE_COST_TZ)
        with patch("matesla.elia_dayahead.ensure_spot_coverage") as mock_ensure:
            price_sessions_starting_in(HV_A, start, end)
        mock_ensure.assert_called_once()
        self.assertEqual(mock_ensure.call_args[0][0], date(2026, 9, 1))
        self.assertEqual(mock_ensure.call_args[0][1], date(2026, 9, 30))
        self.assertEqual(mock_ensure.call_args[1]["max_days"], 14)

        year_2021_start = datetime(2021, 1, 1, tzinfo=CHARGE_COST_TZ)
        year_2021_end = datetime(2022, 1, 1, tzinfo=CHARGE_COST_TZ)
        with patch("matesla.elia_dayahead.ensure_spot_coverage") as mock_ensure:
            price_sessions_starting_in(HV_A, year_2021_start, year_2021_end)
        mock_ensure.assert_not_called()

    def test_pricing_window_skips_elia_without_dynamic(self):
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.30,
        )
        start = datetime(2026, 9, 1, tzinfo=CHARGE_COST_TZ)
        end = datetime(2026, 10, 1, tzinfo=CHARGE_COST_TZ)
        with patch("matesla.elia_dayahead.ensure_spot_coverage") as mock_ensure:
            price_sessions_starting_in(HV_A, start, end)
        mock_ensure.assert_not_called()

    def test_persist_and_daymap_session_total_flag(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2020, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.25,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2020, 1, 1),
        )
        start = _at(2025, 11, 15, 22)
        end = _at(2025, 11, 16, 7)
        vin = "5YJ3E1EA0KFCOST0001"
        cursor = start
        kwh = 0.0
        while cursor <= end:
            kwh = min(12.0, kwh + 0.35)
            TeslaCarDataSnapshot.objects.create(
                vin=vin,
                hashedVin=HV_A,
                Date=cursor,
                DateOnlyDay=cursor.astimezone(UTC).date(),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=kwh,
                latitude=HOME_LAT,
                longitude=HOME_LON,
                battery_level=50.0,
            )
            cursor += timedelta(minutes=15)
        session = self._session(HV_A, start, end, 12.0, HOME_LAT, HOME_LON)
        result = price_and_persist(session)
        self.assertAlmostEqual(result.cost_eur, 3.0, places=4)
        self.assertEqual(
            ChargeSessionCost.objects.filter(hashed_vin=HV_A).count(), 1
        )
        # Two civil-day fragments of the same plug-in (expand via snapshots)
        charges = [
            {
                "start": start,
                "end": _at(2025, 11, 16, 0),
                "kwh_added": 4.0,
                "lat": HOME_LAT,
                "lon": HOME_LON,
            },
            {
                "start": _at(2025, 11, 16, 0),
                "end": end,
                "kwh_added": 12.0,
                "lat": HOME_LAT,
                "lon": HOME_LON,
            },
        ]
        annotate_daymap_charges(HV_A, charges)
        self.assertTrue(charges[0]["cost_is_session_total"])
        self.assertTrue(charges[1]["cost_is_session_total"])
        self.assertAlmostEqual(charges[0]["cost_eur"], 3.0, places=4)
        self.assertAlmostEqual(charges[1]["cost_eur"], 3.0, places=4)
        self.assertEqual(
            ChargeSessionCost.objects.filter(hashed_vin=HV_A).count(), 1
        )

    def test_stored_tesla_invoice_survives_recalc(self):
        start = _at(2025, 11, 15, 12)
        end = _at(2025, 11, 15, 13)
        session = self._session(
            HV_A,
            start,
            end,
            30.0,
            ELSE_LAT,
            ELSE_LON,
            is_supercharger=True,
            tesla_invoice_eur=12.0,
        )
        price_and_persist(session)
        session.tesla_invoice_eur = None
        result = price_and_persist(session)
        self.assertEqual(result.rule, RULE_SUPERCHARGER_INVOICE)
        self.assertAlmostEqual(result.cost_eur, 12.0, places=4)


class EliaDayAheadTests(TestCase):
    def test_qh_preferred_over_hourly(self):
        qh = [
            {"dateTime": "2025-11-14T23:00:00Z", "price": 80.5, "isVisible": True},
            {"dateTime": "2025-11-14T23:15:00Z", "price": 81.0, "isVisible": True},
        ]
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = qh
        session.get.return_value = resp
        rows = fetch_elia_day_ahead(date(2025, 11, 15), session=session)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["resolution_minutes"], 15)
        self.assertAlmostEqual(rows[0]["price_eur_mwh"], 80.5)
        self.assertEqual(session.get.call_count, 1)

    def test_hourly_fallback_when_qh_empty(self):
        empty = MagicMock()
        empty.raise_for_status = MagicMock()
        empty.json.return_value = []
        hourly = MagicMock()
        hourly.raise_for_status = MagicMock()
        hourly.json.return_value = [
            {"dateTime": "2024-01-14T23:00:00Z", "price": 83.8, "isVisible": True},
        ]
        session = MagicMock()
        session.get.side_effect = [empty, hourly]
        rows = fetch_elia_day_ahead(date(2024, 1, 15), session=session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_minutes"], 60)
        n = store_spot_rows(rows)
        self.assertEqual(n, 1)
        self.assertEqual(DayAheadSpotPrice.objects.count(), 1)

    def test_ensure_skips_days_already_cached(self):
        from matesla.elia_dayahead import ensure_spot_coverage, store_spot_rows

        day = date(2025, 11, 15)
        rows = [
            {
                "mtu_start": datetime(2025, 11, 14, 23, 0, tzinfo=UTC),
                "resolution_minutes": 60,
                "price_eur_mwh": 80.0,
            }
        ]
        # 24 fake hourly points so the day counts as cached
        for hour in range(24):
            rows.append(
                {
                    "mtu_start": datetime(2025, 11, 14, 23, 0, tzinfo=UTC)
                    + timedelta(hours=hour),
                    "resolution_minutes": 60,
                    "price_eur_mwh": 80.0,
                }
            )
        store_spot_rows(rows)
        session = MagicMock()
        summary = ensure_spot_coverage(day, day, session=session)
        self.assertEqual(summary["days_skipped"], 1)
        self.assertEqual(summary["days_ok"], 0)
        session.get.assert_not_called()

    def test_default_command_ensures_lookback_through_tomorrow(self):
        from datetime import date as date_cls
        from django.core.management import call_command
        from django.utils import timezone
        from matesla.elia_dayahead import CRON_LOOKBACK_DAYS
        from matesla.management.commands.FetchEliaDayAhead import BRUSSELS

        today = timezone.now().astimezone(BRUSSELS).date()
        with patch(
            "matesla.management.commands.FetchEliaDayAhead.ensure_spot_coverage"
        ) as mock_ensure:
            mock_ensure.return_value = {
                "days_ok": 0,
                "days_failed": 0,
                "days_skipped": 8,
                "days_truncated": 0,
                "rows": 0,
            }
            call_command("FetchEliaDayAhead")
        mock_ensure.assert_called_once()
        start, end = mock_ensure.call_args[0][:2]
        self.assertEqual(start, today - timedelta(days=CRON_LOOKBACK_DAYS))
        self.assertEqual(end, today + timedelta(days=1))
        self.assertIsInstance(start, date_cls)

    def test_range_logs_and_continues_on_http_error(self):
        from matesla.elia_dayahead import fetch_and_store_range

        with self.assertLogs("matesla.elia_dayahead", level="WARNING") as logs:
            with self.settings():
                from unittest.mock import patch

                with patch(
                    "matesla.elia_dayahead.fetch_and_store_day",
                    side_effect=OSError("network down"),
                ):
                    summary = fetch_and_store_range(date(2025, 11, 1), date(2025, 11, 1))
        self.assertEqual(summary["days_ok"], 0)
        self.assertEqual(summary["days_failed"], 1)
        self.assertTrue(any("network down" in line for line in logs.output))


class ChargeRateStatsTests(SimpleTestCase):
    def test_session_rate_needs_energy_and_cost(self):
        self.assertAlmostEqual(_session_eur_per_kwh(4.0, 10.0), 0.4)
        self.assertEqual(_session_eur_per_kwh(0.0, 10.0), 0.0)
        self.assertIsNone(_session_eur_per_kwh(None, 10.0))
        self.assertIsNone(_session_eur_per_kwh(4.0, None))
        self.assertIsNone(_session_eur_per_kwh(4.0, 0.0))

    def test_mean_stdev_needs_two_distinct_rates(self):
        self.assertIsNone(_rate_mean_stdev([]))
        self.assertIsNone(_rate_mean_stdev([0.2]))
        self.assertIsNone(_rate_mean_stdev([0.2, 0.2, 0.2]))
        mean, stdev = _rate_mean_stdev([0.10, 0.10, 0.10, 0.50])
        self.assertAlmostEqual(mean, 0.20, places=4)
        self.assertAlmostEqual(stdev, 0.20, places=4)
        self.assertEqual(_rate_tone(0.50, mean, stdev), "high")
        self.assertIsNone(_rate_tone(0.10, mean, stdev))
        self.assertEqual(_rate_tone(0.0, mean, stdev), None)

    def test_low_tone_when_below_mean_minus_stdev(self):
        mean, stdev = _rate_mean_stdev([0.0, 0.20, 0.20, 0.20, 0.50])
        self.assertEqual(_rate_tone(0.0, mean, stdev), "low")
        self.assertEqual(_rate_tone(0.50, mean, stdev), "high")
        self.assertIsNone(_rate_tone(0.20, mean, stdev))

    def test_incomplete_month_uses_previous_calendar_month(self):
        today = date(2026, 10, 11)
        self.assertTrue(_month_is_complete(2026, 9, today))
        self.assertFalse(_month_is_complete(2026, 10, today))
        start, end = _previous_month_window(2026, 10)
        self.assertEqual(start, _at(2026, 9, 1, 0))
        self.assertEqual(end, _at(2026, 10, 1, 0))
        jan_start, jan_end = _previous_month_window(2026, 1)
        self.assertEqual(jan_start, _at(2025, 12, 1, 0))
        self.assertEqual(jan_end, _at(2026, 1, 1, 0))


@override_settings(ALLOWED_HOSTS=["testserver", "remote.example"])
class ChargeCostPagesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("cost_pages", password="x")
        from matesla.models.TeslaCarInfo import TeslaCarInfo

        TeslaCarInfo.objects.create(
            vin="5YJ3E1EA0KFCOSTUI01",
            hashedVin=HV_A,
            Date=date(2024, 1, 1),
            LastSeenDate=date(2024, 1, 1),
            car_type="model3",
            charge_port_type="CCS",
            exterior_color="Black",
            has_air_suspension=False,
            has_ludicrous_mode=False,
            motorized_charge_port=True,
            rear_seat_heaters="1",
            rhd=False,
            roof_color="Glass",
            wheel_type="Pinwheel18",
            eu_vehicle=True,
        )
        self.client = Client()

    def test_charges_tab_ok_for_known_vin(self):
        response = self.client.get(f"/en/personalstats/ChargeCosts/{HV_A}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Charges")
        self.assertContains(response, 'type="month"')
        year_view = self.client.get(f"/en/personalstats/ChargeCosts/{HV_A}?year=2026")
        self.assertEqual(year_view.status_code, 200)
        self.assertContains(year_view, 'name="year"')
        self.assertNotContains(year_view, 'type="month"')

    def test_charges_tab_shows_place_name_not_named_place(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        camping = ChargePlace.objects.create(
            user=self.user,
            name="Camping Frehel",
            latitude=48.62,
            longitude=-2.36,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=camping,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.0,
        )
        when = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
        for index in range(2):
            TeslaCarDataSnapshot.objects.create(
                vin="5YJ3E1EA0KFCOSTUI01",
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index * 15),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=8.0 + index,
                latitude=48.62,
                longitude=-2.36,
                battery_level=50.0,
            )
        response = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-07"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Camping Frehel")
        self.assertContains(response, "Named place")
        self.assertContains(response, "0.00")
        year_view = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?year=2026"
        )
        self.assertEqual(year_view.status_code, 200)
        self.assertContains(year_view, "Camping Frehel")
        self.assertContains(year_view, "Named place")
        self.assertContains(year_view, "data-cluster-filter")
        self.assertContains(year_view, "Everything")

    def test_charges_kpi_shows_true_zero(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        work = ChargePlace.objects.create(
            user=self.user,
            name="Office",
            latitude=50.1,
            longitude=4.2,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=work,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.0,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=work,
            role=ROLE_WORK,
            valid_from=date(2010, 1, 1),
        )
        when = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
        for index in range(2):
            TeslaCarDataSnapshot.objects.create(
                vin="5YJ3E1EA0KFCOSTUI01",
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index * 20),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=5.0 + index,
                latitude=50.1,
                longitude=4.2,
                battery_level=40.0,
            )
        response = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-03"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0.00")
        self.assertContains(response, "Work")
        self.assertNotContains(response, "Some charges in this period are not priced.")

    def test_period_odometer_delta_miles(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
        from personalstats.charge_pages import _period_odometer_delta_miles

        vin = "5YJ3E1EA0KFCOSTUI01"
        window_start = datetime(2026, 3, 1, tzinfo=CHARGE_COST_TZ)
        window_end = datetime(2026, 4, 1, tzinfo=CHARGE_COST_TZ)
        self.assertIsNone(_period_odometer_delta_miles(HV_A, window_start, window_end))

        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 3, 10),
            odometer=10000.0,
        )
        self.assertIsNone(_period_odometer_delta_miles(HV_A, window_start, window_end))

        # Stale prior (hole of weeks): do not dump the gap into this month.
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 2, 10, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 2, 10),
            odometer=9000.0,
        )
        self.assertIsNone(_period_odometer_delta_miles(HV_A, window_start, window_end))

        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 3, 28),
            odometer=10100.0,
        )
        self.assertAlmostEqual(
            _period_odometer_delta_miles(HV_A, window_start, window_end),
            100.0,
            places=4,
        )

        # Fresh prior just before the window still counts day-1 driving.
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 2, 28, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 2, 28),
            odometer=9950.0,
        )
        self.assertAlmostEqual(
            _period_odometer_delta_miles(HV_A, window_start, window_end),
            150.0,
            places=4,
        )

        TeslaCarDataSnapshot.objects.all().delete()
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 2, 20, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 2, 20),
            odometer=9800.0,
        )
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 4, 2),
            odometer=11000.0,
        )
        self.assertIsNone(_period_odometer_delta_miles(HV_A, window_start, window_end))

        TeslaCarDataSnapshot.objects.all().delete()
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 3, 2, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 3, 2),
            odometer=5000.0,
        )
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 3, 20),
            odometer=5000.0,
        )
        self.assertEqual(
            _period_odometer_delta_miles(HV_A, window_start, window_end), 0.0
        )

        TeslaCarDataSnapshot.objects.filter(
            Date__gte=datetime(2026, 3, 20, tzinfo=UTC)
        ).update(odometer=100.0)
        self.assertIsNone(_period_odometer_delta_miles(HV_A, window_start, window_end))

        # Year with an 11-month blackout before the first sample: only in-year km.
        TeslaCarDataSnapshot.objects.all().delete()
        year_start = datetime(2021, 1, 1, tzinfo=CHARGE_COST_TZ)
        year_end = datetime(2022, 1, 1, tzinfo=CHARGE_COST_TZ)
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2020, 5, 16, 19, 0, tzinfo=UTC),
            DateOnlyDay=date(2020, 5, 16),
            odometer=23233.0,
        )
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2021, 4, 29, 19, 0, tzinfo=UTC),
            DateOnlyDay=date(2021, 4, 29),
            odometer=42514.0,
        )
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2021, 12, 31, 22, 0, tzinfo=UTC),
            DateOnlyDay=date(2021, 12, 31),
            odometer=55898.0,
        )
        self.assertAlmostEqual(
            _period_odometer_delta_miles(HV_A, year_start, year_end),
            55898.0 - 42514.0,
            places=4,
        )
        self.assertLess(
            _period_odometer_delta_miles(HV_A, year_start, year_end),
            55898.0 - 23233.0,
        )

    def test_charges_kpi_distance_and_eur_per_100(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
        from matesla.units import COOKIE_NAME, MILES_TO_KM

        work = ChargePlace.objects.create(
            user=self.user,
            name="Office",
            latitude=50.1,
            longitude=4.2,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=work,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.40,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=work,
            role=ROLE_WORK,
            valid_from=date(2010, 1, 1),
        )
        vin = "5YJ3E1EA0KFCOSTUI01"
        start_mi = 10000.0
        end_mi = start_mi + 1000.0 / MILES_TO_KM
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 2, 28, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 2, 28),
            odometer=start_mi,
        )
        when = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
        for index in range(2):
            TeslaCarDataSnapshot.objects.create(
                vin=vin,
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index * 20),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=10.0 + index * 40.0,
                latitude=50.1,
                longitude=4.2,
                battery_level=40.0,
                odometer=start_mi + 50 + index,
            )
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=HV_A,
            Date=datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            DateOnlyDay=date(2026, 3, 28),
            odometer=end_mi,
        )
        response = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-03"
        )
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.context["distance"], 1000.0, places=2)
        self.assertEqual(response.context["distance_unit"], "km")
        self.assertAlmostEqual(response.context["eur_per_100"], 2.0, places=2)
        self.assertContains(response, "Driven")
        self.assertContains(response, "1000 km")
        self.assertContains(response, "€/100 km")
        self.assertContains(response, "2.00")

        french = self.client.get(
            f"/fr/personalstats/ChargeCosts/{HV_A}?month=2026-03"
        )
        self.assertEqual(french.status_code, 200)
        self.assertContains(french, "Parcourus")

        self.client.cookies[COOKIE_NAME] = "mi"
        miles_view = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-03"
        )
        self.assertEqual(miles_view.status_code, 200)
        self.assertAlmostEqual(miles_view.context["distance"], end_mi - start_mi, places=4)
        self.assertEqual(miles_view.context["distance_unit"], "mi")
        self.assertContains(miles_view, "€/100 mi")
        self.assertIsNotNone(miles_view.context["eur_per_100"])

        TeslaCarDataSnapshot.objects.filter(
            charging_state="Charging"
        ).update(latitude=51.0, longitude=5.0)
        VehiclePlaceRole.objects.all().delete()
        PlaceTariffPeriod.objects.all().delete()
        unpriced = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-03"
        )
        self.assertEqual(unpriced.status_code, 200)
        self.assertAlmostEqual(unpriced.context["distance"], end_mi - start_mi, places=4)
        self.assertIsNone(unpriced.context["eur_per_100"])

    def test_charges_onboarding_when_unpriced(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

        camping = ChargePlace.objects.create(
            user=self.user,
            name="camping cham",
            latitude=51.0,
            longitude=5.0,
            radius_m=150,
        )
        when = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
        for index in range(2):
            TeslaCarDataSnapshot.objects.create(
                vin="5YJ3E1EA0KFCOSTUI01",
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index * 15),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=11.0,
                charge_energy_added=10.0 + index,
                latitude=51.0,
                longitude=5.0,
                battery_level=40.0,
            )
        response = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-04"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Some charges in this period are not priced.")
        self.assertContains(response, "Configure rates")
        self.assertContains(response, "camping cham")
        self.assertContains(response, f"place={camping.id}")
        self.assertContains(response, "Named place")

    def _two_point_charge(self, when, lat, lon, energy=10.0):
        for index, added in enumerate((0.1, energy)):
            TeslaCarDataSnapshot.objects.create(
                vin="5YJ3E1EA0KFCOSTUI01",
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index * 15),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=7.0,
                charge_energy_added=added,
                latitude=lat,
                longitude=lon,
                battery_level=40.0,
            )

    def _cheap_work_and_dear_place(self):
        work = ChargePlace.objects.create(
            user=self.user,
            name="Office",
            latitude=WORK_LAT,
            longitude=WORK_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=work,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.10,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=work,
            role=ROLE_WORK,
            valid_from=date(2010, 1, 1),
        )
        highway = ChargePlace.objects.create(
            user=self.user,
            name="Highway stop",
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=highway,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.50,
        )

    def test_charges_table_shows_eur_per_kwh_and_outlier_colors(self):
        self._cheap_work_and_dear_place()
        for day in (3, 5, 7):
            self._two_point_charge(
                datetime(2026, 9, day, 10, 0, tzinfo=UTC), WORK_LAT, WORK_LON
            )
        self._two_point_charge(
            datetime(2026, 9, 20, 10, 0, tzinfo=UTC), ELSE_LAT, ELSE_LON
        )
        with patch(
            "personalstats.charge_pages._charges_today",
            return_value=date(2026, 10, 11),
        ):
            response = self.client.get(
                f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-09"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "€/kWh")
        self.assertContains(response, 'charges-rate--high"')
        self.assertNotContains(response, 'charges-rate--low"')
        rows = response.context["rows"]
        tones = {
            round(row["eur_per_kwh"], 2): row["rate_tone"]
            for row in rows
            if row["eur_per_kwh"] is not None
        }
        self.assertEqual(tones.get(0.5), "high")
        self.assertIsNone(tones.get(0.1))
        self.assertIsNotNone(response.context["rate_mean"])
        self.assertIsNotNone(response.context["rate_stdev"])

    def test_incomplete_month_colors_from_previous_month(self):
        self._cheap_work_and_dear_place()
        for day in (3, 5, 7):
            self._two_point_charge(
                datetime(2026, 9, day, 10, 0, tzinfo=UTC), WORK_LAT, WORK_LON
            )
        self._two_point_charge(
            datetime(2026, 9, 20, 10, 0, tzinfo=UTC), ELSE_LAT, ELSE_LON
        )
        self._two_point_charge(
            datetime(2026, 10, 2, 10, 0, tzinfo=UTC), ELSE_LAT, ELSE_LON
        )
        self._two_point_charge(
            datetime(2026, 10, 4, 10, 0, tzinfo=UTC), WORK_LAT, WORK_LON
        )
        with patch(
            "personalstats.charge_pages._charges_today",
            return_value=date(2026, 10, 11),
        ):
            response = self.client.get(
                f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-10"
            )
        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 2)
        tones = {
            round(row["eur_per_kwh"], 2): row["rate_tone"]
            for row in rows
            if row["eur_per_kwh"] is not None
        }
        self.assertEqual(tones.get(0.5), "high")
        self.assertIsNone(tones.get(0.1))

    def test_single_priced_session_has_rate_without_color(self):
        self._cheap_work_and_dear_place()
        self._two_point_charge(
            datetime(2026, 8, 12, 10, 0, tzinfo=UTC), ELSE_LAT, ELSE_LON
        )
        with patch(
            "personalstats.charge_pages._charges_today",
            return_value=date(2026, 10, 11),
        ):
            response = self.client.get(
                f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-08"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "€/kWh")
        self.assertNotContains(response, 'charges-rate--high"')
        self.assertNotContains(response, 'charges-rate--low"')
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["eur_per_kwh"], 0.50, places=2)
        self.assertIsNone(rows[0]["rate_tone"])
        self.assertIsNone(response.context["rate_mean"])

    def test_setup_requires_login_on_localhost(self):
        response = self.client.get("/en/personalstats/ChargeCostsSetup")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_setup_404_on_remote_host(self):
        self.client.login(username="cost_pages", password="x")
        response = self.client.get(
            "/en/personalstats/ChargeCostsSetup", HTTP_HOST="remote.example"
        )
        self.assertEqual(response.status_code, 404)

    def test_setup_shows_tesla_invoice_sync_status(self):
        self.client.login(username="cost_pages", password="x")
        TeslaChargingHistorySync.objects.create(
            hashed_vin=HV_A,
            last_ok_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
            last_from=datetime(2022, 6, 1, tzinfo=UTC),
            last_to=datetime(2026, 9, 13, tzinfo=UTC),
            last_error="Date range cannot exceed 1 year",
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="inv-setup-1",
            start=_at(2026, 9, 2, 15, 0),
            end=_at(2026, 9, 2, 16, 0),
            site_name="Test Supercharger",
            total_eur=10.0,
        )
        response = self.client.get(f"/en/personalstats/ChargeCostsSetup?vin={HV_A}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tesla Supercharger invoices")
        self.assertContains(response, "Date range cannot exceed 1 year")
        self.assertContains(response, "Sync Tesla invoices")

    def test_setup_sync_tesla_history_action(self):
        self.client.login(username="cost_pages", password="x")
        with patch(
            "matesla.tesla_charging_history.sync_tesla_invoices", return_value=4
        ):
            response = self.client.post(
                f"/en/personalstats/ChargeCostsSetup?vin={HV_A}",
                {
                    "action": "sync_tesla_history",
                    "vin": HV_A,
                    "hashed_vin": HV_A,
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tesla invoices updated")

    def test_setup_save_rates_and_dynamic_triggers_elia(self):
        self.client.login(username="cost_pages", password="x")
        response = self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {"action": "save_rates", "other_eur_per_kwh": "0.35"},
        )
        self.assertEqual(response.status_code, 302)
        settings = ChargeCostSettings.objects.get(user=self.user)
        self.assertAlmostEqual(settings.other_eur_per_kwh, 0.35, places=4)

        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_place",
                "name": "Test home",
                "latitude": "50.0",
                "longitude": "4.0",
                "radius_m": "150",
            },
        )
        place = ChargePlace.objects.get(user=self.user, name="Test home")
        with patch(
            "personalstats.charge_pages.ensure_spots_for_dynamic_period",
            return_value={
                "days_ok": 2,
                "days_skipped": 0,
                "days_failed": 0,
                "days_truncated": 0,
                "rows": 192,
            },
        ) as mocked:
            response = self.client.post(
                "/en/personalstats/ChargeCostsSetup",
                {
                    "action": "save_tariff",
                    "place_id": str(place.id),
                    "valid_from": "2025-11-01",
                    "mode": TARIFF_DYNAMIC,
                    "dynamic_surcharge_cents": "12",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"place={place.id}", response["Location"])
        mocked.assert_called_once()
        period = PlaceTariffPeriod.objects.get(place=place)
        self.assertEqual(period.mode, TARIFF_DYNAMIC)
        self.assertEqual(period.dynamic_surcharge_cents, 12)

    def test_new_tariff_closes_previous_open_ended(self):
        self.client.login(username="cost_pages", password="x")
        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_place",
                "name": "Uccle",
                "latitude": "50.8",
                "longitude": "4.3",
                "radius_m": "150",
            },
        )
        place = ChargePlace.objects.get(user=self.user, name="Uccle")
        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_tariff",
                "place_id": str(place.id),
                "mode": TARIFF_DAY_NIGHT,
                "day_eur_per_kwh": "0.35",
                "night_eur_per_kwh": "0.30",
            },
        )
        with patch(
            "personalstats.charge_pages.ensure_spots_for_dynamic_period",
            return_value={
                "days_ok": 1,
                "days_skipped": 0,
                "days_failed": 0,
                "days_truncated": 0,
                "rows": 96,
            },
        ):
            self.client.post(
                "/en/personalstats/ChargeCostsSetup",
                {
                    "action": "save_tariff",
                    "place_id": str(place.id),
                    "valid_from": "2025-11-01",
                    "mode": TARIFF_DYNAMIC,
                    "dynamic_surcharge_cents": "19",
                },
            )
        periods = list(place.tariff_periods.order_by("valid_from"))
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0].valid_to, date(2025, 10, 31))
        self.assertEqual(periods[1].mode, TARIFF_DYNAMIC)
        self.assertIsNone(periods[1].valid_to)

    def test_nearby_charge_cells_merge_and_tag(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
        from personalstats.charge_pages import list_charge_clusters

        vin = "5YJ3E1EA0KFCLUST001"
        when = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        for index, (lat, lon) in enumerate(
            ((50.0000, 4.0000), (50.0010, 4.0000), (51.0000, 5.0000))
        ):
            TeslaCarDataSnapshot.objects.create(
                vin=vin,
                hashedVin=HV_A,
                Date=when + timedelta(minutes=index),
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=7.0,
                latitude=lat,
                longitude=lon,
                battery_level=50.0,
            )
        clusters = list_charge_clusters(HV_A)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0]["n"], 2)
        self.client.login(username="cost_pages", password="x")
        response = self.client.post(
            f"/en/personalstats/ChargeCostsSetup?vin={HV_A}",
            {
                "action": "tag_cluster",
                "vin": HV_A,
                "hashed_vin": HV_A,
                "latitude": "50.0005",
                "longitude": "4.0000",
                "name": "Parents",
                "role": "other",
                "valid_from": "2020-01-01",
                "price_eur_per_kwh": "0.28",
            },
        )
        self.assertEqual(response.status_code, 302)
        place = ChargePlace.objects.get(user=self.user, name="Parents")
        self.assertAlmostEqual(place.latitude, 50.0005, places=3)
        tariff = place.tariff_periods.get()
        self.assertAlmostEqual(tariff.flat_eur_per_kwh, 0.28, places=4)

    def test_setup_save_flat_zero_without_dates(self):
        self.client.login(username="cost_pages", password="x")
        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_place",
                "name": "Work",
                "latitude": "50.8",
                "longitude": "4.3",
                "radius_m": "150",
            },
        )
        place = ChargePlace.objects.get(user=self.user, name="Work")
        response = self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_tariff",
                "place_id": str(place.id),
                "mode": TARIFF_FLAT,
                "flat_eur_per_kwh": "0",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        period = PlaceTariffPeriod.objects.get(place=place)
        self.assertEqual(period.valid_from, date(2010, 1, 1))
        self.assertIsNone(period.valid_to)
        self.assertAlmostEqual(period.flat_eur_per_kwh, 0.0, places=4)
        self.assertContains(response, "0 €/kWh")
        self.assertNotContains(response, "No price yet")

        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_tariff",
                "place_id": str(place.id),
                "mode": TARIFF_FLAT,
                "flat_eur_per_kwh": "0",
            },
        )
        self.assertEqual(PlaceTariffPeriod.objects.filter(place=place).count(), 1)

        empty_place = ChargePlace.objects.create(
            user=self.user, name="bvd", latitude=50.81, longitude=4.31, radius_m=150
        )
        empty = self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_tariff",
                "place_id": str(empty_place.id),
                "mode": TARIFF_FLAT,
            },
            follow=True,
        )
        self.assertEqual(empty.status_code, 200)
        self.assertFalse(PlaceTariffPeriod.objects.filter(place=empty_place).exists())
        self.assertContains(empty, "Enter a price. 0 is allowed.")

        camp = ChargePlace.objects.create(
            user=self.user, name="camping cham", latitude=45.9, longitude=6.8, radius_m=150
        )
        self.client.post(
            "/en/personalstats/ChargeCostsSetup",
            {
                "action": "save_tariff",
                "place_id": str(camp.id),
                "mode": TARIFF_PER_DAY,
                "eur_per_day": "15",
            },
            follow=True,
        )
        daily = PlaceTariffPeriod.objects.get(place=camp)
        self.assertEqual(daily.mode, TARIFF_PER_DAY)
        self.assertAlmostEqual(daily.eur_per_day, 15.0, places=4)

    def test_rule_labels_follow_active_language(self):
        from django.utils import translation
        from personalstats.charge_pages import _rule_label, _source_label

        with translation.override("fr"):
            self.assertEqual(
                _rule_label("supercharger_rate"), "Tarif moyen Superchargeur"
            )
            self.assertEqual(_rule_label("home_dynamic"), "Maison (dynamique)")
            self.assertEqual(
                _rule_label("supercharger_invoice", "Drogenbos"), "Drogenbos"
            )
            self.assertEqual(
                _source_label("supercharger_invoice"),
                "Facture Superchargeur Tesla",
            )
            self.assertEqual(_source_label("unpriced"), "Non chiffré")
            self.assertEqual(_source_label("per_day"), "Forfait / jour")
        with translation.override("en"):
            self.assertEqual(
                _rule_label("supercharger_rate"), "Supercharger average rate"
            )

    def test_unpriced_cluster_list_skips_zero_kwh(self):
        from personalstats.charge_pages import _unpriced_clusters

        rows = [
            {
                "cluster_key": "p5",
                "cluster_name": "Uccle",
                "kwh": None,
                "cost_eur": None,
                "status": COST_UNPRICED,
                "rule": RULE_UNPRICED,
            },
            {
                "cluster_key": "p7",
                "cluster_name": "delhaize boondael",
                "kwh": 339.0,
                "cost_eur": None,
                "status": COST_UNPRICED,
                "rule": RULE_PLACE,
                "place_id": 7,
            },
        ]
        listed = _unpriced_clusters(rows)
        names = [item["name"] for item in listed]
        self.assertEqual(names, ["delhaize boondael"])
        self.assertEqual(listed[0]["place_id"], 7)

    def test_cluster_identity_labels_supercharger_sites(self):
        from matesla.charge_cost import ChargeSession, CostResult
        from personalstats.charge_pages import _cluster_identity

        session = ChargeSession(
            hashed_vin=HV_A,
            start=_at(2024, 2, 20, 11, 44),
            end=_at(2024, 2, 20, 11, 56),
            kwh=29.0,
            lat=51.2,
            lon=3.2,
            tesla_site_name="Brugge, Belgium",
        )
        result = CostResult(8.7, COST_PRICED, RULE_SUPERCHARGER_RATE)
        _key, name = _cluster_identity(session, result, {})
        self.assertIn("Supercharger", name)
        self.assertIn("Brugge", name)
        tagged = _cluster_identity(
            session, CostResult(0, COST_PRICED, RULE_PLACE, place_id=9), {9: "tesla drogenbos"}
        )
        self.assertEqual(tagged[1], "tesla drogenbos")


class TeslaChargingHistoryTests(TestCase):
    def test_fees_total_sums_charging_and_idle(self):
        from matesla.tesla_charging_history import fees_total_eur, history_records

        total, currency = fees_total_eur(
            [
                {
                    "feeType": "CHARGING",
                    "currencyCode": "EUR",
                    "totalDue": 12.40,
                    "usageBase": 40.0,
                    "uom": "kwh",
                },
                {"feeType": "PARKING", "currencyCode": "EUR", "totalDue": 1.00},
            ]
        )
        self.assertAlmostEqual(total, 13.40, places=2)
        self.assertEqual(currency, "EUR")
        usd, _ = fees_total_eur(
            [{"feeType": "CHARGING", "currencyCode": "USD", "totalDue": 18.5}]
        )
        self.assertIsNone(usd)
        rows = history_records(
            {"response": {"data": [{"sessionId": 1}, {"sessionId": 2}]}}
        )
        self.assertEqual(len(rows), 2)

    def test_cached_invoice_beats_average_supercharger_rate(self):
        from matesla.tesla_charging_history import apply_tesla_invoices

        User = get_user_model()
        user = User.objects.create_user("sc_hist", password="x")
        ChargeCostSettings.objects.create(
            user=user, other_eur_per_kwh=0.40, supercharger_eur_per_kwh=0.55
        )
        start = _at(2026, 9, 2, 15, 43)
        end = _at(2026, 9, 2, 16, 11)
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="sc-drogenbos-1",
            start=start,
            end=end,
            site_name="Drogenbos Supercharger",
            total_eur=11.20,
            kwh=45.86,
            currency="EUR",
        )
        session = ChargeSession(
            hashed_vin=HV_A,
            start=start,
            end=end,
            kwh=45.86,
            lat=ELSE_LAT,
            lon=ELSE_LON,
            is_supercharger=True,
        )
        apply_tesla_invoices([session])
        self.assertAlmostEqual(session.tesla_invoice_eur, 11.20, places=2)
        self.assertEqual(session.tesla_site_name, "Drogenbos Supercharger")
        result = price_session(session)
        self.assertEqual(result.rule, RULE_SUPERCHARGER_INVOICE)
        self.assertAlmostEqual(result.cost_eur, 11.20, places=2)

    def test_two_tesla_invoices_on_one_stop_are_summed(self):
        from matesla.tesla_charging_history import apply_tesla_invoices

        User = get_user_model()
        user = User.objects.create_user("sc_split", password="x")
        ChargeCostSettings.objects.create(
            user=user, other_eur_per_kwh=0.40, supercharger_eur_per_kwh=0.55
        )
        start = _at(2026, 9, 13, 9, 26)
        end = _at(2026, 9, 13, 10, 11)
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="anderlecht-a",
            start=_at(2026, 9, 13, 9, 26),
            end=_at(2026, 9, 13, 9, 45),
            site_name="Anderlecht, Belgium",
            total_eur=4.00,
            currency="EUR",
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="anderlecht-b",
            start=_at(2026, 9, 13, 9, 47),
            end=_at(2026, 9, 13, 10, 10),
            site_name="Anderlecht, Belgium",
            total_eur=7.69,
            currency="EUR",
        )
        session = ChargeSession(
            hashed_vin=HV_A,
            start=start,
            end=end,
            kwh=29.58,
            lat=ELSE_LAT,
            lon=ELSE_LON,
            is_supercharger=True,
        )
        apply_tesla_invoices([session])
        self.assertAlmostEqual(session.tesla_invoice_eur, 11.69, places=2)
        self.assertEqual(session.tesla_site_name, "Anderlecht, Belgium")

    def test_invoice_overlap_pad_does_not_glue_next_stall(self):
        from matesla.tesla_charging_history import match_invoices

        first = TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="anderlecht-stall-a",
            start=_at(2026, 9, 13, 9, 26),
            end=_at(2026, 9, 13, 9, 32),
            site_name="Anderlecht, Belgium",
            total_eur=4.00,
            currency="EUR",
        )
        second = TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="anderlecht-stall-b",
            start=_at(2026, 9, 13, 9, 47),
            end=_at(2026, 9, 13, 10, 12),
            site_name="Anderlecht, Belgium",
            total_eur=7.69,
            currency="EUR",
        )
        invoices = [first, second]
        matched_first = match_invoices(
            _at(2026, 9, 13, 9, 26),
            _at(2026, 9, 13, 9, 32),
            invoices,
        )
        self.assertEqual([inv.tesla_session_id for inv in matched_first], ["anderlecht-stall-a"])
        matched_second = match_invoices(
            _at(2026, 9, 13, 9, 50),
            _at(2026, 9, 13, 10, 11),
            invoices,
        )
        self.assertEqual(
            [inv.tesla_session_id for inv in matched_second], ["anderlecht-stall-b"]
        )

    def test_history_chunks_keep_padded_calls_under_one_year(self):
        from matesla.tesla_charging_history import (
            MAX_API_SPAN,
            history_chunks,
            padded_api_bounds,
        )

        start = datetime(2024, 1, 1, tzinfo=CHARGE_COST_TZ)
        end = datetime(2025, 1, 1, tzinfo=CHARGE_COST_TZ)
        chunks = history_chunks(start, end)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk_start, chunk_end in chunks:
            fetch_from, fetch_to = padded_api_bounds(chunk_start, chunk_end)
            self.assertLessEqual(fetch_to - fetch_from, MAX_API_SPAN)

    def test_year_window_issues_multiple_sub_year_requests(self):
        from matesla.tesla_charging_history import (
            MAX_API_SPAN,
            ensure_tesla_invoices_for_window,
        )

        token = MagicMock()
        token.access_token = "tok"
        calls = []

        def http_get(url, **kwargs):
            calls.append(kwargs["params"])
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.json.return_value = {"response": {"data": []}}
            return resp

        with patch(
            "matesla.tesla_charging_history._vin_and_token",
            return_value=("5YJTESTVIN0000001", token),
        ), patch(
            "matesla.TeslaOAuth.ensure_fresh_access_token",
            return_value=token,
        ):
            ensure_tesla_invoices_for_window(
                HV_A,
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2025, 1, 1, tzinfo=UTC),
                http_get=http_get,
                force=True,
                backfill=False,
            )
        self.assertGreaterEqual(len(calls), 2)
        for params in calls:
            start = datetime.fromisoformat(params["startTime"])
            end = datetime.fromisoformat(params["endTime"])
            self.assertLessEqual(end - start, MAX_API_SPAN)

    def test_last_error_cleared_after_successful_chunk(self):
        from matesla.tesla_charging_history import ensure_tesla_invoices_for_window

        TeslaChargingHistorySync.objects.create(
            hashed_vin=HV_A,
            last_error="charging/history HTTP 400: Date range cannot exceed 1 year",
        )
        token = MagicMock()
        token.access_token = "tok"

        def http_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.json.return_value = {"response": {"data": []}}
            return resp

        with patch(
            "matesla.tesla_charging_history._vin_and_token",
            return_value=("5YJTESTVIN0000001", token),
        ), patch(
            "matesla.TeslaOAuth.ensure_fresh_access_token",
            return_value=token,
        ):
            ensure_tesla_invoices_for_window(
                HV_A,
                datetime(2026, 9, 1, tzinfo=UTC),
                datetime(2026, 9, 30, tzinfo=UTC),
                http_get=http_get,
                force=True,
                backfill=False,
            )
        sync = TeslaChargingHistorySync.objects.get(hashed_vin=HV_A)
        self.assertEqual(sync.last_error, "")
        self.assertIsNotNone(sync.last_ok_at)

    def test_pagination_continues_past_six_pages(self):
        from matesla.tesla_charging_history import fetch_charging_history

        pages = []

        def http_get(url, **kwargs):
            page = kwargs["params"]["pageNo"]
            pages.append(page)
            n = 50 if page < 8 else 3
            rows = [
                {
                    "sessionId": f"{page}-{index}",
                    "chargeStartDateTime": "2026-01-01T10:00:00Z",
                    "fees": [{"totalDue": 1.0, "currencyCode": "EUR"}],
                }
                for index in range(n)
            ]
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.json.return_value = {"response": {"data": rows}}
            return resp

        records = fetch_charging_history(
            "tok",
            "5YJTESTVIN0000001",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
            http_get=http_get,
        )
        self.assertEqual(pages, list(range(1, 9)))
        self.assertEqual(len(records), 50 * 7 + 3)

    def test_sync_starts_from_oldest_charge_snapshot(self):
        from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
        from matesla.tesla_charging_history import sync_tesla_invoices

        TeslaCarDataSnapshot.objects.create(
            vin="5YJTESTVIN0000001",
            hashedVin=HV_A,
            Date=datetime(2022, 6, 15, 8, 0, tzinfo=UTC),
            DateOnlyDay=date(2022, 6, 15),
            charging_state="Charging",
            charger_power=7.0,
            latitude=50.0,
            longitude=4.0,
        )
        token = MagicMock()
        token.access_token = "tok"
        starts = []

        def http_get(url, **kwargs):
            starts.append(datetime.fromisoformat(kwargs["params"]["startTime"]))
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.json.return_value = {"response": {"data": []}}
            return resp

        with patch(
            "matesla.tesla_charging_history._vin_and_token",
            return_value=("5YJTESTVIN0000001", token),
        ), patch(
            "matesla.TeslaOAuth.ensure_fresh_access_token",
            return_value=token,
        ):
            sync_tesla_invoices(HV_A, http_get=http_get, force=True)
        self.assertTrue(starts)
        self.assertLessEqual(min(starts).year, 2022)
        self.assertGreaterEqual(len(starts), 3)

    def _dc_snaps(self, start, n=4, step_min=10, power=150.0):
        for index in range(n):
            when = start + timedelta(minutes=step_min * index)
            TeslaCarDataSnapshot.objects.create(
                vin="5YJTESTVIN0000001",
                hashedVin=HV_A,
                Date=when,
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=power,
                fast_charger_present=True,
                fast_charger_type="Tesla",
                charge_energy_added=5.0 + index * 12.0,
                latitude=ELSE_LAT,
                longitude=ELSE_LON,
            )

    def _invoice_http(self, total=12.79, start_iso="", stop_iso=""):
        def http_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = ""
            resp.json.return_value = {
                "response": {
                    "data": [
                        {
                            "sessionId": "sc-live-1",
                            "chargeStartDateTime": start_iso,
                            "chargeStopDateTime": stop_iso,
                            "siteLocationName": "Anderlecht, Belgium",
                            "fees": [
                                {
                                    "feeType": "CHARGING",
                                    "currencyCode": "EUR",
                                    "totalDue": total,
                                    "usageBase": 60.91,
                                    "uom": "kwh",
                                }
                            ],
                        }
                    ]
                }
            }
            return resp

        return http_get

    def test_daymap_uses_cached_tesla_invoice_without_opening_charges(self):
        User = get_user_model()
        user = User.objects.create_user("sc_daymap", password="x")
        ChargeCostSettings.objects.create(
            user=user, other_eur_per_kwh=0.40, supercharger_eur_per_kwh=0.30
        )
        start = _at(2026, 10, 2, 9, 50)
        end = _at(2026, 10, 2, 10, 37)
        charges = [
            {
                "start": start,
                "end": end,
                "kwh_added": 56.84,
                "lat": ELSE_LAT,
                "lon": ELSE_LON,
                "is_supercharger": True,
            }
        ]
        annotate_daymap_charges(HV_A, charges)
        self.assertAlmostEqual(charges[0]["cost_eur"], 56.84 * 0.30, places=2)
        self.assertEqual(charges[0]["cost_rule"], RULE_SUPERCHARGER_RATE)

        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="anderlecht-today",
            start=start,
            end=end,
            site_name="Anderlecht, Belgium",
            total_eur=12.79,
            kwh=60.91,
            currency="EUR",
        )
        annotate_daymap_charges(HV_A, charges)
        self.assertAlmostEqual(charges[0]["cost_eur"], 12.79, places=2)
        self.assertEqual(charges[0]["cost_rule"], RULE_SUPERCHARGER_INVOICE)
        stored = ChargeSessionCost.objects.get(hashed_vin=HV_A, start=start.astimezone(UTC))
        self.assertAlmostEqual(stored.tesla_invoice_eur, 12.79, places=2)

    def test_capture_skips_tesla_history_while_supercharge_is_live(self):
        from matesla.tesla_charging_history import maybe_refresh_invoice_after_supercharge

        now = _at(2026, 10, 2, 10, 20)
        self._dc_snaps(_at(2026, 10, 2, 9, 50), n=4, step_min=10)

        def http_get(*args, **kwargs):
            raise AssertionError("must not fetch Tesla history while still Supercharging")

        info = maybe_refresh_invoice_after_supercharge(
            HV_A, now=now, http_get=http_get
        )
        self.assertEqual(info["status"], "skipped_live")
        self.assertEqual(TeslaChargingInvoice.objects.count(), 0)

    def test_capture_skips_tesla_history_for_ac_wall(self):
        from matesla.tesla_charging_history import maybe_refresh_invoice_after_supercharge

        start = _at(2026, 10, 2, 1, 0)
        for index in range(3):
            when = start + timedelta(minutes=15 * index)
            TeslaCarDataSnapshot.objects.create(
                vin="5YJTESTVIN0000001",
                hashedVin=HV_A,
                Date=when,
                DateOnlyDay=when.date(),
                charging_state="Charging",
                charger_power=11.0,
                fast_charger_present=False,
                fast_charger_type="MCSingleWireCAN",
                charge_energy_added=2.0 + index,
                latitude=HOME_LAT,
                longitude=HOME_LON,
            )

        def http_get(*args, **kwargs):
            raise AssertionError("must not fetch Tesla history for AC wall")

        info = maybe_refresh_invoice_after_supercharge(
            HV_A, now=_at(2026, 10, 2, 2, 0), http_get=http_get
        )
        self.assertEqual(info["status"], "skipped_none")

    def test_capture_fetches_invoice_after_supercharge_unplug(self):
        from matesla.tesla_charging_history import maybe_refresh_invoice_after_supercharge

        start = _at(2026, 10, 2, 9, 50)
        self._dc_snaps(start, n=5, step_min=10)
        unplug = _at(2026, 10, 2, 10, 40)
        TeslaCarDataSnapshot.objects.create(
            vin="5YJTESTVIN0000001",
            hashedVin=HV_A,
            Date=unplug,
            DateOnlyDay=unplug.date(),
            charging_state="Disconnected",
            charger_power=0.0,
            fast_charger_present=False,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        token = MagicMock()
        token.access_token = "tok"
        http_get = self._invoice_http(
            total=12.79,
            start_iso="2026-10-02T07:50:00+00:00",
            stop_iso="2026-10-02T08:37:00+00:00",
        )
        with patch(
            "matesla.tesla_charging_history._vin_and_token",
            return_value=("5YJTESTVIN0000001", token),
        ), patch(
            "matesla.TeslaOAuth.ensure_fresh_access_token",
            return_value=token,
        ):
            info = maybe_refresh_invoice_after_supercharge(
                HV_A, now=_at(2026, 10, 2, 10, 45), http_get=http_get
            )
        self.assertEqual(info["status"], "fetched")
        self.assertAlmostEqual(info["cost_eur"], 12.79, places=2)
        stored = ChargeSessionCost.objects.get(hashed_vin=HV_A)
        self.assertEqual(stored.rule, RULE_SUPERCHARGER_INVOICE)
        self.assertAlmostEqual(stored.cost_eur, 12.79, places=2)

    def test_capture_matches_local_invoice_without_http(self):
        from matesla.tesla_charging_history import maybe_refresh_invoice_after_supercharge

        start = _at(2026, 10, 2, 9, 50)
        self._dc_snaps(start, n=5, step_min=10)
        TeslaCarDataSnapshot.objects.create(
            vin="5YJTESTVIN0000001",
            hashedVin=HV_A,
            Date=_at(2026, 10, 2, 10, 40),
            DateOnlyDay=date(2026, 10, 2),
            charging_state="Disconnected",
            charger_power=0.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="already-cached",
            start=start,
            end=_at(2026, 10, 2, 10, 37),
            site_name="Anderlecht, Belgium",
            total_eur=12.79,
            currency="EUR",
        )

        def http_get(*args, **kwargs):
            raise AssertionError("local cache must not hit Tesla")

        info = maybe_refresh_invoice_after_supercharge(
            HV_A, now=_at(2026, 10, 2, 10, 45), http_get=http_get
        )
        self.assertEqual(info["status"], "matched_local")
        self.assertAlmostEqual(info["cost_eur"], 12.79, places=2)

    def test_capture_respects_invoice_retry_cooldown(self):
        from matesla.tesla_charging_history import maybe_refresh_invoice_after_supercharge

        start = _at(2026, 10, 2, 9, 50)
        self._dc_snaps(start, n=5, step_min=10)
        TeslaCarDataSnapshot.objects.create(
            vin="5YJTESTVIN0000001",
            hashedVin=HV_A,
            Date=_at(2026, 10, 2, 10, 40),
            DateOnlyDay=date(2026, 10, 2),
            charging_state="Disconnected",
            charger_power=0.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        TeslaChargingHistorySync.objects.create(
            hashed_vin=HV_A,
            last_ok_at=_at(2026, 10, 2, 10, 42).astimezone(UTC),
        )

        def http_get(*args, **kwargs):
            raise AssertionError("cooldown must skip Tesla history")

        info = maybe_refresh_invoice_after_supercharge(
            HV_A, now=_at(2026, 10, 2, 10, 45), http_get=http_get
        )
        self.assertEqual(info["status"], "skipped_cooldown")


class ChargesShippedPathTests(TestCase):
    """Drive iter_sessions / price_sessions / Charges GET — not a reimplementation."""

    VIN = "5YJ3E1EA0KFCOSTUI01"

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("charges_path", password="x")
        from matesla.models.TeslaCarInfo import TeslaCarInfo

        TeslaCarInfo.objects.create(
            vin=self.VIN,
            hashedVin=HV_A,
            Date=date(2024, 1, 1),
            LastSeenDate=date(2024, 1, 1),
            car_type="model3",
            charge_port_type="CCS",
            exterior_color="Black",
            has_air_suspension=False,
            has_ludicrous_mode=False,
            motorized_charge_port=True,
            rear_seat_heaters="1",
            rhd=False,
            roof_color="Glass",
            wheel_type="Pinwheel18",
            eu_vehicle=True,
        )
        self.home = ChargePlace.objects.create(
            user=self.user,
            name="Home path",
            latitude=HOME_LAT,
            longitude=HOME_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=self.home,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.20,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=self.home,
            role=ROLE_HOME,
            valid_from=date(2010, 1, 1),
        )
        ChargeCostSettings.objects.create(
            user=self.user,
            supercharger_eur_per_kwh=0.40,
        )
        self.client = Client()

    def _snap(self, when, **fields):
        defaults = dict(
            vin=self.VIN,
            hashedVin=HV_A,
            Date=when,
            DateOnlyDay=when.date(),
            battery_level=50.0,
            charger_power=7.0,
            charging_state="Charging",
        )
        defaults.update(fields)
        return TeslaCarDataSnapshot.objects.create(**defaults)

    def test_idle_zero_between_plugs_keeps_second_peak_kwh(self):
        """NoPower 0 after a drive must be the baseline, not the previous peak.

        Tesla often starts the next Supercharge around 40 kWh; that is not a
        50 % drop from a 46 kWh previous stop, so a charging-only prior would
        subtract and show ~15 kWh instead of ~60.
        """
        self._snap(
            _at(2026, 9, 2, 15, 43),
            charge_energy_added=0.4,
            charger_power=90.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 9, 2, 16, 11),
            charge_energy_added=45.9,
            charger_power=90.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 9, 2, 23, 11),
            charging_state="NoPower",
            charger_power=0.0,
            charge_energy_added=0.0,
            latitude=50.45,
            longitude=3.95,
        )
        self._snap(
            _at(2026, 9, 3, 0, 32),
            charge_energy_added=40.52,
            charger_power=78.0,
            latitude=50.45,
            longitude=3.95,
        )
        self._snap(
            _at(2026, 9, 3, 0, 56),
            charge_energy_added=60.46,
            charger_power=32.0,
            latitude=50.45,
            longitude=3.95,
        )
        sessions = iter_sessions_starting_in(
            HV_A,
            datetime(2026, 9, 1, tzinfo=CHARGE_COST_TZ),
            datetime(2026, 10, 1, tzinfo=CHARGE_COST_TZ),
        )
        self.assertEqual(len(sessions), 2)
        self.assertAlmostEqual(sessions[0].kwh, 45.9, places=1)
        self.assertAlmostEqual(sessions[1].kwh, 60.46, places=1)

    def test_null_energy_before_plug_keeps_peak_kwh(self):
        """Unknown/NULL immediately before a plug is the LIMIT-1 baseline.

        Walking back past it to a leftover 0.72 would subtract (33.5 − 0.72).
        """
        self._snap(
            _at(2026, 7, 3, 12, 0),
            charge_energy_added=0.72,
            charging_state="Disconnected",
            charger_power=0.0,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            _at(2026, 7, 3, 22, 59),
            charge_energy_added=None,
            charging_state="Unknown",
            charger_power=None,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            _at(2026, 7, 4, 1, 0),
            charge_energy_added=0.74,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            _at(2026, 7, 4, 1, 20),
            charge_energy_added=33.5,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        sessions = iter_sessions_starting_in(
            HV_A,
            datetime(2026, 7, 1, tzinfo=CHARGE_COST_TZ),
            datetime(2026, 8, 1, tzinfo=CHARGE_COST_TZ),
        )
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].kwh, 33.5, places=1)

    def test_prior_older_than_expand_pad_still_subtracts(self):
        """First session of a window: baseline may sit before the 16 h pad."""
        window_start = datetime(2026, 9, 1, tzinfo=CHARGE_COST_TZ)
        window_end = datetime(2026, 9, 2, tzinfo=CHARGE_COST_TZ)
        self._snap(
            window_start - timedelta(hours=20),
            charge_energy_added=10.0,
            charging_state="Disconnected",
            charger_power=0.0,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            window_start + timedelta(minutes=5),
            charge_energy_added=12.0,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            window_start + timedelta(minutes=25),
            charge_energy_added=22.0,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        sessions = iter_sessions_starting_in(HV_A, window_start, window_end)
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0].kwh, 12.0, places=1)

    def test_two_plugs_split_on_counter_reset_under_30_min(self):
        """Same stall, 16 min gap, new cable: two sessions, kWh is each plug."""
        self._snap(
            _at(2026, 9, 13, 9, 26),
            charge_energy_added=0.5,
            charger_power=70.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 9, 13, 9, 32),
            charge_energy_added=14.9,
            charger_power=70.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 9, 13, 9, 50),
            charge_energy_added=0.4,
            charger_power=90.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 9, 13, 10, 12),
            charge_energy_added=30.86,
            charger_power=90.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="stall-a",
            start=_at(2026, 9, 13, 9, 26),
            end=_at(2026, 9, 13, 9, 32),
            site_name="Anderlecht Supercharger",
            total_eur=4.00,
            currency="EUR",
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="stall-b",
            start=_at(2026, 9, 13, 9, 47),
            end=_at(2026, 9, 13, 10, 12),
            site_name="Anderlecht Supercharger",
            total_eur=7.69,
            currency="EUR",
        )
        window_start = datetime(2026, 9, 1, tzinfo=CHARGE_COST_TZ)
        window_end = datetime(2026, 10, 1, tzinfo=CHARGE_COST_TZ)
        sessions = iter_sessions_starting_in(HV_A, window_start, window_end)
        self.assertEqual(len(sessions), 2)
        self.assertAlmostEqual(sessions[0].kwh, 14.9, places=1)
        self.assertAlmostEqual(sessions[1].kwh, 30.86, places=1)

        priced = price_sessions_starting_in(HV_A, window_start, window_end)
        self.assertEqual(len(priced), 2)
        self.assertEqual(priced[0][1].rule, RULE_SUPERCHARGER_INVOICE)
        self.assertEqual(priced[1][1].rule, RULE_SUPERCHARGER_INVOICE)
        self.assertAlmostEqual(priced[0][1].cost_eur, 4.00, places=2)
        self.assertAlmostEqual(priced[1][1].cost_eur, 7.69, places=2)
        n_before = ChargeSessionCost.objects.filter(hashed_vin=HV_A).count()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            priced_again = price_sessions_starting_in(HV_A, window_start, window_end)
        self.assertLess(
            len(captured),
            40,
            "second price_sessions must not round-trip per session",
        )
        self.assertEqual(len(priced_again), 2)
        self.assertAlmostEqual(priced_again[0][1].cost_eur, 4.00, places=2)
        self.assertAlmostEqual(priced_again[1][1].cost_eur, 7.69, places=2)
        self.assertEqual(
            ChargeSessionCost.objects.filter(hashed_vin=HV_A).count(), n_before
        )

    def test_charges_get_home_work_unpriced_and_invoice(self):
        self._snap(
            _at(2026, 8, 2, 22, 0),
            charge_energy_added=0.2,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        self._snap(
            _at(2026, 8, 2, 22, 20),
            charge_energy_added=10.0,
            latitude=HOME_LAT,
            longitude=HOME_LON,
        )
        work = ChargePlace.objects.create(
            user=self.user,
            name="Office path",
            latitude=WORK_LAT,
            longitude=WORK_LON,
            radius_m=150,
        )
        PlaceTariffPeriod.objects.create(
            place=work,
            valid_from=date(2010, 1, 1),
            mode=TARIFF_FLAT,
            flat_eur_per_kwh=0.0,
        )
        VehiclePlaceRole.objects.create(
            hashed_vin=HV_A,
            place=work,
            role=ROLE_WORK,
            valid_from=date(2010, 1, 1),
        )
        self._snap(
            _at(2026, 8, 10, 12, 0),
            charge_energy_added=0.1,
            latitude=WORK_LAT,
            longitude=WORK_LON,
        )
        self._snap(
            _at(2026, 8, 10, 12, 20),
            charge_energy_added=5.0,
            latitude=WORK_LAT,
            longitude=WORK_LON,
        )
        self._snap(
            _at(2026, 8, 20, 15, 0),
            charge_energy_added=0.1,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
            charger_power=11.0,
        )
        self._snap(
            _at(2026, 8, 20, 15, 20),
            charge_energy_added=8.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
            charger_power=11.0,
        )
        TeslaChargingInvoice.objects.create(
            hashed_vin=HV_A,
            tesla_session_id="inv-aug",
            start=_at(2026, 8, 25, 9, 0),
            end=_at(2026, 8, 25, 9, 20),
            site_name="Test Supercharger",
            total_eur=6.50,
            currency="EUR",
        )
        self._snap(
            _at(2026, 8, 25, 9, 0),
            charge_energy_added=0.3,
            charger_power=80.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )
        self._snap(
            _at(2026, 8, 25, 9, 20),
            charge_energy_added=20.0,
            charger_power=80.0,
            latitude=ELSE_LAT,
            longitude=ELSE_LON,
        )

        month = self.client.get(f"/en/personalstats/ChargeCosts/{HV_A}?month=2026-08")
        self.assertEqual(month.status_code, 200)
        rows = month.context["rows"]
        self.assertEqual(len(rows), 4)
        by_rule = {row["rule"]: row for row in rows}
        self.assertIn(RULE_HOME_FLAT, by_rule)
        self.assertAlmostEqual(by_rule[RULE_HOME_FLAT]["cost_eur"], 2.0, places=2)
        self.assertEqual(by_rule[RULE_HOME_FLAT]["source_label"], "Home (flat)")
        self.assertIn(RULE_WORK, by_rule)
        self.assertAlmostEqual(by_rule[RULE_WORK]["cost_eur"], 0.0, places=2)
        self.assertIn(RULE_SUPERCHARGER_INVOICE, by_rule)
        self.assertAlmostEqual(
            by_rule[RULE_SUPERCHARGER_INVOICE]["cost_eur"], 6.50, places=2
        )
        self.assertIn(RULE_UNPRICED, by_rule)
        self.assertIsNone(by_rule[RULE_UNPRICED]["cost_eur"])
        self.assertContains(month, "2.00")
        self.assertContains(month, "6.50")
        year = self.client.get(f"/en/personalstats/ChargeCosts/{HV_A}?year=2026")
        self.assertEqual(year.status_code, 200)
        self.assertEqual(len(year.context["rows"]), 4)
        self.assertAlmostEqual(year.context["total_eur"], 8.50, places=2)

