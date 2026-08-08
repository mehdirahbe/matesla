"""Unit tests for Supercharger nearest-match (no network when cache is primed)."""

from django.core.cache import cache
from django.test import SimpleTestCase

from matesla.superchargers import (
    CACHE_KEY,
    _normalize_sites,
    nearest_supercharger,
)


class SuperchargerMatchTests(SimpleTestCase):
    def setUp(self):
        cache.delete(CACHE_KEY)

    def tearDown(self):
        cache.delete(CACHE_KEY)

    def test_nearest_within_radius(self):
        sites = _normalize_sites(
            [
                {
                    "id": 1,
                    "locationId": "testdummy",
                    "name": "Test SC",
                    "status": "OPEN",
                    "gps": {"latitude": 43.5120, "longitude": 5.4664},
                    "powerKilowatt": 250,
                    "stallCount": 12,
                },
                {
                    "id": 2,
                    "locationId": "faraway",
                    "name": "Far SC",
                    "status": "OPEN",
                    "gps": {"latitude": 48.85, "longitude": 2.35},
                    "powerKilowatt": 150,
                    "stallCount": 8,
                },
            ]
        )
        cache.set(CACHE_KEY, sites, 60)
        match = nearest_supercharger(43.51202, 5.466372)
        self.assertIsNotNone(match)
        self.assertEqual(match["name"], "Test SC")
        self.assertEqual(match["power_kw"], 250)
        self.assertIn("tesla.com/findus/location/supercharger/testdummy", match["url"])
        self.assertLessEqual(match["distance_m"], 50)

    def test_no_match_when_too_far(self):
        sites = _normalize_sites(
            [
                {
                    "id": 1,
                    "locationId": "paris",
                    "name": "Paris SC",
                    "status": "OPEN",
                    "gps": {"latitude": 48.85, "longitude": 2.35},
                    "powerKilowatt": 250,
                    "stallCount": 8,
                },
            ]
        )
        cache.set(CACHE_KEY, sites, 60)
        # Aix-en-Provence coords — Paris is hundreds of km away
        match = nearest_supercharger(43.51202, 5.466372)
        self.assertIsNone(match)

    def test_skips_closed_sites(self):
        sites = _normalize_sites(
            [
                {
                    "id": 9,
                    "locationId": "closedone",
                    "name": "Closed",
                    "status": "CLOSED",
                    "gps": {"latitude": 43.5120, "longitude": 5.4664},
                    "powerKilowatt": 250,
                },
            ]
        )
        cache.set(CACHE_KEY, sites, 60)
        self.assertIsNone(nearest_supercharger(43.51202, 5.466372))
