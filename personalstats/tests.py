"""
Personal stats URL + graph tests.

Uses Django TestCase → isolated test DB only (test_matesla.sqlite3).
Fake telemetry is seeded via test_factories — never touches db.sqlite3.
"""

from django.test import Client, TestCase
from django.db import connection

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from mysite.test_helpers import configured_language_codes
from personalstats.test_factories import (
    FAKE_HASHED_VIN,
    FAKE_VIN,
    assert_not_production_database,
    seed_fake_car_telemetry,
)
from personalstats.urls import urlpatterns

# Fields exercised by StatsOnCarGraph (incl. computed / histogram keys)
STATS_ON_CAR_GRAPH_FIELDS = (
    "outside_temp",
    "inside_temp",
    "odometer",
    "speed",
    "power",
    "battery_level",
    "battery_range",
    "charge_limit_soc",
    "charge_rate",
    "charger_power",
    "est_battery_range",
    "usable_battery_level",
    "battery_degradation",
    "range_at_100",
    "efficiency_by_speed",
    "efficiency_by_temp",
    "fleet_poll_cost",
)

BATTERY_DEGRADATION_FIELDS = (
    "odometer",
    "range_at_100_odometer",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class DatabaseIsolationTests(TestCase):
    """Safety: tests must never open production db.sqlite3."""

    def test_not_using_production_sqlite(self):
        assert_not_production_database()
        name = str(connection.settings_dict["NAME"])
        self.assertIn("test", name.lower())


class PersonalStatsUrlTests(TestCase):
    def test_has_url(self):
        self.assertGreaterEqual(
            len(urlpatterns), 1, "urlpatterns in personalstats.urls is empty"
        )

    def test_bogus_url_fails(self):
        client = Client()
        for lang in configured_language_codes():
            response = client.get(
                f"/{lang}/personalstats/StatsOnCarGraph/fakesha/dontexist/5"
            )
            self.assertEqual(
                response.status_code, 404, "bogus field should 404"
            )
            response = client.get(f"/{lang}/personalstats/Stats/--")
            self.assertEqual(
                response.status_code, 404, "SQL-injection-ish hash should 404"
            )

    def test_invalid_hash_rejected_on_all_personal_routes(self):
        """Path tokens must pass IsValidHash — no SQL/path smuggling."""
        client = Client()
        bad = "--"
        paths = (
            f"/en/personalstats/Stats/{bad}",
            f"/en/personalstats/DayMap/{bad}",
            f"/en/personalstats/DayMap/{bad}/2024-01-15",
            f"/en/personalstats/DayChargeSessionGraph/{bad}/2024-01-15/1700000000/power_vs_time",
            f"/en/personalstats/Drives/{bad}",
            f"/en/personalstats/DCCharge/{bad}",
            f"/en/personalstats/DCChargeGraph/{bad}/power_vs_soc/52",
            f"/en/personalstats/PollDetails/{bad}",
            f"/en/personalstats/LifetimeMapData/{bad}",
            f"/en/personalstats/FirmwareHistory/{bad}",
            f"/en/personalstats/FirmwareHistoryCSV/{bad}",
            f"/en/personalstats/AllMyDataAsCSV/{bad}",
            f"/en/personalstats/BatteryDegradationGraph/{bad}/odometer/52",
        )
        for path in paths:
            response = client.get(path)
            self.assertEqual(response.status_code, 404, path)


class PersonalStatsPageTests(TestCase):
    """HTML / CSV / map pages that do not call Tesla Fleet (local DB only)."""

    @classmethod
    def setUpTestData(cls):
        assert_not_production_database()
        seed_fake_car_telemetry(
            hashed_vin=FAKE_HASHED_VIN,
            vin=FAKE_VIN,
            days=30,
            samples_per_day=6,
        )

    def setUp(self):
        assert_not_production_database()

    def test_day_map_page_ok(self):
        client = Client()
        response = client.get(f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "daymap", status_code=200)

    def test_day_map_with_iso_day(self):
        client = Client()
        # Seed uses recent UTC days — pick an ISO date that may be empty or full
        response = client.get(
            f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}/2020-01-01"
        )
        # Empty day still renders the day map shell (200), not 500
        self.assertEqual(response.status_code, 200)

    def test_drives_page_ok(self):
        client = Client()
        response = client.get(f"/en/personalstats/Drives/{FAKE_HASHED_VIN}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "drives", status_code=200)

    def test_dc_charge_page_ok(self):
        client = Client()
        response = client.get(f"/en/personalstats/DCCharge/{FAKE_HASHED_VIN}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DC charge", status_code=200)
        # Range-vs-time card is gated on start-SoC curves + full pack range;
        # the page always ships the mode-switcher JS for when the card appears.
        self.assertContains(response, "applyRangeMode", status_code=200)
        self.assertContains(response, "matesla_dc_range_mode", status_code=200)

    def test_poll_details_page_ok(self):
        client = Client()
        response = client.get(f"/en/personalstats/PollDetails/{FAKE_HASHED_VIN}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Query spacing details", status_code=200)
        self.assertContains(response, "Habit model", status_code=200)

    def test_dc_charge_graphs_png(self):
        client = Client()
        for chart in (
            "power_vs_soc",
            "soc_vs_time",
            "range_vs_time_real",
            "range_vs_time_rated",
        ):
            response = client.get(
                f"/en/personalstats/DCChargeGraph/{FAKE_HASHED_VIN}/"
                f"{chart}/520?filter=robust&envelope=p10_p90&size=full"
            )
            self.assertEqual(response.status_code, 200, chart)
            self.assertTrue(
                response.content.startswith(PNG_MAGIC),
                f"{chart} should return PNG",
            )

    def test_day_charge_session_graph_png(self):
        """DC stop → separate power_vs_time and power_vs_soc PNGs."""
        from zoneinfo import ZoneInfo

        client = Client()
        # Seed puts DC (150 kW) on day_offset % 5 == 0; charge samples at 18:00
        sample = (
            TeslaCarDataSnapshot.objects.filter(
                hashedVin=FAKE_HASHED_VIN,
                charging_state="Charging",
                charger_power__gte=40,
            )
            .order_by("Date")
            .first()
        )
        self.assertIsNotNone(sample)
        day_tz = ZoneInfo("Europe/Brussels")
        day_iso = sample.Date.astimezone(day_tz).date().isoformat()
        start_ts = int(sample.Date.timestamp())
        for chart in ("power_vs_time", "power_vs_soc"):
            response = client.get(
                f"/en/personalstats/DayChargeSessionGraph/{FAKE_HASHED_VIN}/"
                f"{day_iso}/{start_ts}/{chart}?size=full"
            )
            self.assertEqual(response.status_code, 200, chart)
            self.assertTrue(
                response.content.startswith(PNG_MAGIC),
                f"{chart} should return PNG",
            )
        # Unknown start still returns a PNG (empty-state figure)
        response_empty = client.get(
            f"/en/personalstats/DayChargeSessionGraph/{FAKE_HASHED_VIN}/"
            f"{day_iso}/1/power_vs_time?size=thumb"
        )
        self.assertEqual(response_empty.status_code, 200)
        self.assertTrue(response_empty.content.startswith(PNG_MAGIC))
        # Unknown chart key
        response_bad = client.get(
            f"/en/personalstats/DayChargeSessionGraph/{FAKE_HASHED_VIN}/"
            f"{day_iso}/{start_ts}/not_a_chart"
        )
        self.assertEqual(response_bad.status_code, 404)

    def test_drives_page_sort_criteria(self):
        client = Client()
        for sort in (
            "longest",
            "elev_up",
            "elev_down",
            "hot",
            "cold",
            "soc_end",
        ):
            response = client.get(
                f"/en/personalstats/Drives/{FAKE_HASHED_VIN}"
                f"?sort={sort}&period=520"
            )
            self.assertEqual(response.status_code, 200, sort)
            # Date (day-map link) must stay present for every ranking —
            # only the optional score column toggles with sort.
            self.assertContains(response, "drives-date", msg_prefix=sort)
            self.assertContains(response, "Open day map", msg_prefix=sort)
            # elev/temp sorts insert an extra dedicated score <th>;
            # longest/soc_end reuse Distance / SoC end instead.
            if sort == "elev_up":
                self.assertContains(response, "Elevation gain", msg_prefix=sort)
            elif sort == "elev_down":
                self.assertContains(response, "Elevation loss", msg_prefix=sort)

    def test_drives_leaderboard_has_long_trips(self):
        from personalstats.views import _load_ranked_drives, DRIVES_MIN_KM

        trips = _load_ranked_drives(FAKE_HASHED_VIN, 520, min_km=DRIVES_MIN_KM)
        self.assertGreater(len(trips), 0)
        for trip in trips:
            self.assertGreaterEqual(trip["km"], DRIVES_MIN_KM)

    def test_firmware_history_page_ok(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/FirmwareHistory/{FAKE_HASHED_VIN}"
        )
        self.assertEqual(response.status_code, 200)

    def test_firmware_history_csv_ok(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/FirmwareHistoryCSV/{FAKE_HASHED_VIN}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))

    def test_all_my_data_csv_ok(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/AllMyDataAsCSV/{FAKE_HASHED_VIN}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.get("Content-Type", ""))
        self.assertGreater(len(response.content), 50)

    def test_resolve_address_validates_coords(self):
        client = Client()
        bad = client.get("/en/personalstats/ResolveAddress")
        self.assertEqual(bad.status_code, 400)
        out = client.get(
            "/en/personalstats/ResolveAddress?lat=999&lon=0"
        )
        self.assertEqual(out.status_code, 400)
        # Valid coords: may return unresolved_or_quota without Nominatim —
        # must not 500
        okish = client.get(
            "/en/personalstats/ResolveAddress?lat=50.85&lon=4.35"
        )
        self.assertIn(okish.status_code, (200,))
        payload = okish.json()
        self.assertIn("ok", payload)

    def test_match_supercharger_validates_coords(self):
        client = Client()
        bad = client.get("/en/personalstats/MatchSupercharger")
        self.assertEqual(bad.status_code, 400)
        out = client.get(
            "/en/personalstats/MatchSupercharger?lat=999&lon=0"
        )
        self.assertEqual(out.status_code, 400)


