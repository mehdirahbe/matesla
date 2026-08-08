"""
DC (fast) charge analytics: power vs SoC curves and SoC-vs-time by start SoC.

Built from TeslaCarDataSnapshot charge samples. Filters target common
skews in real Supercharging:

- V2 stall sharing (session peak far below this car's usual peaks)
- Cold / unpreconditioned starts (very low power in the first minutes)
- Supercharger power ramp in the first tens of seconds (first samples of a
  session are far below the true curve — drop them for power-vs-SoC)

Default mode is robust (MAD on peak + slow-start gate). Use mode ``all`` to
include every DC session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils.translation import gettext as _

# Same civil day as day map (user mental model)
_DC_DAY_TZ = ZoneInfo("Europe/Brussels")

# Point-level: below this is AC / noise (same idea as capture DC_POWER_KW_MIN).
DC_POWER_KW_MIN = 12.0
# Session-level: real Supercharging / CCS. Destination AC (≤22 kW 3φ) and wall
# AC must NOT enter the curves — a 12 kW floor was mixing them in and pulled
# medians / P10 down to ~11 kW mid-SoC.
DC_SESSION_PEAK_KW_MIN = 40.0
CHARGE_SESSION_MIN_MINUTES = 5.0
CHARGE_SESSION_GAP = timedelta(minutes=30)
# Safety cap only — query is pre-filtered to DC-ish rows (~8k for multi-year).
DC_CHARGE_MAX_SAMPLES = 200000

# Supercharger contactors / power ramp: first tens of seconds are not the
# steady curve. Session start = first sample we have for that session (we do
# not get a Tesla "plug-in" event). With ~1 min TeslaFi samples, 120 s drops
# the first one or two points; the relative rule catches a slow ramp longer.
DC_RAMP_SKIP_SECONDS = 120.0
DC_RAMP_RELATIVE_WINDOW_SECONDS = 180.0
DC_RAMP_PEAK_FRAC = 0.45  # below 45% of session peak → still ramping

SOC_BIN_WIDTH = 2.0  # %
SOC_BIN_MIN_N = 3
START_SOC_BUCKETS = (10, 20, 30, 40, 50)
START_SOC_TOLERANCE = 5.0  # bucket 20 → start in [15, 25)
SOC_VS_TIME_MAX_MINUTES = 90
SOC_VS_TIME_STEP_MIN = 1
SOC_VS_TIME_MIN_SESSIONS = 2

OUTLIER_MODES = frozenset({"robust", "all"})
ENVELOPE_MODES = frozenset({"p10_p90", "min_max"})


@dataclass
class ChargePoint:
    t: object  # datetime
    soc: float
    power_kw: float
    outside_temp: float | None = None


@dataclass
class DcSession:
    points: list[ChargePoint] = field(default_factory=list)
    peak_kw: float = 0.0
    start_soc: float | None = None
    end_soc: float | None = None
    duration_min: float = 0.0
    outlier_reason: str | None = None

    def early_mean_power(self, minutes: float = 10.0) -> float | None:
        if not self.points:
            return None
        t0 = self.points[0].t
        vals = []
        for point in self.points:
            elapsed = (point.t - t0).total_seconds() / 60.0
            if elapsed > minutes:
                break
            if point.power_kw is not None and point.power_kw > 0.5:
                vals.append(float(point.power_kw))
        if not vals:
            return None
        return sum(vals) / len(vals)


def _percentile_sorted(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear interpolation percentile; ``sorted_vals`` must be non-empty sorted."""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    return _percentile_sorted(ordered, pct)


def _point_soc(row: dict) -> float | None:
    raw = row.get("usable_battery_level")
    if raw is None:
        raw = row.get("battery_level")
    if raw is None:
        return None
    try:
        soc = float(raw)
    except (TypeError, ValueError):
        return None
    if soc < 0 or soc > 105:
        return None
    return min(100.0, soc)


