"""Tests for geo cache elevation (Open-Meteo) and address backfill hooks."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from matesla.geo_enrich import (
    apply_cached_elevation_to_snapshot,
    enrich_elevations_once,
    fetch_open_meteo_elevations,
    lookup_cached_elevation,
    propagate_elevation_to_ids,
    propagate_elevation_to_snapshots,
    round_grid,
    upsert_grid_elevation,
)
from matesla.models.AddressFromLatLong import AddressFromLatLong, LookupCachedAddress
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.VinHash import HashTheVin


class RoundGridTests(TestCase):
    def test_round_grid(self):
        # Python round uses banker's rounding; just match built-in round(..., 4).
        self.assertEqual(round_grid(50.79621, 4.33545), (50.7962, 4.3354))
        lat, lon = 46.18805, 6.133777
        self.assertEqual(round_grid(lat, lon), (round(lat, 4), round(lon, 4)))


class OpenMeteoClientTests(TestCase):
    def test_fetch_parses_elevation_list(self):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"elevation": [48.5, 120.0]}
        session.get.return_value = resp

        out = fetch_open_meteo_elevations(
            [(50.8, 4.3), (46.2, 6.1)], session=session
        )
        self.assertEqual(out, [48.5, 120.0])
        session.get.assert_called_once()
        args, kwargs = session.get.call_args
        self.assertIn("elevation", args[0])
        self.assertIn("latitude", kwargs["params"])

    def test_fetch_failure_returns_nones(self):
        session = MagicMock()
        session.get.side_effect = OSError("network down")
        out = fetch_open_meteo_elevations([(50.0, 4.0)], session=session)
        self.assertEqual(out, [None])


class ElevationCacheTests(TestCase):
    def test_upsert_and_lookup(self):
        upsert_grid_elevation(50.7962, 4.3354, 88.0)
        self.assertEqual(lookup_cached_elevation(50.79621, 4.33541), 88.0)
        row = AddressFromLatLong.objects.get(latitude=50.7962, longitude=4.3354)
        self.assertEqual(row.elevation, 88.0)
        self.assertEqual(row.address, "")
        self.assertIsNotNone(row.elevation_fetched_at)

    def test_upsert_preserves_address(self):
        AddressFromLatLong.objects.create(
            latitude=50.1,
            longitude=4.1,
            address="1 Rue Test, Bruxelles",
            date=timezone.now().date(),
        )
        upsert_grid_elevation(50.1, 4.1, 55.0)
        row = AddressFromLatLong.objects.get(latitude=50.1, longitude=4.1)
        self.assertEqual(row.address, "1 Rue Test, Bruxelles")
        self.assertEqual(row.elevation, 55.0)

    def test_propagate_only_null_elevation(self):
        vin = "LRWYGCEK0NC000001"
        hv = HashTheVin(vin)
        base = timezone.now()
        # Raw coords that round to 50.7962, 4.3354
        s_null = TeslaCarDataSnapshot(
            vin=vin,
            hashedVin=hv,
            Date=base,
            DateOnlyDay=base.date(),
            latitude=50.79621,
            longitude=4.33541,
            elevation=None,
            charging_state="Disconnected",
            randomNr=0.1,
        )
        s_null.save()
        s_keep = TeslaCarDataSnapshot(
            vin=vin,
            hashedVin=hv,
            Date=base + timedelta(minutes=1),
            DateOnlyDay=base.date(),
            latitude=50.79622,
            longitude=4.33542,
            elevation=999.0,  # TeslaFi-style prior value
            charging_state="Disconnected",
            randomNr=0.2,
        )
        s_keep.save()

        n = propagate_elevation_to_snapshots(50.7962, 4.3354, 77.0)
        self.assertEqual(n, 1)
        s_null.refresh_from_db()
        s_keep.refresh_from_db()
        self.assertEqual(s_null.elevation, 77.0)
        self.assertEqual(s_keep.elevation, 999.0)

    def test_propagate_by_ids_only_null(self):
        vin = "LRWYGCEK0NC000003"
        hv = HashTheVin(vin)
        base = timezone.now()
        a = TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=hv,
            Date=base,
            DateOnlyDay=base.date(),
            latitude=50.0,
            longitude=4.0,
            elevation=None,
            charging_state="Disconnected",
            randomNr=0.4,
        )
        b = TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=hv,
            Date=base + timedelta(minutes=1),
            DateOnlyDay=base.date(),
            latitude=50.0,
            longitude=4.0,
            elevation=10.0,
            charging_state="Disconnected",
            randomNr=0.5,
        )
        n = propagate_elevation_to_ids([a.id, b.id], 55.0)
        self.assertEqual(n, 1)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.elevation, 55.0)
        self.assertEqual(b.elevation, 10.0)

    def test_enrich_noop_when_no_null_elev(self):
        stats = enrich_elevations_once()
        self.assertTrue(stats.get("elev_noop"))
        self.assertEqual(stats.get("elev_snapshots_updated"), 0)

    def test_enrich_uses_cache_without_http(self):
        vin = "LRWYGCEK0NC000004"
        hv = HashTheVin(vin)
        base = timezone.now()
        lat, lon = 51.0, 5.0
        lat4, lon4 = round_grid(lat, lon)
        upsert_grid_elevation(lat4, lon4, 123.0)
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=hv,
            Date=base,
            DateOnlyDay=base.date(),
            latitude=lat,
            longitude=lon,
            elevation=None,
            charging_state="Disconnected",
            randomNr=0.6,
        )
        session = MagicMock()
        stats = enrich_elevations_once(session=session)
        session.get.assert_not_called()
        self.assertEqual(stats["elev_grids_requested"], 0)
        self.assertGreaterEqual(stats["elev_snapshots_updated"], 1)
        self.assertEqual(
            TeslaCarDataSnapshot.objects.get(vin=vin).elevation, 123.0
        )

    def test_apply_cached_on_snapshot_object(self):
        lat, lon = 46.18805, 6.133777
        lat4, lon4 = round_grid(lat, lon)
        upsert_grid_elevation(lat4, lon4, 420.0)
        snap = TeslaCarDataSnapshot(
            latitude=lat,
            longitude=lon,
            elevation=None,
        )
        self.assertTrue(apply_cached_elevation_to_snapshot(snap))
        self.assertEqual(snap.elevation, 420.0)

    def test_enrich_elevations_once_end_to_end(self):
        vin = "LRWYGCEK0NC000002"
        hv = HashTheVin(vin)
        base = timezone.now()
        TeslaCarDataSnapshot.objects.create(
            vin=vin,
            hashedVin=hv,
            Date=base,
            DateOnlyDay=base.date(),
            latitude=50.5536,
            longitude=4.2733,
            elevation=None,
            charging_state="Disconnected",
            randomNr=0.3,
            shift_state="D",
            speed=30.0,
        )
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"elevation": [116.3]}
        session.get.return_value = resp

        with override_settings(GEO_ELEV_BATCH_SIZE=10, GEO_ELEV_SCAN_LIMIT=100):
            stats = enrich_elevations_once(session=session)

        self.assertTrue(stats["elev_http_ok"])
        self.assertEqual(stats["elev_grids_filled"], 1)
        self.assertGreaterEqual(stats["elev_snapshots_updated"], 1)
        snap = TeslaCarDataSnapshot.objects.get(vin=vin)
        self.assertAlmostEqual(snap.elevation, 116.3)
        lat4, lon4 = round_grid(50.5536, 4.2733)
        self.assertEqual(lookup_cached_elevation(lat4, lon4), 116.3)


class AddressLookupPreservesElevationTests(TestCase):
    def test_lookup_empty_address_is_miss(self):
        AddressFromLatLong.objects.create(
            latitude=50.0,
            longitude=4.0,
            address="",
            date=timezone.now().date(),
            elevation=12.0,
        )
        self.assertIsNone(LookupCachedAddress(50.0, 4.0))
        self.assertEqual(lookup_cached_elevation(50.0, 4.0), 12.0)


class NominatimQuotaPurposeTests(TestCase):
    """Backfill must not exhaust the hard daily cap used by day-map AJAX."""

    @override_settings(
        NOMINATIM_MAX_PER_DAY=10,
        NOMINATIM_BACKFILL_MAX_PER_DAY=6,
        NOMINATIM_MIN_INTERVAL_SEC=0,
    )
    def test_backfill_stops_before_interactive_budget(self):
        from datetime import date

        from matesla.models.AddressFromLatLong import (
            NOMINATIM_PURPOSE_BACKFILL,
            NOMINATIM_PURPOSE_INTERACTIVE,
            NominatimDailyQuota,
            _acquire_nominatim_slot,
        )

        today = date.today()
        NominatimDailyQuota.objects.create(
            day=today, call_count=6, last_call_at=timezone.now()
        )
        self.assertFalse(
            _acquire_nominatim_slot(purpose=NOMINATIM_PURPOSE_BACKFILL)
        )
        self.assertTrue(
            _acquire_nominatim_slot(purpose=NOMINATIM_PURPOSE_INTERACTIVE)
        )
        row = NominatimDailyQuota.objects.get(day=today)
        self.assertEqual(row.call_count, 7)

    @override_settings(
        NOMINATIM_MAX_PER_DAY=5,
        NOMINATIM_BACKFILL_MAX_PER_DAY=5,
        NOMINATIM_MIN_INTERVAL_SEC=0,
    )
    def test_interactive_hard_cap(self):
        from datetime import date

        from matesla.models.AddressFromLatLong import (
            NOMINATIM_PURPOSE_INTERACTIVE,
            NominatimDailyQuota,
            _acquire_nominatim_slot,
        )

        today = date.today()
        NominatimDailyQuota.objects.create(
            day=today, call_count=5, last_call_at=timezone.now()
        )
        self.assertFalse(
            _acquire_nominatim_slot(purpose=NOMINATIM_PURPOSE_INTERACTIVE)
        )

    @override_settings(
        NOMINATIM_MAX_PER_DAY=20,
        NOMINATIM_BACKFILL_MAX_PER_DAY=10,
        NOMINATIM_MIN_INTERVAL_SEC=0,
    )
    def test_backfill_get_address_uses_backfill_purpose(self):
        """enrich path must not consume interactive-only headroom."""
        from datetime import date
        from unittest.mock import patch

        from matesla.models.AddressFromLatLong import (
            NOMINATIM_PURPOSE_BACKFILL,
            NominatimDailyQuota,
            GetAddressFromLatLong,
        )

        today = date.today()
        NominatimDailyQuota.objects.create(
            day=today, call_count=10, last_call_at=timezone.now()
        )
        with patch(
            "matesla.models.AddressFromLatLong._nominatim_reverse",
            return_value=None,
        ) as reverse_mock:
            result = GetAddressFromLatLong(
                50.1, 4.1, purpose=NOMINATIM_PURPOSE_BACKFILL
            )
        self.assertEqual(result, "Unknown")
        # reverse is still called once; slot denied inside → returns None
        reverse_mock.assert_called()
        # call_count unchanged if reverse mocks and skips acquire — mock replaces
        # whole reverse. Check enrich_addresses_once wires purpose instead:
        from matesla.geo_enrich import enrich_addresses_once

        with patch(
            "matesla.geo_enrich.GetAddressFromLatLong", return_value="Unknown"
        ) as get_mock:
            with patch(
                "matesla.geo_enrich._collect_grids_missing_address",
                return_value=[(50.2, 4.2)],
            ):
                enrich_addresses_once(max_calls=1)
        get_mock.assert_called_once_with(
            50.2, 4.2, purpose=NOMINATIM_PURPOSE_BACKFILL
        )


class CarRoadsFormatTests(TestCase):
    def test_prefers_road_over_footway(self):
        from matesla.models.AddressFromLatLong import _format_from_components

        # Only footway → no car road → may still use place/locality only
        only_path = _format_from_components(
            {
                "footway": "Allée Jacques Brel",
                "city": "Uccle",
                "postcode": "1180",
                "country": "Belgique",
            },
            car_roads_only=True,
        )
        self.assertIsNotNone(only_path)
        self.assertNotIn("Jacques Brel", only_path or "")

        with_road = _format_from_components(
            {
                "house_number": "45",
                "road": "Rue Rouge",
                "footway": "Allée Jacques Brel",
                "city": "Uccle",
                "postcode": "1180",
                "country": "Belgique",
            },
            car_roads_only=True,
        )
        self.assertIn("Rue Rouge", with_road or "")
        self.assertNotIn("Jacques Brel", with_road or "")


class GeoapifyProviderTests(TestCase):
    @override_settings(GEOAPIFY_API_KEY="test-key-xyz", GEOAPIFY_MIN_INTERVAL_SEC=0)
    def test_active_geocoder_geoapify(self):
        from matesla.models.AddressFromLatLong import active_geocoder

        self.assertEqual(active_geocoder(), "geoapify")

    @override_settings(GEOAPIFY_API_KEY="")
    def test_active_geocoder_nominatim_without_key(self):
        from matesla.models.AddressFromLatLong import active_geocoder

        # Clear env leakage for this process if any
        with patch.dict("os.environ", {"GEOAPIFY_API_KEY": "", "GEOAPIFY_KEY": ""}, clear=False):
            self.assertEqual(active_geocoder(), "nominatim")

    @override_settings(
        GEOAPIFY_API_KEY="test-key-xyz",
        GEOAPIFY_MIN_INTERVAL_SEC=0,
        GEOAPIFY_MAX_PER_DAY=50,
        GEOAPIFY_BACKFILL_MAX_PER_DAY=40,
    )
    def test_get_address_uses_geoapify(self):
        from matesla.models.AddressFromLatLong import GetAddressFromLatLong

        fake_json = {
            "features": [
                {
                    "properties": {
                        "housenumber": "45",
                        "street": "Rue Rouge",
                        "city": "Uccle",
                        "postcode": "1180",
                        "country": "Belgique",
                        "formatted": "Rue Rouge 45, 1180 Uccle, Belgique",
                    }
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fake_json

        with patch(
            "matesla.models.AddressFromLatLong.requests.get", return_value=mock_resp
        ) as get_mock:
            result = GetAddressFromLatLong(50.801154, 4.34285)

        self.assertIn("Rue Rouge", result)
        self.assertIn("Uccle", result)
        get_mock.assert_called_once()
        self.assertIn("geoapify.com", get_mock.call_args[0][0])
        # Cached for second call
        with patch(
            "matesla.models.AddressFromLatLong.requests.get"
        ) as get_again:
            again = GetAddressFromLatLong(50.801154, 4.34285)
        self.assertEqual(again, result)
        get_again.assert_not_called()

    @override_settings(
        GEOAPIFY_API_KEY="test-key-xyz",
        GEOAPIFY_MIN_INTERVAL_SEC=0,
        GEOAPIFY_MAX_PER_DAY=2500,
    )
    def test_geoapify_quota_defaults_higher(self):
        from matesla.models.AddressFromLatLong import _geocode_max_per_day

        self.assertEqual(_geocode_max_per_day(), 2500)
