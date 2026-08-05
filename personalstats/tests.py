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
            f"/en/personalstats/Drives/{bad}",
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