def _point_power(row: dict) -> float | None:
    raw = row.get("charger_power")
    if raw is None:
        return None
    try:
        power = float(raw)
    except (TypeError, ValueError):
        return None
    return power


def _dc_sample_filter_q() -> Q:
    """
    Rows that can belong to a DC / Supercharge session.

    Do NOT use bare charging_state — that pulls every AC wall charge and
    exhausts the sample budget on multi-year histories (e.g. 178k AC rows
    before recent 250 kW Supercharges are ever seen).
    """
    return Q(charger_power__gte=DC_POWER_KW_MIN) | Q(fast_charger_present=True)


def iter_charge_sessions_from_queryset(queryset) -> Iterable[list[dict]]:
    """
    Yield raw DC-ish sample dicts grouped into sessions (gap / min duration).

    Pre-filters to power ≥ 12 kW or fast_charger_present so full history fits
    and AC home charging never enters the stream.
    """
    fields = (
        "Date",
        "battery_level",
        "usable_battery_level",
        "charger_power",
        "outside_temp",
        "fast_charger_present",
        "fast_charger_type",
    )
    base = (
        queryset.filter(_dc_sample_filter_q())
        .order_by("Date")
        .values(*fields)
    )
    current: list[dict] = []
    kept = 0
    for row in base.iterator(chunk_size=4000):
        sample_time = row.get("Date")
        if sample_time is None:
            continue
        kept += 1
        if kept > DC_CHARGE_MAX_SAMPLES:
            break
        point = dict(row)
        point["t"] = sample_time
        if current and (sample_time - current[-1]["t"]) > CHARGE_SESSION_GAP:
            if _raw_session_ok(current):
                yield current
            current = [point]
        else:
            current.append(point)
    if current and _raw_session_ok(current):
        yield current


def _raw_session_ok(session_rows: list[dict]) -> bool:
    if len(session_rows) < 2:
        return False
    duration = (
        max(0.0, (session_rows[-1]["t"] - session_rows[0]["t"]).total_seconds()) / 60.0
    )
    return duration >= CHARGE_SESSION_MIN_MINUTES


def session_from_rows(rows: list[dict]) -> DcSession | None:
    points: list[ChargePoint] = []
    peak = 0.0
    saw_fast = False
    for row in rows:
        if row.get("fast_charger_present"):
            saw_fast = True
        soc = _point_soc(row)
        power = _point_power(row)
        if soc is None:
            continue
        power_kw = float(power) if power is not None else 0.0
        # Skip sub-DC noise inside a session (should be rare after SQL filter)
        if power_kw < DC_POWER_KW_MIN and not row.get("fast_charger_present"):
            continue
        if power_kw > peak:
            peak = power_kw
        outside = row.get("outside_temp")
        try:
            outside_f = float(outside) if outside is not None else None
        except (TypeError, ValueError):
            outside_f = None
        points.append(
            ChargePoint(
                t=row["t"],
                soc=soc,
                power_kw=power_kw,
                outside_temp=outside_f,
            )
        )
    if len(points) < 2:
        return None
    duration = max(0.0, (points[-1].t - points[0].t).total_seconds()) / 60.0
    if duration < CHARGE_SESSION_MIN_MINUTES:
        return None
    # True DC gate: exclude destination AC (~11–22 kW) that cleared the 12 kW floor
    if peak >= DC_SESSION_PEAK_KW_MIN:
        pass
    elif saw_fast and peak >= DC_POWER_KW_MIN:
        # Flagged Supercharger/CCS but peak slightly under floor (shared V2 edge)
        pass
    else:
        return None
    return DcSession(
        points=points,
        peak_kw=peak,
        start_soc=points[0].soc,
        end_soc=points[-1].soc,
        duration_min=duration,
    )


def load_dc_sessions(queryset) -> list[DcSession]:
    sessions: list[DcSession] = []
    for rows in iter_charge_sessions_from_queryset(queryset):
        session = session_from_rows(rows)
        if session is not None:
            sessions.append(session)
    return sessions


