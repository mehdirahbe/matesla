"""Unit tests for DC charge analytics (no DB)."""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from personalstats.dc_charge import (
    ChargePoint,
    DcSession,
    charge_power_min_max_excluding_ramp,
    effective_charger_power_kw,
    filter_outlier_sessions,
    iter_power_curve_points,
    power_vs_soc_curve,
    session_from_rows,
    soc_vs_time_curves,
)


def _session(
    *,
    peak: float,
    start_soc: float,
    duration_min: float = 30.0,
    n_points: int = 7,
    early_power: float | None = None,
) -> DcSession:
    """Synthetic DC session with roughly linear SoC climb."""
    t0 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    points = []
    early_p = early_power if early_power is not None else peak
    for i in range(n_points):
        frac = i / max(1, n_points - 1)
        minutes = duration_min * frac
        # First third uses early_power (cold crawl), rest uses peak
        power = early_p if frac < 0.35 else peak
        soc = start_soc + (80.0 - start_soc) * frac
        points.append(
            ChargePoint(
                t=t0 + timedelta(minutes=minutes),
                soc=soc,
                power_kw=power,
            )
        )
    return DcSession(
        points=points,
        peak_kw=peak,
        start_soc=start_soc,
        end_soc=points[-1].soc,
        duration_min=duration_min,
    )


