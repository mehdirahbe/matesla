"""OSMF Standard raster tile usage policy.

https://operations.osmfoundation.org/policies/tiles/
https://wiki.osmfoundation.org/wiki/Licence/Attribution_Guidelines
"""

from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings

from personalstats.test_factories import (
    FAKE_HASHED_VIN,
    assert_not_production_database,
    seed_fake_car_telemetry,
)

CANONICAL_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
FORBIDDEN_SUBDOMAIN_HOST = "{s}.tile.openstreetmap.org"


class OsmTileSettingsTests(SimpleTestCase):
    def test_canonical_tile_url_has_no_letter_subdomains(self):
        self.assertEqual(settings.OSM_TILE_URL, CANONICAL_TILE_URL)
        self.assertNotIn("{s}", settings.OSM_TILE_URL)
        self.assertTrue(settings.OSM_TILE_URL.startswith("https://"))

    def test_referrer_policy_sends_origin_to_tile_host(self):
        # Django's default same-origin would omit Referer on OSM tile requests.
        self.assertEqual(
            settings.SECURE_REFERRER_POLICY, "strict-origin-when-cross-origin"
        )
        self.assertNotEqual(settings.SECURE_REFERRER_POLICY, "same-origin")
        self.assertNotEqual(settings.SECURE_REFERRER_POLICY, "no-referrer")


class OsmTileTemplateTests(SimpleTestCase):
    def test_no_template_uses_letter_subdomains(self):
        roots = [
            Path(settings.BASE_DIR) / "personalstats" / "templates",
            Path(settings.BASE_DIR) / "templates",
            Path(settings.BASE_DIR) / "matesla" / "templates",
        ]
        hits = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if FORBIDDEN_SUBDOMAIN_HOST in text:
                    hits.append(str(path.relative_to(settings.BASE_DIR)))
        self.assertEqual(hits, [], f"OSM letter-subdomain tile host in {hits}")


class OsmTilePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        assert_not_production_database()
        seed_fake_car_telemetry(
            hashed_vin=FAKE_HASHED_VIN,
            days=8,
            samples_per_day=4,
        )

    def setUp(self):
        assert_not_production_database()

    def test_daymap_uses_canonical_url_and_contributors_attribution(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}/2024-01-01"
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn(FORBIDDEN_SUBDOMAIN_HOST, body)
        self.assertIn(CANONICAL_TILE_URL, body)
        self.assertIn("OpenStreetMap</a> contributors", body)
        self.assertEqual(
            response.headers.get("Referrer-Policy"),
            "strict-origin-when-cross-origin",
        )

    @override_settings(GEOAPIFY_API_KEY="test-key-xyz")
    def test_footer_names_osm_contributors(self):
        client = Client()
        response = client.get("/en/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "© OpenStreetMap contributors")