def filter_outlier_sessions(
    sessions: Sequence[DcSession], *, mode: str = "robust"
) -> tuple[list[DcSession], list[DcSession]]:
    """
    Return (kept, rejected).

    ``robust`` (default):
      1) MAD lower fence on session peak kW (catches V2 share vs this car's
         usual Supercharge peaks).
      2) Slow-start gate: mean power in first 10 min < 30% of session peak
         on sessions ≥ 15 min (cold / unpreconditioned crawls).
    ``all``: keep every DC session.
    """
    mode = (mode or "robust").strip().lower()
    if mode not in OUTLIER_MODES:
        mode = "robust"
    if mode == "all" or len(sessions) < 4:
        return list(sessions), []

    peaks = [s.peak_kw for s in sessions if s.peak_kw > 0]
    if len(peaks) < 4:
        return list(sessions), []

    med_peak = median(peaks)
    abs_dev = [abs(p - med_peak) for p in peaks]
    mad = median(abs_dev) if abs_dev else 0.0
    # 1.4826 ≈ consistency constant for normal MAD → σ
    sigma = 1.4826 * mad if mad > 1e-6 else med_peak * 0.15
    mad_lower = med_peak - 2.5 * sigma
    # Relative floor: severe share / crawl relative to *this* car's median peak
    relative_floor = med_peak * 0.40
    peak_floor = max(DC_POWER_KW_MIN, mad_lower, relative_floor)

    kept: list[DcSession] = []
    rejected: list[DcSession] = []
    for session in sessions:
        if session.peak_kw < peak_floor:
            session.outlier_reason = "low_peak"
            rejected.append(session)
            continue
        early = session.early_mean_power(10.0)
        if (
            session.duration_min >= 15.0
            and early is not None
            and session.peak_kw > 0
            and early < 0.30 * session.peak_kw
        ):
            session.outlier_reason = "slow_start"
            rejected.append(session)
            continue
        kept.append(session)
    # Safety: never discard everything
    if not kept and sessions:
        return list(sessions), []
    return kept, rejected


def _sample_day_iso(sample_time) -> str | None:
    """Civil date (Europe/Brussels) for day-map links."""
    if sample_time is None:
        return None
    try:
        if getattr(sample_time, "tzinfo", None) is None:
            sample_time = sample_time.replace(tzinfo=_DC_DAY_TZ)
        return sample_time.astimezone(_DC_DAY_TZ).date().isoformat()
    except Exception:
        return None


def _is_supercharger_ramp_sample(
    session: DcSession, point: ChargePoint, *, elapsed_s: float
) -> bool:
    """
    True if this sample is still in the post-plug power ramp (not steady DC).

    We only know session start from our first telemetry sample (no Fleet
    “charge started” event). Absolute skip + “far below this session’s peak”
    in the first minutes removes the fake low-kW points that inflate the
    min envelope and pull the median down at low SoC.
    """
    if elapsed_s < DC_RAMP_SKIP_SECONDS:
        return True
    peak = float(session.peak_kw or 0.0)
    if (
        elapsed_s < DC_RAMP_RELATIVE_WINDOW_SECONDS
        and peak > 0
        and point.power_kw is not None
        and float(point.power_kw) < DC_RAMP_PEAK_FRAC * peak
    ):
        return True
    return False


def iter_power_curve_points(session: DcSession):
    """Yield charge points suitable for power-vs-SoC (skip AC noise + ramp)."""
    if not session.points:
        return
    t0 = session.points[0].t
    for point in session.points:
        if point.power_kw is None or point.power_kw < DC_POWER_KW_MIN:
            continue
        try:
            elapsed_s = (point.t - t0).total_seconds()
        except Exception:
            elapsed_s = 0.0
        if elapsed_s < 0:
            elapsed_s = 0.0
        if _is_supercharger_ramp_sample(session, point, elapsed_s=elapsed_s):
            continue
        yield point