class PersonalStatsGraphTests(TestCase):
    """
    Graph endpoints with seeded fake history.

    ~400 days × 8 samples ≈ 3200 rows — enough for histograms, monthly
    climate ribbons, efficiency trips, and charge sessions.
    """

    @classmethod
    def setUpTestData(cls):
        assert_not_production_database()
        cls.n_rows = seed_fake_car_telemetry(
            hashed_vin=FAKE_HASHED_VIN,
            vin=FAKE_VIN,
            days=400,
            samples_per_day=8,
        )

    def setUp(self):
        assert_not_production_database()

    def test_seed_created_thousands_of_rows(self):
        self.assertGreaterEqual(self.n_rows, 2000)
        self.assertEqual(
            TeslaCarDataSnapshot.objects.filter(hashedVin=FAKE_HASHED_VIN).count(),
            self.n_rows,
        )
        # Production file still must not be the connection target
        assert_not_production_database()

    def test_stats_page_ok(self):
        client = Client()
        for lang in configured_language_codes():
            response = client.get(f"/{lang}/personalstats/Stats/{FAKE_HASHED_VIN}")
            self.assertEqual(
                response.status_code, 200, f"Stats page failed for {lang}"
            )

    def _assert_png(self, response, label: str):
        self.assertEqual(response.status_code, 200, f"{label} status")
        ctype = response.get("Content-Type", "")
        self.assertIn("image/png", ctype, f"{label} content-type={ctype}")
        body = response.content
        self.assertTrue(body.startswith(PNG_MAGIC), f"{label} not PNG")
        self.assertGreater(len(body), 1500, f"{label} PNG too small ({len(body)})")

    def test_all_stats_on_car_graphs_return_png(self):
        client = Client()
        period = 520  # weeks — full seeded range
        for field in STATS_ON_CAR_GRAPH_FIELDS:
            for size in ("thumb", "full"):
                url = (
                    f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/"
                    f"{field}/{period}?size={size}"
                )
                response = client.get(url)
                self._assert_png(response, f"{field}/{size}")

    def test_battery_degradation_scatter_png(self):
        client = Client()
        for field in BATTERY_DEGRADATION_FIELDS:
            url = (
                f"/en/personalstats/BatteryDegradationGraph/{FAKE_HASHED_VIN}/"
                f"{field}/520?size=thumb"
            )
            response = client.get(url)
            self._assert_png(response, f"degrad/{field}")

    def test_drive_histograms_have_content(self):
        """Speed/power histograms should not be empty PNGs of an empty axes only."""
        client = Client()
        for field in ("speed", "power"):
            response = client.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)
            # Full charts with real bars are typically >> empty axes
            self.assertGreater(len(response.content), 8000, field)

    def test_monthly_temp_ribbon_png(self):
        client = Client()
        for field in ("outside_temp", "inside_temp"):
            response = client.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)

    def test_charge_histograms_png(self):
        client = Client()
        for field in ("charger_power", "charge_limit_soc", "charge_rate"):
            response = client.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)

    def test_lifetime_map_json(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/LifetimeMapData/{FAKE_HASHED_VIN}?period=520"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("has_track"))
        self.assertGreater(data.get("path_points", 0), 10)
        self.assertGreater(data.get("drives", 0), 0)
        self.assertGreater(data.get("km_driven", 0), 1)

    def test_empty_hashed_vin_still_returns_png(self):
        """Unknown but valid hash: graphs render empty axes, not 500."""
        client = Client()
        empty_hash = "b" * 56
        response = client.get(
            f"/en/personalstats/StatsOnCarGraph/{empty_hash}/outside_temp/52"
            f"?size=thumb"
        )
        self._assert_png(response, "empty-car outside_temp")

    def test_factory_does_not_pollute_other_hashes(self):
        other = TeslaCarDataSnapshot.objects.exclude(
            hashedVin=FAKE_HASHED_VIN
        ).count()
        self.assertEqual(other, 0)


class PersonalStatsGraphPerfTests(TestCase):
    """
    Time each graph on seeded fake data and rank the slowest.

    Run with -v2 to see the PERF report in the test output:
      python manage.py test personalstats.tests.PersonalStatsGraphPerfTests -v2
    """

    # Soft ceiling on fake data (catch regressions, not real multi-year fleets)
    THUMB_BUDGET_S = 1.5
    CACHE_HIT_BUDGET_S = 0.05

    @classmethod
    def setUpTestData(cls):
        assert_not_production_database()
        seed_fake_car_telemetry(
            hashed_vin=FAKE_HASHED_VIN,
            vin=FAKE_VIN,
            days=400,
            samples_per_day=8,
        )

    def setUp(self):
        assert_not_production_database()
        # Isolate timings from other tests' LocMem cache entries
        from django.core.cache import cache

        cache.clear()

    def test_rank_graph_timings_and_cache(self):
        import time

        client = Client()
        period = 520
        rows = []

        def _time_get(path, label):
            t0 = time.perf_counter()
            resp = client.get(path)
            dt = time.perf_counter() - t0
            rows.append((dt, label, resp.status_code, len(resp.content),
                         resp.get("X-MaTesla-Graph-Cache", "")))
            self.assertEqual(resp.status_code, 200, label)
            return resp, dt

        for field in STATS_ON_CAR_GRAPH_FIELDS:
            _time_get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/"
                f"{field}/{period}?size=thumb",
                f"StatsOnCar/{field}/thumb",
            )
        for field in BATTERY_DEGRADATION_FIELDS:
            _time_get(
                f"/en/personalstats/BatteryDegradationGraph/{FAKE_HASHED_VIN}/"
                f"{field}/{period}?size=thumb",
                f"Degrad/{field}/thumb",
            )
        _time_get(
            f"/en/personalstats/LifetimeMapData/{FAKE_HASHED_VIN}?period={period}",
            "LifetimeMapData",
        )

        rows.sort(key=lambda r: r[0], reverse=True)
        report = ["\n--- Graph PERF ranking (slowest first, fake ~4k rows) ---"]
        total = 0.0
        for dt, label, code, nbytes, cache_hdr in rows:
            total += dt
            report.append(
                f"  {dt * 1000:7.1f} ms  {code}  cache={cache_hdr or '-':4}  "
                f"{nbytes:6d} B  {label}"
            )
        report.append(f"  TOTAL thumbs+map: {total * 1000:.0f} ms ({total:.2f}s)")
        print("\n".join(report))

        # No single thumb should be pathologically slow on this small seed
        for dt, label, *_ in rows:
            if label == "LifetimeMapData":
                continue
            self.assertLess(
                dt,
                self.THUMB_BUDGET_S,
                f"{label} took {dt:.2f}s (budget {self.THUMB_BUDGET_S}s)",
            )

        # Second request of a known-heavy chart must hit cache and be cheap
        heavy = "outside_temp"
        path = (
            f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/"
            f"{heavy}/{period}?size=thumb"
        )
        # ensure first fill
        client.get(path)
        t0 = time.perf_counter()
        resp = client.get(path)
        dt_hit = time.perf_counter() - t0
        print(
            f"  CACHE HIT recheck {heavy}/thumb: {dt_hit * 1000:.1f} ms  "
            f"header={resp.get('X-MaTesla-Graph-Cache')}"
        )
        self.assertEqual(resp.get("X-MaTesla-Graph-Cache"), "HIT")
        self.assertLess(
            dt_hit,
            self.CACHE_HIT_BUDGET_S,
            f"cache hit too slow: {dt_hit:.3f}s",
        )