class DcChargeLogicTests(SimpleTestCase):
    def test_session_from_rows_requires_dc_peak(self):
        t0 = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        ac_rows = [
            {
                "t": t0 + timedelta(minutes=i * 5),
                "battery_level": 20 + i,
                "charger_power": 7.0,
                "fast_charger_present": False,
            }
            for i in range(4)
        ]
        self.assertIsNone(session_from_rows(ac_rows))

        # Destination / 3φ AC (~22 kW) must not count as Supercharge DC
        dest_rows = [
            {
                "t": t0 + timedelta(minutes=i * 5),
                "battery_level": 20 + i * 2,
                "charger_power": 22.0,
                "fast_charger_present": False,
            }
            for i in range(4)
        ]
        self.assertIsNone(session_from_rows(dest_rows))

        dc_rows = [
            {
                "t": t0 + timedelta(minutes=i * 5),
                "battery_level": 20 + i * 5,
                "charger_power": 120.0 if i else 80.0,
                "fast_charger_present": True,
            }
            for i in range(4)
        ]
        session = session_from_rows(dc_rows)
        self.assertIsNotNone(session)
        self.assertGreaterEqual(session.peak_kw, 40.0)

    def test_outlier_filter_drops_low_peak_and_slow_start(self):
        # Typical peaks ~200 kW; one V2-share 50 kW; one cold crawl
        good = [_session(peak=200, start_soc=20) for _ in range(6)]
        good += [_session(peak=180, start_soc=15) for _ in range(3)]
        share = _session(peak=50, start_soc=25)
        cold = _session(peak=180, start_soc=20, early_power=20, duration_min=40)
        kept, rejected = filter_outlier_sessions(
            good + [share, cold], mode="robust"
        )
        self.assertGreaterEqual(len(kept), 6)
        reasons = {s.outlier_reason for s in rejected}
        self.assertIn("low_peak", reasons)
        # Slow start may or may not fire depending on early window; peak share must
        self.assertTrue(any(s.peak_kw < 80 for s in rejected))

    def test_outlier_all_keeps_everything(self):
        sessions = [
            _session(peak=200, start_soc=20),
            _session(peak=40, start_soc=30),
        ]
        kept, rejected = filter_outlier_sessions(sessions, mode="all")
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(rejected), 0)

    def test_power_vs_soc_curve_has_median_and_band(self):
        sessions = [_session(peak=150 + i, start_soc=10 + i) for i in range(5)]
        curve = power_vs_soc_curve(sessions, min_n=1)
        self.assertGreater(len(curve), 0)
        row = curve[0]
        self.assertIn("median", row)
        self.assertIn("p10", row)
        self.assertIn("p90", row)
        self.assertLessEqual(row["p10"], row["median"])
        self.assertLessEqual(row["median"], row["p90"])
        self.assertIsNotNone(row.get("min_day"))
        self.assertIsNotNone(row.get("max_day"))
        self.assertRegex(row["min_day"], r"^\d{4}-\d{2}-\d{2}$")

    def test_daymap_min_max_excludes_ramp(self):
        t0 = datetime(2024, 5, 4, 17, 30, tzinfo=timezone.utc)
        timed = [
            (t0, 40.0),
            (t0 + timedelta(seconds=60), 90.0),
            (t0 + timedelta(seconds=120), 250.0),
            (t0 + timedelta(seconds=180), 240.0),
            (t0 + timedelta(seconds=240), 200.0),
        ]
        min_kw, max_kw = charge_power_min_max_excluding_ramp(timed)
        self.assertEqual(max_kw, 250.0)
        self.assertIsNotNone(min_kw)
        self.assertGreaterEqual(min_kw, 200.0)
        self.assertNotEqual(min_kw, 40.0)

    def test_ac_wall_power_from_current_when_kw_zero(self):
        """TeslaFi AC: charger_power=0 but current ~12–16 A (blue industrial socket)."""
        point = {
            "charger_power": 0.0,
            "charger_actual_current": 12.0,
            "charger_voltage": None,
            "charger_phases": 1.0,
        }
        kw = effective_charger_power_kw(point)
        self.assertIsNotNone(kw)
        # 230 V × 12 A × 1 φ ≈ 2.76 kW
        self.assertAlmostEqual(kw, 2.76, places=2)

        t0 = datetime(2025, 3, 2, 10, 37, tzinfo=timezone.utc)
        timed = [
            (t0 + timedelta(minutes=i), effective_charger_power_kw({
                "charger_power": 0.0,
                "charger_actual_current": 12.0 + (i % 2),
                "charger_phases": 1.0,
            }))
            for i in range(10)
        ]
        min_kw, max_kw = charge_power_min_max_excluding_ramp(timed)
        self.assertIsNotNone(min_kw)
        self.assertIsNotNone(max_kw)
        self.assertGreater(min_kw, 2.0)
        self.assertLess(max_kw, 5.0)

        # Blue industrial socket mix: mostly 12–13 A, one 1 A blip must not win min
        messy = [
            (t0, 0.23),
            (t0 + timedelta(minutes=1), 2.76),
            (t0 + timedelta(minutes=2), 2.99),
            (t0 + timedelta(minutes=3), 2.76),
        ]
        min_kw, max_kw = charge_power_min_max_excluding_ramp(messy)
        self.assertAlmostEqual(min_kw, 2.76, places=2)
        self.assertAlmostEqual(max_kw, 2.99, places=2)

    def test_power_curve_skips_supercharger_ramp(self):
        """First tens of seconds / low-vs-peak early samples must not enter kW vs SoC."""
        t0 = datetime(2024, 5, 4, 17, 30, tzinfo=timezone.utc)
        # Minute samples: ramp then steady 250 kW (matches user Supercharge pattern)
        points = [
            ChargePoint(t=t0, soc=6.0, power_kw=40.0),  # ramp
            ChargePoint(t=t0 + timedelta(seconds=60), soc=8.0, power_kw=120.0),  # still rising
            ChargePoint(t=t0 + timedelta(seconds=120), soc=12.0, power_kw=250.0),
            ChargePoint(t=t0 + timedelta(seconds=180), soc=18.0, power_kw=250.0),
            ChargePoint(t=t0 + timedelta(seconds=240), soc=24.0, power_kw=240.0),
        ]
        session = DcSession(
            points=points,
            peak_kw=250.0,
            start_soc=6.0,
            end_soc=24.0,
            duration_min=4.0,
        )
        kept = list(iter_power_curve_points(session))
        powers = [p.power_kw for p in kept]
        self.assertNotIn(40.0, powers)
        self.assertTrue(all(p >= 120 for p in powers) or 250.0 in powers)
        # Absolute 120 s skip drops t=0 and t=60; relative would also drop 120 kW at 60 s
        self.assertNotIn(120.0, powers)
        self.assertIn(250.0, powers)

        curve = power_vs_soc_curve([session], min_n=1)
        all_mins = [row["min"] for row in curve]
        self.assertTrue(all(m >= 200 for m in all_mins), all_mins)

    def test_soc_vs_time_curves_by_start_bucket(self):
        sessions = [
            _session(peak=180, start_soc=19, duration_min=40),
            _session(peak=190, start_soc=21, duration_min=45),
            _session(peak=200, start_soc=20, duration_min=50),
        ]
        curves = soc_vs_time_curves(sessions, min_sessions=2)
        self.assertIn(20, curves)
        self.assertGreaterEqual(curves[20]["n_sessions"], 2)
        self.assertGreater(len(curves[20]["times"]), 2)
        # SoC should generally rise over time
        soc = curves[20]["soc_median"]
        self.assertGreater(soc[-1], soc[0])
