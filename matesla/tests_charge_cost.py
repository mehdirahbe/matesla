"""Charge-cost engine, Elia cache, and rule priority (no live HTTP / no real VINs)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from matesla.charge_cost import (
    CHARGE_COST_TZ,
    ChargeSession,
    annotate_daymap_charges,
    price_and_persist,
    price_session,
)
from matesla.elia_dayahead import fetch_elia_day_ahead, store_spot_rows
from matesla.models.ChargeCost import (
    COST_PARTIAL,
    COST_PRICED,
    COST_UNPRICED,
    ChargeCostSettings,
    ChargePlace,
    ChargeSessionCost,
    DayAheadSpotPrice,
    PlaceTariffPeriod,
    TeslaChargingInvoice,
    ROLE_HOME,
    ROLE_WORK,
    RULE_HOME_DAY_NIGHT,
    RULE_HOME_DYNAMIC,
    RULE_HOME_FLAT,
    RULE_OTHER,
    RULE_PLACE,
    RULE_SUPERCHARGER_INVOICE,
    RULE_SUPERCHARGER_RATE,
    RULE_UNPRICED,
    RULE_WORK,
    TARIFF_DAY_NIGHT,
    TARIFF_DYNAMIC,
    TARIFF_FLAT,
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
        self.assertNotContains(response, "Named place")
        year_view = self.client.get(
            f"/en/personalstats/ChargeCosts/{HV_A}?year=2026"
        )
        self.assertEqual(year_view.status_code, 200)
        self.assertContains(year_view, "Camping Frehel")
        self.assertContains(year_view, "data-cluster-filter")
        self.assertContains(year_view, "Everything")

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

        # Placeholder 0: the field looks filled but POST may omit it or send "".
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
        blank = PlaceTariffPeriod.objects.get(place=empty_place)
        self.assertAlmostEqual(blank.flat_eur_per_kwh, 0.0, places=4)
        self.assertContains(empty, "0 €/kWh")
        self.assertNotContains(empty, "Enter a price. 0 is allowed.")

    def test_rule_labels_follow_active_language(self):
        from django.utils import translation
        from personalstats.charge_pages import _rule_label

        with translation.override("fr"):
            self.assertEqual(
                _rule_label("supercharger_rate"), "Tarif moyen Superchargeur"
            )
            self.assertEqual(_rule_label("home_dynamic"), "Maison (dynamique)")
            self.assertEqual(
                _rule_label("supercharger_invoice", "Drogenbos"), "Drogenbos"
            )
        with translation.override("en"):
            self.assertEqual(
                _rule_label("supercharger_rate"), "Supercharger average rate"
            )

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
