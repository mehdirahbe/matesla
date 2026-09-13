"""Place / region search: forward geocode, bbox policy, snapshot days."""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from matesla.models.AddressFromLatLong import (
    ForwardGeocode,
    ForwardGeocodeCache,
    GeocodeHit,
    apply_bbox_policy,
    rank_forward_hits,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.place_search import (
    civil_bounds,
    days_from_snapshots,
    fetch_snapshots_in_bbox,
    order_hits_by_presence,
    range_too_long,
    summer_bounds,
    traces_from_snapshots,
)
from personalstats.test_factories import (
    FAKE_HASHED_VIN,
    FAKE_VIN,
    assert_not_production_database,
)

# Real-world-ish fixtures (same neighbourhood as the owner's 2024 traces).
BRETAGNE_HIT = GeocodeHit(
    label="Bretagne, France",
    name="Bretagne",
    kind="region",
    lat=48.12,
    lon=-2.75,
    south=47.277,
    north=48.896,
    west=-5.141,
    east=-1.016,
    provider="test",
    importance=0.9,
)
NAMUR_CITY_HIT = GeocodeHit(
    label="Namur, Belgique",
    name="Namur",
    kind="city",
    lat=50.4669,
    lon=4.8675,
    south=50.428,
    north=50.502,
    west=4.805,
    east=4.932,
    provider="test",
    importance=0.8,
)
NAMUR_PROVINCE_HIT = GeocodeHit(
    label="Province de Namur, Belgique",
    name="Namur",
    kind="county",
    lat=50.3,
    lon=4.85,
    south=49.90,
    north=50.81,
    west=4.30,
    east=5.22,
    provider="test",
    importance=0.7,
)
LORIENT = (47.62, -2.95)
NAMUR_CENTRE = (50.468, 4.867)
UCCLE = (50.796, 4.336)
CAMPING_VERDON = (43.755, 5.95)
VERDON_RIVER_HIT = GeocodeHit(
    label="Le Verdon, PAC, France",
    name="Le Verdon",
    kind="region",
    lat=43.800,
    lon=6.254,
    south=43.694,
    north=44.321,
    west=5.745,
    east=6.637,
    provider="test",
    importance=0.47,
)
VERDON_MARNE_HIT = GeocodeHit(
    label="Verdon, GES, France",
    name="Verdon",
    kind="city",
    lat=48.948,
    lon=3.619,
    south=48.931,
    north=48.967,
    west=3.591,
    east=3.659,
    provider="test",
    importance=0.58,
)


def _aware(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)


def _snap(*, when, lat, lon, odo=1000.0, shift="P"):
    TeslaCarDataSnapshot.objects.create(
        vin=FAKE_VIN,
        hashedVin=FAKE_HASHED_VIN,
        Date=when,
        DateOnlyDay=when.date(),
        latitude=lat,
        longitude=lon,
        odometer=odo,
        shift_state=shift,
        speed=30.0 if shift == "D" else 0.0,
        battery_level=70.0,
    )


class BboxPolicyTests(TestCase):
    def test_region_keeps_large_outline(self):
        hit = apply_bbox_policy(BRETAGNE_HIT)
        self.assertLess(hit.south, 47.28)
        self.assertGreater(hit.north, 48.89)
        self.assertLess(hit.west, -5.0)
        self.assertGreater(hit.east, -1.05)
        self.assertLess(hit.south, LORIENT[0])
        self.assertGreater(hit.north, LORIENT[0])
        self.assertLess(hit.west, LORIENT[1])
        self.assertGreater(hit.east, LORIENT[1])

    def test_city_stays_tight_and_covers_namur_centre(self):
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        lat_span = hit.north - hit.south
        lon_span = hit.east - hit.west
        self.assertLess(lat_span, 0.45)
        self.assertLess(lon_span, 0.65)
        self.assertTrue(hit.south <= NAMUR_CENTRE[0] <= hit.north)
        self.assertTrue(hit.west <= NAMUR_CENTRE[1] <= hit.east)
        self.assertFalse(hit.south <= UCCLE[0] <= hit.north)

    def test_huge_city_bbox_is_rebuilt_around_centre(self):
        huge = GeocodeHit(
            label="Namur, Belgique",
            name="Namur",
            kind="city",
            lat=50.4669,
            lon=4.8675,
            south=49.8,
            north=51.0,
            west=4.0,
            east=5.5,
            provider="test",
        )
        hit = apply_bbox_policy(huge)
        self.assertLess(hit.north - hit.south, 0.45)
        self.assertTrue(hit.south <= NAMUR_CENTRE[0] <= hit.north)
        self.assertTrue(hit.west <= NAMUR_CENTRE[1] <= hit.east)

    def test_rank_prefers_region_bretagne_and_city_namur(self):
        bretagne_city = GeocodeHit(
            label="Bretagne, Territoire de Belfort",
            name="Bretagne",
            kind="city",
            lat=47.59,
            lon=6.94,
            south=47.58,
            north=47.60,
            west=6.93,
            east=6.95,
            provider="test",
            importance=0.4,
        )
        ranked = rank_forward_hits("Bretagne", [bretagne_city, BRETAGNE_HIT])
        self.assertEqual(ranked[0].kind, "region")
        ranked_namur = rank_forward_hits(
            "Namur", [NAMUR_PROVINCE_HIT, NAMUR_CITY_HIT]
        )
        self.assertEqual(ranked_namur[0].kind, "city")

    def test_rank_prefers_verdon_river_over_tiny_communes(self):
        ranked = rank_forward_hits(
            "verdon", [VERDON_MARNE_HIT, VERDON_RIVER_HIT]
        )
        self.assertEqual(ranked[0].name, "Le Verdon")
        self.assertEqual(ranked[0].kind, "region")
        river = apply_bbox_policy(VERDON_RIVER_HIT)
        self.assertTrue(river.south <= CAMPING_VERDON[0] <= river.north)
        self.assertTrue(river.west <= CAMPING_VERDON[1] <= river.east)


class SnapshotQueryTests(TestCase):
    def setUp(self):
        assert_not_production_database()

    def test_bretagne_summer_lists_known_august_days(self):
        # Drive into Brittany 5 Aug, parked 6–8, leave 9 Aug; Uccle in between ignored.
        _snap(when=_aware(2024, 8, 5, 10), lat=47.62, lon=-2.95, odo=10010, shift="D")
        _snap(when=_aware(2024, 8, 6, 12), lat=47.60, lon=-3.00, odo=10040, shift="P")
        _snap(when=_aware(2024, 8, 7, 9), lat=47.61, lon=-2.99, odo=10080, shift="D")
        _snap(when=_aware(2024, 8, 8, 15), lat=48.60, lon=-2.20, odo=10120, shift="P")
        _snap(when=_aware(2024, 8, 9, 11), lat=48.55, lon=-1.80, odo=10190, shift="D")
        _snap(when=_aware(2024, 8, 10, 12), lat=50.80, lon=4.35, odo=10300, shift="P")
        start, end = summer_bounds(2024)
        window = civil_bounds(start, end)
        hit = apply_bbox_policy(BRETAGNE_HIT)
        rows = fetch_snapshots_in_bbox(
            FAKE_HASHED_VIN, window[0], window[1], hit.south, hit.north, hit.west, hit.east
        )
        days = [item.day for item in days_from_snapshots(rows)]
        self.assertEqual(
            days,
            [
                date(2024, 8, 5),
                date(2024, 8, 6),
                date(2024, 8, 7),
                date(2024, 8, 8),
                date(2024, 8, 9),
            ],
        )
        traces = traces_from_snapshots(rows)
        self.assertTrue(traces)

    def test_namur_new_years_eve_parked_still_matches(self):
        # Morning at home, evening parked in Namur (would miss the Drives ≥20 km cut).
        _snap(when=_aware(2024, 12, 31, 8), lat=UCCLE[0], lon=UCCLE[1], odo=20000)
        _snap(
            when=_aware(2024, 12, 31, 18, 12),
            lat=NAMUR_CENTRE[0],
            lon=NAMUR_CENTRE[1],
            odo=20040,
        )
        _snap(
            when=_aware(2024, 12, 31, 21, 0),
            lat=NAMUR_CENTRE[0],
            lon=NAMUR_CENTRE[1],
            odo=20040,
        )
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        start, end = civil_bounds(date(2024, 12, 31), date(2024, 12, 31))
        rows = fetch_snapshots_in_bbox(
            FAKE_HASHED_VIN, start, end, hit.south, hit.north, hit.west, hit.east
        )
        days = days_from_snapshots(rows)
        self.assertEqual([item.day for item in days], [date(2024, 12, 31)])
        self.assertGreaterEqual(days[0].point_count, 2)

    def test_empty_bbox_is_empty_list(self):
        _snap(when=_aware(2024, 12, 31, 18), lat=UCCLE[0], lon=UCCLE[1])
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        start, end = civil_bounds(date(2024, 12, 31), date(2024, 12, 31))
        rows = fetch_snapshots_in_bbox(
            FAKE_HASHED_VIN, start, end, hit.south, hit.north, hit.west, hit.east
        )
        self.assertEqual(days_from_snapshots(rows), [])

    def test_range_cap(self):
        self.assertTrue(range_too_long(date(2020, 1, 1), date(2022, 1, 10)))
        self.assertFalse(range_too_long(date(2024, 6, 21), date(2024, 9, 22)))

    def test_verdon_river_bbox_matches_camping_not_marne_village(self):
        _snap(
            when=_aware(2026, 7, 5, 20),
            lat=CAMPING_VERDON[0],
            lon=CAMPING_VERDON[1],
            odo=30000,
        )
        river = apply_bbox_policy(VERDON_RIVER_HIT)
        village = apply_bbox_policy(VERDON_MARNE_HIT)
        start, end = civil_bounds(date(2026, 6, 21), date(2026, 9, 22))
        river_days = days_from_snapshots(
            fetch_snapshots_in_bbox(
                FAKE_HASHED_VIN, start, end, river.south, river.north, river.west, river.east
            )
        )
        village_days = days_from_snapshots(
            fetch_snapshots_in_bbox(
                FAKE_HASHED_VIN,
                start,
                end,
                village.south,
                village.north,
                village.west,
                village.east,
            )
        )
        self.assertEqual([item.day for item in river_days], [date(2026, 7, 5)])
        self.assertEqual(village_days, [])
        ordered = order_hits_by_presence(
            FAKE_HASHED_VIN, start, end, [village, river]
        )
        self.assertEqual(ordered[0].name, "Le Verdon")


class ForwardGeocodeTests(TestCase):
    def setUp(self):
        assert_not_production_database()

    @override_settings(GEOAPIFY_API_KEY="test-key-xyz", GEOAPIFY_MIN_INTERVAL_SEC=0)
    def test_geoapify_parses_region_bbox(self):
        fake_json = {
            "features": [
                {
                    "type": "Feature",
                    "bbox": [-5.141, 47.277, -1.016, 48.896],
                    "geometry": {"type": "Point", "coordinates": [-2.75, 48.12]},
                    "properties": {
                        "name": "Bretagne",
                        "formatted": "Bretagne, France",
                        "result_type": "state",
                        "lat": 48.12,
                        "lon": -2.75,
                        "rank": {"confidence": 0.95},
                    },
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_json
        with patch(
            "matesla.models.AddressFromLatLong.requests.get", return_value=mock_resp
        ):
            result = ForwardGeocode("Bretagne")
        self.assertIsNone(result.error)
        self.assertEqual(result.hits[0].kind, "region")
        self.assertLess(result.hits[0].south, LORIENT[0])
        self.assertGreater(result.hits[0].north, LORIENT[0])
        with patch("matesla.models.AddressFromLatLong.requests.get") as again:
            cached = ForwardGeocode("Bretagne")
        again.assert_not_called()
        self.assertTrue(cached.cached)
        self.assertEqual(ForwardGeocodeCache.objects.count(), 1)

    @override_settings(GEOAPIFY_API_KEY="test-key-xyz", GEOAPIFY_MIN_INTERVAL_SEC=0)
    def test_geoapify_amenity_supplement_finds_river(self):
        cities = {
            "features": [
                {
                    "bbox": [3.59, 48.93, 3.66, 48.97],
                    "geometry": {"type": "Point", "coordinates": [3.62, 48.95]},
                    "properties": {
                        "name": "Verdon",
                        "formatted": "Verdon, GES, France",
                        "result_type": "city",
                        "lat": 48.95,
                        "lon": 3.62,
                        "rank": {"importance": 0.58},
                    },
                }
            ]
        }
        amenity = {
            "features": [
                {
                    "bbox": [5.745, 43.694, 6.637, 44.321],
                    "geometry": {"type": "Point", "coordinates": [6.254, 43.80]},
                    "properties": {
                        "name": "Le Verdon",
                        "formatted": "Le Verdon, PAC, France",
                        "result_type": "amenity",
                        "lat": 43.80,
                        "lon": 6.254,
                        "rank": {"importance": 0.47, "popularity": 4.7},
                    },
                }
            ]
        }
        mock_city = MagicMock()
        mock_city.raise_for_status = MagicMock()
        mock_city.json.return_value = cities
        mock_amenity = MagicMock()
        mock_amenity.raise_for_status = MagicMock()
        mock_amenity.json.return_value = amenity
        with patch(
            "matesla.models.AddressFromLatLong.requests.get",
            side_effect=[mock_city, mock_amenity],
        ) as get_mock:
            result = ForwardGeocode("verdon")
        self.assertIsNone(result.error)
        self.assertEqual(result.hits[0].name, "Le Verdon")
        self.assertEqual(result.hits[0].kind, "region")
        self.assertTrue(
            result.hits[0].south <= CAMPING_VERDON[0] <= result.hits[0].north
        )
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(get_mock.call_args_list[1].kwargs["params"]["type"], "amenity")

    @override_settings(GEOAPIFY_API_KEY="test-key-xyz", GEOAPIFY_MIN_INTERVAL_SEC=0)
    def test_quota_is_error_not_exception(self):
        with patch(
            "matesla.models.AddressFromLatLong._acquire_nominatim_slot",
            return_value=False,
        ):
            result = ForwardGeocode("Namur")
        self.assertEqual(result.error, "quota")
        self.assertEqual(result.hits, [])

    @override_settings(GEOAPIFY_API_KEY="", GEOAPIFY_KEY="")
    def test_nominatim_network_failure(self):
        with patch("matesla.models.AddressFromLatLong.Nominatim") as nominatim_cls:
            nominatim_cls.return_value.geocode.side_effect = OSError("down")
            with self.assertLogs("matesla.models.AddressFromLatLong", level="WARNING"):
                result = ForwardGeocode("Namur")
        self.assertEqual(result.error, "network")


class PlaceSearchPageTests(TestCase):
    def setUp(self):
        assert_not_production_database()
        _snap(
            when=_aware(2024, 8, 6, 12),
            lat=LORIENT[0],
            lon=LORIENT[1],
            odo=10000,
        )
        _snap(
            when=_aware(2024, 12, 31, 18, 12),
            lat=NAMUR_CENTRE[0],
            lon=NAMUR_CENTRE[1],
            odo=20000,
        )

    def test_form_ok(self):
        client = Client()
        response = client.get(f"/en/personalstats/Where/{FAKE_HASHED_VIN}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Where did I go")

    def test_bretagne_summer_lists_daymap_link(self):
        client = Client()
        hit = apply_bbox_policy(BRETAGNE_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[hit]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Bretagne&from=2024-06-21&to=2024-09-22"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2024-08-06")
        self.assertContains(
            response, f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}/2024-08-06"
        )
        self.assertContains(response, "where-map")

    def test_namur_single_day(self):
        client = Client()
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[hit]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Namur&from=2024-12-31&to=2024-12-31"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2024-12-31")
        self.assertContains(
            response, f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}/2024-12-31"
        )

    def test_missing_to_is_that_one_day(self):
        client = Client()
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[hit]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Namur&from=2024-12-31"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, f"/en/personalstats/DayMap/{FAKE_HASHED_VIN}/2024-12-31"
        )
        self.assertNotContains(response, "Enter a place and a date range.")

    def test_empty_state(self):
        client = Client()
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[hit]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Namur&from=2024-01-01&to=2024-01-02"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No days in this place for that period.")

    def test_geocode_fail_is_not_500(self):
        client = Client()
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error="quota", hits=[]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Namur&from=2024-12-31&to=2024-12-31"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Geocoding quota reached")

    def test_invalid_date_is_400(self):
        client = Client()
        response = client.get(
            f"/en/personalstats/Where/{FAKE_HASHED_VIN}?q=Namur&from=nope&to=2024-12-31"
        )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)

    def test_verdon_page_lists_july_camping_day(self):
        client = Client()
        _snap(
            when=_aware(2026, 7, 5, 20),
            lat=CAMPING_VERDON[0],
            lon=CAMPING_VERDON[1],
            odo=30000,
        )
        river = apply_bbox_policy(VERDON_RIVER_HIT)
        village = apply_bbox_policy(VERDON_MARNE_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[village, river]),
        ):
            response = client.get(
                f"/en/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=verdon&from=2026-06-21&to=2026-09-22"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-07-05")
        self.assertContains(response, "Le Verdon")
        self.assertNotContains(response, "No days in this place for that period.")

    def test_french_copy(self):
        client = Client()
        hit = apply_bbox_policy(NAMUR_CITY_HIT)
        with patch(
            "personalstats.place_search.ForwardGeocode",
            return_value=MagicMock(error=None, hits=[hit]),
        ):
            response = client.get(
                f"/fr/personalstats/Where/{FAKE_HASHED_VIN}"
                "?q=Namur&from=2024-01-01&to=2024-01-02"
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aucun jour")
