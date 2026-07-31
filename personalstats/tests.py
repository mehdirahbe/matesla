"""
Personal stats URL + graph tests.

Uses Django TestCase → isolated test DB only (test_matesla.sqlite3).
Fake telemetry is seeded via test_factories — never touches db.sqlite3.
"""

from django.test import Client, TestCase
from django.db import connection

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
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
        c = Client()
        for lang in ("fr", "en"):
            response = c.get(
                f"/{lang}/personalstats/StatsOnCarGraph/fakesha/dontexist/5"
            )
            self.assertEqual(
                response.status_code, 404, "bogus field should 404"
            )
            response = c.get(f"/{lang}/personalstats/Stats/--")
            self.assertEqual(
                response.status_code, 404, "SQL-injection-ish hash should 404"
            )


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
        c = Client()
        for lang in ("fr", "en"):
            response = c.get(f"/{lang}/personalstats/Stats/{FAKE_HASHED_VIN}")
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
        c = Client()
        period = 520  # weeks — full seeded range
        for field in STATS_ON_CAR_GRAPH_FIELDS:
            for size in ("thumb", "full"):
                url = (
                    f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/"
                    f"{field}/{period}?size={size}"
                )
                response = c.get(url)
                self._assert_png(response, f"{field}/{size}")

    def test_battery_degradation_scatter_png(self):
        c = Client()
        for field in BATTERY_DEGRADATION_FIELDS:
            url = (
                f"/en/personalstats/BatteryDegradationGraph/{FAKE_HASHED_VIN}/"
                f"{field}/520?size=thumb"
            )
            response = c.get(url)
            self._assert_png(response, f"degrad/{field}")

    def test_drive_histograms_have_content(self):
        """Speed/power histograms should not be empty PNGs of an empty axes only."""
        c = Client()
        for field in ("speed", "power"):
            response = c.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)
            # Full charts with real bars are typically >> empty axes
            self.assertGreater(len(response.content), 8000, field)

    def test_monthly_temp_ribbon_png(self):
        c = Client()
        for field in ("outside_temp", "inside_temp"):
            response = c.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)

    def test_charge_histograms_png(self):
        c = Client()
        for field in ("charger_power", "charge_limit_soc", "charge_rate"):
            response = c.get(
                f"/en/personalstats/StatsOnCarGraph/{FAKE_HASHED_VIN}/{field}/520"
                f"?size=full"
            )
            self._assert_png(response, field)

    def test_lifetime_map_json(self):
        c = Client()
        response = c.get(
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
        c = Client()
        empty_hash = "b" * 56
        response = c.get(
            f"/en/personalstats/StatsOnCarGraph/{empty_hash}/outside_temp/52"
            f"?size=thumb"
        )
        self._assert_png(response, "empty-car outside_temp")

    def test_factory_does_not_pollute_other_hashes(self):
        other = TeslaCarDataSnapshot.objects.exclude(
            hashedVin=FAKE_HASHED_VIN
        ).count()
        self.assertEqual(other, 0)
