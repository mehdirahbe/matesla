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