class DayMapSegmentSocTests(TestCase):
    """Drive/charge SoC anchors with sparse Supercharger polls."""

    def _ts(self, hour, minute=0):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Brussels")
        return datetime(2026, 8, 9, hour, minute, 0, tzinfo=tz)

    def _row(self, t, *, kind, soc, odo, lat=50.0, lon=5.0, power_kw=0.0):
        """Minimal day-map sample dict."""
        if kind == "drive":
            return {
                "t": t,
                "lat": lat,
                "lon": lon,
                "odometer": odo,
                "battery_level": soc,
                "usable_battery_level": soc,
                "battery_range": soc * 2.5,
                "shift_state": "D",
                "speed": 60.0,
                "power": 20.0,
                "charging_state": "Disconnected",
                "charger_power": 0.0,
                "charge_energy_added": 0.0,
                "outside_temp": 15.0,
                "elevation": None,
            }
        if kind == "charge":
            return {
                "t": t,
                "lat": lat,
                "lon": lon,
                "odometer": odo,
                "battery_level": soc,
                "usable_battery_level": soc,
                "battery_range": soc * 2.5,
                "shift_state": None,
                "speed": None,
                "power": -power_kw,
                "charging_state": "Charging",
                "charger_power": power_kw,
                "charge_energy_added": max(0.0, (soc - 10.0) * 0.6),
                "outside_temp": 15.0,
                "elevation": None,
            }
        # park
        return {
            "t": t,
            "lat": lat,
            "lon": lon,
            "odometer": odo,
            "battery_level": soc,
            "usable_battery_level": soc,
            "battery_range": soc * 2.5,
            "shift_state": None,
            "speed": None,
            "power": 0.0,
            "charging_state": "Disconnected",
            "charger_power": 0.0,
            "charge_energy_added": 0.0,
            "outside_temp": 15.0,
            "elevation": None,
        }

    def test_drive_soc_end_ignores_mid_session_charge_poll(self):
        """
        Sparse SC: last drive ~9%, first charge poll already ~89%.
        Drive end SoC must stay near arrival (~9%), not the mid-charge poll.
        """
        from personalstats.views import _segment_day

        rows = [
            self._row(self._ts(8, 0), kind="park", soc=76.0, odo=1000.0),
            self._row(self._ts(8, 12), kind="drive", soc=76.0, odo=1000.5),
            self._row(self._ts(10, 16), kind="drive", soc=8.9, odo=1140.0),
            # 53 min gap; first SC poll already high
            self._row(
                self._ts(11, 9),
                kind="charge",
                soc=88.7,
                odo=1148.0,
                power_kw=40.0,
            ),
            self._row(self._ts(11, 12), kind="park", soc=90.0, odo=1148.0),
        ]
        drives, charges = _segment_day(rows, pack_kwh=75.0)
        self.assertEqual(len(drives), 1)
        drive = drives[0]
        self.assertAlmostEqual(drive["soc_start"], 76.0, places=1)
        self.assertAlmostEqual(drive["soc_end"], 8.9, places=1)
        self.assertGreater(drive["soc_used"], 60.0)
        # Late single charge poll kept with backfilled start SoC
        self.assertEqual(len(charges), 1)
        charge = charges[0]
        self.assertAlmostEqual(charge["soc_start"], 8.9, places=1)
        self.assertAlmostEqual(charge["soc_end"], 88.7, places=1)
        self.assertGreater(charge["soc_added"], 70.0)
        self.assertTrue(charge["is_dc_candidate"])

    def test_dense_charge_not_backfilled_from_drive(self):
        """Normal SC: first charge poll only slightly above last drive SoC."""
        from personalstats.views import _segment_day

        rows = [
            self._row(self._ts(11, 0), kind="park", soc=90.0, odo=2000.0),
            self._row(self._ts(11, 18), kind="drive", soc=90.0, odo=2000.5),
            self._row(self._ts(13, 17), kind="drive", soc=41.1, odo=2100.0),
            self._row(
                self._ts(13, 20), kind="charge", soc=42.7, odo=2100.0, power_kw=100.0
            ),
            self._row(
                self._ts(13, 40), kind="charge", soc=89.6, odo=2100.0, power_kw=50.0
            ),
            self._row(self._ts(13, 42), kind="park", soc=90.0, odo=2100.0),
        ]
        # Explicit session energy totals (Tesla resets on plug-in)
        rows[3]["charge_energy_added"] = 1.0
        rows[4]["charge_energy_added"] = 28.5
        rows[5]["charge_energy_added"] = 30.0  # post-unplug still holds session max
        drives, charges = _segment_day(rows, pack_kwh=75.0)
        self.assertEqual(len(drives), 1)
        self.assertAlmostEqual(drives[0]["soc_end"], 41.1, places=1)
        self.assertEqual(len(charges), 1)
        # Start stays on first charge sample (not last drive) — small gap
        self.assertAlmostEqual(charges[0]["soc_start"], 42.7, places=1)
        # End SoC uses post-charge park when higher
        self.assertAlmostEqual(charges[0]["soc_end"], 90.0, places=1)
        # kWh = max session total (incl. post-unplug), not delta of Charging only
        self.assertAlmostEqual(charges[0]["kwh_added"], 30.0, places=1)
        self.assertEqual(charges[0]["minutes"], 22)  # 13:20 → 13:42

    def test_short_sc_uses_post_unplug_energy_and_mid_session_start(self):
        """
        Sparse short Supercharge (Corentin-style): 2 Charging polls + Disconnected
        holding the real session kWh; first Charging already mid-session.
        """
        from personalstats.views import _segment_day

        rows = [
            self._row(self._ts(10, 31), kind="drive", soc=44.8, odo=3000.0),
            self._row(self._ts(10, 34), kind="drive", soc=44.8, odo=3000.1),
            self._row(
                self._ts(10, 37), kind="charge", soc=53.3, odo=3000.1, power_kw=141.0
            ),
            self._row(
                self._ts(10, 40), kind="charge", soc=63.2, odo=3000.1, power_kw=107.0
            ),
            self._row(self._ts(10, 43), kind="park", soc=69.4, odo=3000.1),
        ]
        rows[2]["charge_energy_added"] = 5.1
        rows[3]["charge_energy_added"] = 10.94
        rows[4]["charge_energy_added"] = 14.54
        drives, charges = _segment_day(rows, pack_kwh=75.0)
        self.assertEqual(len(charges), 1)
        charge = charges[0]
        # Mid-session SoC start from last drive; end from post-unplug
        self.assertAlmostEqual(charge["soc_start"], 44.8, places=1)
        self.assertAlmostEqual(charge["soc_end"], 69.4, places=1)
        self.assertAlmostEqual(charge["kwh_added"], 14.54, places=2)
        # Clock: last drive (mid-session) → first park after charge
        self.assertEqual(charge["start_local"], "10:34")
        self.assertEqual(charge["end_local"], "10:43")
        self.assertEqual(charge["minutes"], 9)

    def test_short_sc_without_mid_session_still_takes_post_energy(self):
        """First charge poll near arrival SoC: still extend kWh/end via unplug sample."""
        from personalstats.views import _segment_day

        rows = [
            self._row(self._ts(15, 43), kind="drive", soc=14.3, odo=4000.0),
            self._row(
                self._ts(15, 46), kind="charge", soc=18.6, odo=4000.0, power_kw=250.0
            ),
            self._row(
                self._ts(15, 49), kind="charge", soc=34.8, odo=4000.0, power_kw=180.0
            ),
            self._row(self._ts(15, 52), kind="park", soc=47.5, odo=4000.0),
        ]
        rows[1]["charge_energy_added"] = 2.6
        rows[2]["charge_energy_added"] = 11.84
        rows[3]["charge_energy_added"] = 19.52
        _drives, charges = _segment_day(rows, pack_kwh=75.0)
        self.assertEqual(len(charges), 1)
        charge = charges[0]
        self.assertAlmostEqual(charge["soc_start"], 18.6, places=1)
        self.assertAlmostEqual(charge["soc_end"], 47.5, places=1)
        self.assertAlmostEqual(charge["kwh_added"], 19.52, places=2)
        self.assertEqual(charge["start_local"], "15:46")
        self.assertEqual(charge["end_local"], "15:52")
        self.assertEqual(charge["minutes"], 6)

    def test_stopped_park_extends_charge_start(self):
        """Plugged Stopped sample before first Charging counts in duration."""
        from personalstats.views import _segment_day

        rows = [
            self._row(self._ts(12, 26), kind="drive", soc=20.5, odo=5000.0),
            self._row(self._ts(12, 29), kind="park", soc=20.3, odo=5000.0),
            self._row(
                self._ts(12, 32), kind="charge", soc=21.4, odo=5000.0, power_kw=140.0
            ),
            self._row(
                self._ts(12, 50), kind="charge", soc=80.0, odo=5000.0, power_kw=50.0
            ),
            self._row(self._ts(12, 53), kind="park", soc=82.0, odo=5000.0),
        ]
        rows[1]["charging_state"] = "Stopped"
        rows[1]["charge_energy_added"] = 0.0
        rows[2]["charge_energy_added"] = 0.64
        rows[3]["charge_energy_added"] = 35.0
        rows[4]["charge_energy_added"] = 36.5
        _drives, charges = _segment_day(rows, pack_kwh=75.0)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["start_local"], "12:29")
        self.assertEqual(charges[0]["end_local"], "12:53")
        self.assertAlmostEqual(charges[0]["kwh_added"], 36.5, places=1)


