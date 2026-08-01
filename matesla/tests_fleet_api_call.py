"""Tests for FleetApiCall billing log (no Tesla network)."""

from django.test import TestCase
from django.utils import timezone

from matesla.models.FleetApiCall import (
    FleetApiCall,
    KIND_VEHICLE_DATA,
    record_vehicle_data_call,
)
from matesla.models.VinHash import HashTheVin
from personalstats.views import _fleet_poll_buckets


class FleetApiCallTests(TestCase):
    def test_record_billable_under_500(self):
        vin = "5YJ3E7EB1KFTESTLOG"
        row = record_vehicle_data_call(
            http_status=200,
            vin=vin,
            source="capture",
        )
        self.assertIsNotNone(row)
        self.assertTrue(row.billable)
        self.assertEqual(row.kind, KIND_VEHICLE_DATA)
        self.assertEqual(row.hashedVin, HashTheVin(vin))
        self.assertEqual(FleetApiCall.objects.count(), 1)

    def test_record_408_still_billable(self):
        row = record_vehicle_data_call(http_status=408, vin="5YJ3E7EB1KFTEST408")
        self.assertTrue(row.billable)

    def test_network_fail_not_billable(self):
        row = record_vehicle_data_call(
            http_status=None, vin="5YJ3E7EB1KFTESTNET", detail="timeout"
        )
        self.assertFalse(row.billable)

    def test_fleet_poll_buckets_counts_billable_only(self):
        vin = "5YJ3E7EB1KFTESTBKT"
        hashed = HashTheVin(vin)
        now = timezone.now()
        record_vehicle_data_call(http_status=200, vin=vin, when=now)
        record_vehicle_data_call(http_status=408, vin=vin, when=now)
        record_vehicle_data_call(http_status=None, vin=vin, when=now)
        labels, counts = _fleet_poll_buckets(hashed, days=7)
        self.assertEqual(len(labels), 7)
        self.assertEqual(sum(counts), 2)