def effective_charger_power_kw(point: dict) -> float | None:
    """
    Best available kW for one telemetry sample.

    Fleet/TeslaFi often leave ``charger_power`` at 0 on AC wall charging while
    still reporting ``charger_actual_current`` (and sometimes voltage/phases).
    Estimate P ≈ V × I × phases (EU default V=230 V when voltage is missing).
    """
    raw = point.get("charger_power")
    if raw is not None:
        try:
            power = float(raw)
            if power >= 0.3:
                return power
        except (TypeError, ValueError):
            pass

    current = point.get("charger_actual_current")
    if current is None:
        return None
    try:
        amps = float(current)
    except (TypeError, ValueError):
        return None
    if amps < 0.5:
        return None

    voltage = point.get("charger_voltage")
    try:
        volts = float(voltage) if voltage is not None else None
    except (TypeError, ValueError):
        volts = None
    # Missing / nonsense (seen 1.7–2.0 on some exports) → EU single-phase nominal
    if volts is None or volts < 50 or volts > 480:
        volts = 230.0

    phases = point.get("charger_phases")
    try:
        phase_count = float(phases) if phases is not None else 1.0
    except (TypeError, ValueError):
        phase_count = 1.0
    if phase_count < 1.0:
        phase_count = 1.0

    return (volts * amps * phase_count) / 1000.0


def charge_power_min_max_excluding_ramp(
    timed_powers: Sequence[tuple[object, float]],
) -> tuple[float | None, float | None]:
    """
    Min/max charger power for a charge segment after Supercharger ramp skip.

    ``timed_powers``: ordered (timestamp, power_kw) samples for one stop
    (already effective kW — see ``effective_charger_power_kw``).

    - AC / destination (session peak &lt; DC_SESSION_PEAK_KW_MIN): plain min/max,
      no ramp filter (there is no Supercharger power ramp).
    - DC: same rules as power-vs-SoC curves (absolute + relative-to-peak windows).
    """
    samples: list[tuple[object, float]] = []
    for sample_time, power in timed_powers:
        if sample_time is None or power is None:
            continue
        try:
            power_f = float(power)
        except (TypeError, ValueError):
            continue
        if power_f < 0.3:
            continue
        samples.append((sample_time, power_f))
    if not samples:
        return None, None
    peak = max(p for _, p in samples)

    # AC wall / slow destination: plain min/max (no Supercharger ramp).
    # Ignore sub-1 kW blips (1 A contactors / end of session noise).
    if peak < DC_SESSION_PEAK_KW_MIN:
        solid = [p for _, p in samples if p >= 1.0]
        if solid:
            return min(solid), max(solid)
        return min(p for _, p in samples), peak

    # DC Supercharge: drop plug-in ramp
    t0 = samples[0][0]
    kept: list[float] = []
    for sample_time, power_f in samples:
        try:
            elapsed_s = (sample_time - t0).total_seconds()
        except Exception:
            elapsed_s = 0.0
        if elapsed_s < 0:
            elapsed_s = 0.0
        if elapsed_s < DC_RAMP_SKIP_SECONDS:
            continue
        if (
            elapsed_s < DC_RAMP_RELATIVE_WINDOW_SECONDS
            and peak > 0
            and power_f < DC_RAMP_PEAK_FRAC * peak
        ):
            continue
        # Ignore residual AC-scale noise inside a DC session
        if power_f < DC_POWER_KW_MIN:
            continue
        kept.append(power_f)
    if not kept:
        # Very short DC stop: fall back to raw peak only
        return None, peak
    return min(kept), max(kept)