class DayMapUnmonitoredTailTests(TestCase):
    """Sparse capture: day-end GPS far from last drive arrival."""

    def test_no_gap_when_same_place(self):
        from personalstats.views import _daymap_unmonitored_tail

        drives = [{"end_lat": 50.7959, "end_lon": 4.3352}]
        self.assertIsNone(_daymap_unmonitored_tail(drives, 50.79587, 4.33523))

    def test_short_gap_message(self):
        from personalstats.views import _daymap_unmonitored_tail

        # ~475 m — Delhaize-style short hop home
        drives = [{"end_lat": 50.799972, "end_lon": 4.337122}]
        result = _daymap_unmonitored_tail(drives, 50.795871, 4.335234)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "short")
        self.assertGreater(result["gap_m"], 150)
        self.assertLess(result["gap_m"], 2500)
        self.assertIn("too short", result["message"].lower())

    def test_long_gap_message(self):
        from personalstats.views import _daymap_unmonitored_tail

        # ~5 km straight line → technical / capture gap wording
        drives = [{"end_lat": 50.80, "end_lon": 4.34}]
        result = _daymap_unmonitored_tail(drives, 50.76, 4.30)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "long")
        self.assertGreater(result["gap_m"], 2500)
        self.assertIn("missing telemetry", result["message"].lower())

    def test_no_drives_or_missing_coords(self):
        from personalstats.views import _daymap_unmonitored_tail

        self.assertIsNone(_daymap_unmonitored_tail([], 50.8, 4.3))
        self.assertIsNone(
            _daymap_unmonitored_tail([{"end_lat": None, "end_lon": 4.3}], 50.8, 4.3)
        )
        self.assertIsNone(
            _daymap_unmonitored_tail(
                [{"end_lat": 50.8, "end_lon": 4.3}], None, 4.3
            )
        )
