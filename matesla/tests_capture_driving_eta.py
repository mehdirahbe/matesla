"""Driving poll interval from navigation ETA (no Tesla network)."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from matesla.capture import (
    INTERVAL_DC_CHARGE_MIN,
    INTERVAL_DRIVING_ARRIVAL_MIN,
    INTERVAL_DRIVING_FAR_MIN,
    INTERVAL_DRIVING_MID_MIN,
    INTERVAL_DRIVING_MIN,
    base_poll_interval_minutes,
    driving_poll_interval_minutes,
)


def _snap(**kwargs):
    base = {
        "speed": 70.0,
        "active_route_minutes_to_arrival": None,
        "active_route_miles_to_arrival": None,
        "active_route_destination": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class DrivingEtaIntervalTests(SimpleTestCase):
    def test_no_eta_stays_dense(self):
        self.assertEqual(driving_poll_interval_minutes(_snap()), INTERVAL_DRIVING_MIN)
        self.assertEqual(driving_poll_interval_minutes(None), INTERVAL_DRIVING_MIN)

    def test_far_eta_stretches(self):
        self.assertEqual(
            driving_poll_interval_minutes(_snap(active_route_minutes_to_arrival=90)),
            INTERVAL_DRIVING_FAR_MIN,
        )

    def test_mid_eta(self):
        self.assertEqual(
            driving_poll_interval_minutes(_snap(active_route_minutes_to_arrival=25)),
            INTERVAL_DRIVING_MID_MIN,
        )

    def test_near_eta_dense(self):
        self.assertEqual(
            driving_poll_interval_minutes(_snap(active_route_minutes_to_arrival=8)),
            INTERVAL_DRIVING_MIN,
        )

    def test_arrival_eta_one_minute(self):
        self.assertEqual(
            driving_poll_interval_minutes(_snap(active_route_minutes_to_arrival=3)),
            INTERVAL_DRIVING_ARRIVAL_MIN,
        )
        self.assertEqual(
            driving_poll_interval_minutes(_snap(active_route_minutes_to_arrival=1.2)),
            INTERVAL_DRIVING_ARRIVAL_MIN,
        )

    def test_supercharger_dest_near_is_one_minute(self):
        self.assertEqual(
            driving_poll_interval_minutes(
                _snap(
                    active_route_minutes_to_arrival=10,
                    active_route_destination="Superchargeur Arc-sur-Tille, France",
                )
            ),
            INTERVAL_DRIVING_ARRIVAL_MIN,
        )

    def test_non_charger_dest_near_stays_two_minutes(self):
        self.assertEqual(
            driving_poll_interval_minutes(
                _snap(
                    active_route_minutes_to_arrival=10,
                    active_route_destination="14 Rue Général Micheler",
                )
            ),
            INTERVAL_DRIVING_MIN,
        )

    def test_crawl_overrides_far_eta(self):
        self.assertEqual(
            driving_poll_interval_minutes(
                _snap(speed=10.0, active_route_minutes_to_arrival=90)
            ),
            INTERVAL_DRIVING_MIN,
        )

    def test_dc_charge_is_one_minute(self):
        self.assertEqual(INTERVAL_DC_CHARGE_MIN, 1)
        self.assertEqual(
            base_poll_interval_minutes("dc_charge", night=False),
            INTERVAL_DC_CHARGE_MIN,
        )
        self.assertEqual(
            base_poll_interval_minutes("dc_charge", night=True),
            INTERVAL_DC_CHARGE_MIN,
        )