def power_vs_soc_curve(
    sessions: Sequence[DcSession],
    *,
    bin_width: float = SOC_BIN_WIDTH,
    min_n: int = SOC_BIN_MIN_N,
) -> list[dict]:
    """
    Aggregate charger_power by SoC bin.

    Each row: soc_center, n, median, mean, p10, p90, min, max,
    plus min_day / max_day (ISO YYYY-MM-DD) and min_at / max_at for drill-down
    to the day map when the envelope is min/max.

    Excludes Supercharger ramp samples at the start of each session.
    """
    if bin_width <= 0:
        bin_width = SOC_BIN_WIDTH
    # idx → list of (power_kw, sample_time, exact_soc)
    buckets: dict[int, list[tuple[float, object, float]]] = {}
    for session in sessions:
        for point in iter_power_curve_points(session):
            # Bin index: 0 → [0, bin_width), …
            idx = int(point.soc // bin_width)
            if idx < 0:
                continue
            buckets.setdefault(idx, []).append(
                (float(point.power_kw), point.t, float(point.soc))
            )

    rows: list[dict] = []
    for idx in sorted(buckets):
        samples = buckets[idx]
        if len(samples) < min_n:
            continue
        powers = [s[0] for s in samples]
        ordered = sorted(powers)
        center = (idx + 0.5) * bin_width
        if center > 100:
            continue
        # First occurrence of absolute min / max (stable, for day-map link)
        min_power = ordered[0]
        max_power = ordered[-1]
        min_sample = next(s for s in samples if s[0] == min_power)
        max_sample = next(s for s in samples if s[0] == max_power)
        rows.append(
            {
                "soc": round(center, 2),
                "n": len(samples),
                "median": _percentile_sorted(ordered, 50),
                "mean": sum(powers) / len(powers),
                "p10": _percentile_sorted(ordered, 10),
                "p90": _percentile_sorted(ordered, 90),
                "min": min_power,
                "max": max_power,
                "min_day": _sample_day_iso(min_sample[1]),
                "max_day": _sample_day_iso(max_sample[1]),
                "min_soc": round(min_sample[2], 1),
                "max_soc": round(max_sample[2], 1),
            }
        )
    return rows


def power_curve_extreme_rows(power_curve: Sequence[dict]) -> list[dict]:
    """
    Flatten min/max extremes for the min–max envelope drill-down table.

    One row per (SoC bin, kind min|max) with day_iso for PersoDayMapDay.
    """
    out: list[dict] = []
    for row in power_curve:
        soc = row.get("soc")
        for kind, power_key, day_key, exact_soc_key in (
            ("min", "min", "min_day", "min_soc"),
            ("max", "max", "max_day", "max_soc"),
        ):
            day = row.get(day_key)
            power = row.get(power_key)
            if day is None or power is None:
                continue
            out.append(
                {
                    "kind": kind,
                    "soc_bin": soc,
                    "soc": row.get(exact_soc_key, soc),
                    "power_kw": power,
                    "day_iso": day,
                    "n": row.get("n"),
                    "median": row.get("median"),
                }
            )
    # Sort by SoC then min before max
    out.sort(key=lambda r: (float(r["soc_bin"] or 0), 0 if r["kind"] == "min" else 1))
    return out


def soc_vs_time_curves(
    sessions: Sequence[DcSession],
    *,
    start_buckets: Sequence[int] = START_SOC_BUCKETS,
    tolerance: float = START_SOC_TOLERANCE,
    max_minutes: int = SOC_VS_TIME_MAX_MINUTES,
    step_min: int = SOC_VS_TIME_STEP_MIN,
    min_sessions: int = SOC_VS_TIME_MIN_SESSIONS,
) -> dict[int, dict]:
    """
    For each start-SoC bucket, median SoC trajectory vs minutes since plug-in.

    Returns {bucket: {"times": [...], "soc_median": [...], "n_sessions": N}}.
    """
    grid = list(range(0, max_minutes + 1, step_min))
    # bucket → list of interpolated series (same length as grid, None if ended)
    by_bucket: dict[int, list[list[float | None]]] = {int(b): [] for b in start_buckets}

    for session in sessions:
        if session.start_soc is None or not session.points:
            continue
        bucket = None
        for candidate in start_buckets:
            if abs(session.start_soc - candidate) < tolerance:
                bucket = int(candidate)
                break
        if bucket is None:
            continue

        t0 = session.points[0].t
        series_t = [
            (p.t - t0).total_seconds() / 60.0 for p in session.points
        ]
        series_soc = [p.soc for p in session.points]
        if series_t[-1] < 1.0:
            continue

        interp: list[float | None] = []
        for minute in grid:
            if minute > series_t[-1] + 0.5:
                interp.append(None)
                continue
            # linear interpolation between surrounding samples
            if minute <= series_t[0]:
                interp.append(series_soc[0])
                continue
            j = 0
            while j < len(series_t) - 1 and series_t[j + 1] < minute:
                j += 1
            if j >= len(series_t) - 1:
                interp.append(series_soc[-1])
                continue
            t_a, t_b = series_t[j], series_t[j + 1]
            s_a, s_b = series_soc[j], series_soc[j + 1]
            if t_b <= t_a:
                interp.append(s_b)
            else:
                frac = (minute - t_a) / (t_b - t_a)
                interp.append(s_a + frac * (s_b - s_a))
        by_bucket[bucket].append(interp)

    result: dict[int, dict] = {}
    for bucket, series_list in by_bucket.items():
        if len(series_list) < min_sessions:
            continue
        med_soc: list[float] = []
        times_out: list[int] = []
        for col, minute in enumerate(grid):
            col_vals = [
                series[col]
                for series in series_list
                if series[col] is not None
            ]
            # Need enough sessions still charging at this minute
            if len(col_vals) < min_sessions:
                break
            times_out.append(minute)
            med_soc.append(median(col_vals))
        if len(times_out) < 2:
            continue
        result[bucket] = {
            "times": times_out,
            "soc_median": med_soc,
            "n_sessions": len(series_list),
        }
    return result


def summarize_sessions(
    kept: Sequence[DcSession], rejected: Sequence[DcSession]
) -> dict:
    peaks = [s.peak_kw for s in kept if s.peak_kw > 0]
    return {
        "n_kept": len(kept),
        "n_rejected": len(rejected),
        "n_total_dc": len(kept) + len(rejected),
        "peak_max_kw": max(peaks) if peaks else None,
        "peak_median_kw": median(peaks) if peaks else None,
        "n_low_peak": sum(1 for s in rejected if s.outlier_reason == "low_peak"),
        "n_slow_start": sum(1 for s in rejected if s.outlier_reason == "slow_start"),
    }


def outlier_mode_label(mode: str) -> str:
    if mode == "all":
        return _("All DC sessions")
    return _("Robust (drop V2 share / cold crawl)")


def envelope_mode_label(mode: str) -> str:
    if mode == "min_max":
        return _("Min / max")
    return _("P10–P90 band")


def charge_session_curve_series(points: Sequence[dict]) -> list[dict]:
    """
    Build plot series for one day-map charge stop (power vs time / SoC).

    Each row: elapsed_min, soc, power_kw. Includes the Supercharger ramp —
    this is a single-session detail curve, not the aggregate envelope.
    Samples without usable SoC or power are skipped.
    """
    if not points:
        return []
    ordered = [p for p in points if p.get("t") is not None]
    if not ordered:
        return []
    ordered = sorted(ordered, key=lambda p: p["t"])
    t0 = ordered[0]["t"]
    series: list[dict] = []
    for point in ordered:
        power = effective_charger_power_kw(point)
        if power is None or power < 0.3:
            continue
        soc = _point_soc(point)
        if soc is None:
            # Day-map rows use battery_level / usable_battery_level keys too
            raw = point.get("usable_battery_level")
            if raw is None:
                raw = point.get("battery_level")
            if raw is not None:
                try:
                    soc = float(raw)
                except (TypeError, ValueError):
                    soc = None
        if soc is None:
            continue
        try:
            elapsed_min = max(0.0, (point["t"] - t0).total_seconds() / 60.0)
        except Exception:
            elapsed_min = 0.0
        series.append(
            {
                "elapsed_min": elapsed_min,
                "soc": float(soc),
                "power_kw": float(power),
            }
        )
    return series
