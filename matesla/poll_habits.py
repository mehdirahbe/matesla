"""
Per-vehicle poll spacing from recent usage habits (not all-time history).

Design (hybrid):
  - Live drive/charge/cabin → short intervals (caller keeps reactive policy).
  - Idle/asleep only → habit may set 5 / 15 / 30 min for this weekday+hour:
      * busy  → 5 min  (denser than night baseline 30 — e.g. night driver)
      * moderate quiet → 15 min
      * quiet → 30 min
  - When a habit interval is set, it *replaces* the baseline for idle (not max()).
  - Training data = last HABIT_WINDOW_DAYS only (ignore old TeslaFi / prior drivers).
  - Sample unit for confidence = weeks (not correlated minutes).
  - Regime break: last HABIT_RECENT_DAYS incompatible with earlier weeks in the
    window → distrust habits (school ↔ holidays, new driver, trip south…).

Cache: LocMem/default cache, one entry per VIN, TTL 12 h (recomputed on miss).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

# Same civil clock as capture night window (household local).
HABIT_TZ = ZoneInfo("Europe/Brussels")

# --- Policy knobs ---
HABIT_WINDOW_DAYS = 84  # ~12 weeks max lookback
HABIT_RECENT_DAYS = 14  # regime-check window + excluded from reference weeks
HABIT_MIN_WEEKS = 4  # below this → no habit trust
HABIT_CI_Z = 1.64485  # ~95% one-sided Wilson (approx)
# Upper bound on activity probability → long spacing when idle.
# Note: with only 4 zero-activity weeks, Wilson p_hi≈0.53 — so thresholds
# must allow that, and k=0 gets an explicit path (see _classify_slot).
P_HI_QUIET = 0.22  # very quiet → 30 min (needs ~n≥12 zeros or low rate)
P_HI_MODERATE = 0.55  # fairly quiet → 15 min
# Zero-activity weeks: stronger spacing once we have enough empty weeks
ZERO_ACTIVE_WEEKS_FOR_QUIET = 8  # k=0, n≥8 → 30 min
INTERVAL_HABIT_QUIET_MIN = 30
INTERVAL_HABIT_MODERATE_MIN = 15
# Historically busy idle slot → denser logging (can beat night baseline 30 min)
INTERVAL_HABIT_BUSY_MIN = 5
# Week-level *mobility* rate (not mere AC charge) → denser idle polls
P_HAT_BUSY = 0.55

# Regime-break thresholds (counts over the recent window)
REGIME_ACTIVE_MISMATCH_MIN = 4  # mobility in historically calm slots
# Activity heuristics (aligned with capture.activity_kind inputs)
DRIVING_SPEED_MPH_MIN = 1.0
CACHE_TTL_SECONDS = 12 * 3600
CACHE_KEY_PREFIX = "matesla:poll_habits:v3:"


@dataclass
class SlotStats:
    """Binomial counts for one (weekday, hour) with week-level trials."""

    weeks_observed: int = 0
    weeks_active: int = 0

    @property
    def p_hat(self) -> float:
        if self.weeks_observed <= 0:
            return 0.0
        return self.weeks_active / self.weeks_observed

    @property
    def p_hi(self) -> float:
        return binomial_ci_upper(
            self.weeks_active, self.weeks_observed, z=HABIT_CI_Z
        )


@dataclass
class HabitModel:
    """Cached habit summary for one VIN."""

    vin: str
    trusted: bool
    reason: str
    weeks_in_window: int
    computed_at: datetime
    # (isoweekday 1-7, hour 0-23) → stats from reference period
    slots: dict[tuple[int, int], SlotStats] = field(default_factory=dict)
    # smoothed classes over hour±1
    quiet_hours: set[tuple[int, int]] = field(default_factory=set)
    moderate_hours: set[tuple[int, int]] = field(default_factory=set)
    busy_hours: set[tuple[int, int]] = field(default_factory=set)
    regime_break: bool = False
    regime_detail: str = ""

    def suggested_idle_interval_minutes(self, when: datetime) -> int | None:
        """
        If habits apply at `when`, return 5 (busy), 15, or 30; else None.

        Caller uses this as the idle interval (replaces baseline, not max):
          - busy → 5 min even at night (denser than night baseline 30)
          - quiet → 30 min (sparser than day baseline 5)
          - moderate → 15 min by day only; at night leave baseline 30
            (moderate means “fairly calm”, not “log more at night”)
        """
        if not self.trusted or self.regime_break:
            return None
        local = when
        hour = local.hour
        key = (local.isoweekday(), hour)
        night = hour in NIGHT_HOURS
        if key in self.busy_hours:
            return INTERVAL_HABIT_BUSY_MIN
        if key in self.quiet_hours:
            return INTERVAL_HABIT_QUIET_MIN
        if key in self.moderate_hours:
            if night:
                return None
            return INTERVAL_HABIT_MODERATE_MIN
        return None


def binomial_ci_upper(successes: int, trials: int, z: float = HABIT_CI_Z) -> float:
    """
    Upper bound of the Wilson score interval.

    Why Wilson: stable for small n and for k=0; treats *weeks* as trials.
    For k=0: falls back to 1 - alpha^(1/n) style via Wilson (alpha≈0.05 → z=1.645).
    """
    if trials <= 0:
        return 1.0
    successes = max(0, min(int(successes), int(trials)))
    trials = int(trials)
    if successes == 0:
        # One-sided 95%: P(all zero) = (1-p)^n ≥ 0.05 → p ≤ 1 - 0.05^(1/n)
        return 1.0 - (0.05 ** (1.0 / trials))
    p = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    centre = p + z2 / (2.0 * trials)
    margin = z * math.sqrt(p * (1.0 - p) / trials + z2 / (4.0 * trials * trials))
    return min(1.0, (centre + margin) / denom)


def sample_is_mobile(
    *,
    speed,
    shift_state,
    is_user_present,
) -> bool:
    """
    Drive / presence only — used for school↔holiday regime detection.

    Why not charging: overnight AC is common and would make “expected busy”
    night slots, then quiet nights look like a false regime break.
    """
    try:
        if speed is not None and float(speed) >= DRIVING_SPEED_MPH_MIN:
            return True
    except (TypeError, ValueError):
        pass
    shift = (shift_state or "").strip().upper()
    if shift in {"D", "R", "N"}:
        return True
    if is_user_present:
        return True
    return False


def sample_is_active(
    *,
    speed,
    shift_state,
    charging_state,
    power,
    is_user_present,
) -> bool:
    """True if this telemetry row needs denser polling (drive, charge, cabin…)."""
    if sample_is_mobile(
        speed=speed, shift_state=shift_state, is_user_present=is_user_present
    ):
        return True
    charging = (charging_state or "").strip()
    if charging in {"Charging", "Starting"}:
        return True
    try:
        if power is not None and abs(float(power)) >= 0.5:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _iter_activity_rows(vin: str, start: datetime, end: datetime):
    """Stream minimal columns for habit stats."""
    qs = (
        TeslaCarDataSnapshot.objects.filter(
            vin=vin, Date__gte=start, Date__lt=end
        )
        .order_by("Date")
        .values_list(
            "Date",
            "speed",
            "shift_state",
            "charging_state",
            "power",
            "is_user_present",
        )
        .iterator(chunk_size=4000)
    )
    for row in qs:
        yield row


def _week_key(dt: datetime) -> tuple[int, int]:
    iso = dt.isocalendar()
    return int(iso.year), int(iso.week)


def _build_week_slot_flags(
    rows: Iterable[tuple],
    *,
    mobile_only: bool,
) -> dict[tuple[int, int, int, int], bool]:
    """
    (iso_year, iso_week, isoweekday, hour) → any matching activity.

    Weeks without a sample in that hour are omitted (unknown, not “quiet”).
    """
    any_sample: set[tuple[int, int, int, int]] = set()
    any_hit: set[tuple[int, int, int, int]] = set()
    for date, speed, shift, charging, power, user_present in rows:
        if date is None:
            continue
        if timezone.is_naive(date):
            date = timezone.make_aware(date, timezone.utc)
        # Habits are civil (school runs, nights) — bucket in local TZ, not UTC.
        local = date.astimezone(HABIT_TZ)
        year, week = _week_key(local)
        dow = local.isoweekday()
        hour = local.hour
        key = (year, week, dow, hour)
        any_sample.add(key)
        if mobile_only:
            hit = sample_is_mobile(
                speed=speed, shift_state=shift, is_user_present=user_present
            )
        else:
            hit = sample_is_active(
                speed=speed,
                shift_state=shift,
                charging_state=charging,
                power=power,
                is_user_present=user_present,
            )
        if hit:
            any_hit.add(key)
    return {key: (key in any_hit) for key in any_sample}


def _slot_stats_from_week_map(
    week_map: dict[tuple[int, int, int, int], bool],
) -> dict[tuple[int, int], SlotStats]:
    """Aggregate week-level trials per (dow, hour)."""
    # (dow, hour) → { (year,week) → active_or }
    per_slot_weeks: dict[tuple[int, int], dict[tuple[int, int], bool]] = defaultdict(
        dict
    )
    for (year, week, dow, hour), active in week_map.items():
        slot = (dow, hour)
        yw = (year, week)
        prev = per_slot_weeks[slot].get(yw, False)
        per_slot_weeks[slot][yw] = prev or active

    stats: dict[tuple[int, int], SlotStats] = {}
    for slot, weeks in per_slot_weeks.items():
        active_weeks = sum(1 for flag in weeks.values() if flag)
        stats[slot] = SlotStats(
            weeks_observed=len(weeks), weeks_active=active_weeks
        )
    return stats


def _classify_slot_calm(slot: SlotStats) -> str | None:
    """
    Quiet / moderate only (from full activity incl. charge).

    Does not mark busy — that uses mobility-only stats (see _classify_slot_busy).
    """
    if slot.weeks_observed < HABIT_MIN_WEEKS:
        return None
    p = slot.p_hat
    if slot.weeks_active == 0:
        if slot.weeks_observed >= ZERO_ACTIVE_WEEKS_FOR_QUIET:
            return "quiet"
        return "moderate"
    if p <= 0.10 and slot.weeks_observed >= 6:
        return "quiet"
    if p <= 0.25 and slot.weeks_observed >= 4:
        return "moderate"
    if slot.p_hi <= P_HI_QUIET:
        return "quiet"
    if slot.p_hi <= P_HI_MODERATE:
        return "moderate"
    return None


def _classify_slot_busy(slot: SlotStats) -> bool:
    """True if mobility rate is high enough to densify idle polling."""
    if slot.weeks_observed < HABIT_MIN_WEEKS:
        return False
    return slot.p_hat >= P_HAT_BUSY


NIGHT_HOURS = (22, 23, 0, 1, 2, 3, 4, 5)


def _fill_night_band_from_weeks(
    ref_active_map: dict[tuple[int, int, int, int], bool],
    quiet: set[tuple[int, int]],
    moderate: set[tuple[int, int]],
    busy: set[tuple[int, int]],
) -> None:
    """
    Aggregate night band (22h–06h) per weekday when per-hour n is sparse
    (asleep → few vehicle_data rows). Can mark the whole band quiet/moderate/busy.
    """
    for dow in range(1, 8):
        week_hit: dict[tuple[int, int], bool] = {}
        for (year, week, d, hour), active in ref_active_map.items():
            if d != dow or hour not in NIGHT_HOURS:
                continue
            yw = (year, week)
            week_hit[yw] = week_hit.get(yw, False) or active
        n = len(week_hit)
        if n < HABIT_MIN_WEEKS:
            continue
        k = sum(1 for flag in week_hit.values() if flag)
        st = SlotStats(weeks_observed=n, weeks_active=k)
        # Night band fill uses the same map passed in (active or mobile).
        if _classify_slot_busy(st):
            cls = "busy"
        else:
            cls = _classify_slot_calm(st)
        if not cls:
            continue
        for hour in NIGHT_HOURS:
            key = (dow, hour)
            if cls == "busy":
                busy.add(key)
                quiet.discard(key)
                moderate.discard(key)
            elif cls == "quiet":
                if key not in busy:
                    quiet.add(key)
                    moderate.discard(key)
            elif key not in busy and key not in quiet:
                moderate.add(key)


def _smoothed_calm_class(
    stats: dict[tuple[int, int], SlotStats], dow: int, hour: int
) -> str | None:
    """
    Neighbour-aware quiet/moderate over hour±1.
    Any neighbour that is not calm → no stretch for this hour.
    """
    classes = []
    for delta in (-1, 0, 1):
        h = hour + delta
        if h < 0 or h > 23:
            continue
        slot = stats.get((dow, h))
        if slot is None or slot.weeks_observed < HABIT_MIN_WEEKS:
            continue
        classes.append(_classify_slot_calm(slot))
    if not classes:
        return None
    if any(c is None for c in classes):
        return None
    if all(c == "quiet" for c in classes):
        return "quiet"
    return "moderate"


def _smoothed_busy(
    mobile_stats: dict[tuple[int, int], SlotStats], dow: int, hour: int
) -> bool:
    """Busy if this hour or a neighbour hour is mobility-busy."""
    for delta in (-1, 0, 1):
        h = hour + delta
        if h < 0 or h > 23:
            continue
        slot = mobile_stats.get((dow, h))
        if slot is not None and _classify_slot_busy(slot):
            return True
    return False


def _daily_mobile_hours(
    week_map: dict[tuple[int, int, int, int], bool],
) -> list[float]:
    """
    Approx mobile-active hours per calendar week occurrence of each day.

    Uses week-slot flags: count hours active per (year, week, dow), then average.
    """
    per_day: dict[tuple[int, int, int], int] = defaultdict(int)
    for (year, week, dow, hour), mobile in week_map.items():
        if mobile:
            per_day[(year, week, dow)] += 1
    if not per_day:
        return []
    return [float(v) for v in per_day.values()]


def _detect_regime_break(
    ref_mobile_stats: dict[tuple[int, int], SlotStats],
    ref_mobile_map: dict[tuple[int, int, int, int], bool],
    recent_mobile_map: dict[tuple[int, int, int, int], bool],
) -> tuple[bool, str]:
    """
    Compare recent *mobility* to the reference model.

    1) Volume shift: mean mobile-hours/day recent vs reference (school↔holiday).
    2) Active surprises: mobility in slots that were historically very calm.
    """
    ref_hours = _daily_mobile_hours(ref_mobile_map)
    recent_hours = _daily_mobile_hours(recent_mobile_map)
    if ref_hours and recent_hours:
        ref_mean = sum(ref_hours) / len(ref_hours)
        recent_mean = sum(recent_hours) / len(recent_hours)
        # Much quieter than reference (e.g. school → vacation)
        if ref_mean >= 1.0 and recent_mean <= 0.35 * ref_mean:
            return (
                True,
                f"volume_drop ref_mean={ref_mean:.2f} recent_mean={recent_mean:.2f}",
            )
        # Much busier than reference (e.g. vacation → school / trip)
        if recent_mean >= max(2.5 * ref_mean, ref_mean + 2.0) and recent_mean >= 1.5:
            return (
                True,
                f"volume_rise ref_mean={ref_mean:.2f} recent_mean={recent_mean:.2f}",
            )

    active_mismatch = 0
    for (_year, _week, dow, hour), mobile in recent_mobile_map.items():
        if not mobile:
            continue
        ref = ref_mobile_stats.get((dow, hour))
        if ref is None or ref.weeks_observed < HABIT_MIN_WEEKS:
            continue
        # Historically almost never mobile in this slot
        if ref.p_hi <= 0.20 and ref.p_hat <= 0.12:
            active_mismatch += 1

    if active_mismatch >= REGIME_ACTIVE_MISMATCH_MIN:
        return (
            True,
            f"active_mismatch={active_mismatch} (mobility in historically calm slots)",
        )
    return False, ""


def compute_habit_model(vin: str, *, now: datetime | None = None) -> HabitModel:
    """Build habit model from recent snapshots only (expensive: call via cache)."""
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.utc)
    vin = (vin or "").strip()
    if not vin:
        return HabitModel(
            vin="",
            trusted=False,
            reason="no_vin",
            weeks_in_window=0,
            computed_at=now,
        )

    window_start = now - timedelta(days=HABIT_WINDOW_DAYS)
    recent_start = now - timedelta(days=HABIT_RECENT_DAYS)

    # Materialize rows once (iterator is single-pass).
    ref_rows = list(_iter_activity_rows(vin, window_start, recent_start))
    recent_rows = list(_iter_activity_rows(vin, recent_start, now))

    # Full activity (incl. charge) drives “quiet slot” spacing decisions.
    ref_active_map = _build_week_slot_flags(ref_rows, mobile_only=False)
    ref_stats = _slot_stats_from_week_map(ref_active_map)

    # Mobility-only maps for school/holiday regime breaks.
    ref_mobile_map = _build_week_slot_flags(ref_rows, mobile_only=True)
    recent_mobile_map = _build_week_slot_flags(recent_rows, mobile_only=True)
    ref_mobile_stats = _slot_stats_from_week_map(ref_mobile_map)

    weeks = {
        (y, w)
        for (y, w, _d, _h) in list(ref_active_map.keys())
        + list(recent_mobile_map.keys())
    }
    weeks_in_window = len(weeks)

    # Need enough *reference* weeks (before recent) for a stable model
    ref_weeks = {(y, w) for (y, w, _d, _h) in ref_active_map.keys()}
    n_ref_weeks = len(ref_weeks)

    if n_ref_weeks < HABIT_MIN_WEEKS:
        return HabitModel(
            vin=vin,
            trusted=False,
            reason=f"insufficient_ref_weeks={n_ref_weeks}<{HABIT_MIN_WEEKS}",
            weeks_in_window=weeks_in_window,
            computed_at=now,
            slots=ref_stats,
        )

    regime_break, regime_detail = _detect_regime_break(
        ref_mobile_stats, ref_mobile_map, recent_mobile_map
    )
    if regime_break:
        return HabitModel(
            vin=vin,
            trusted=False,
            reason="regime_break",
            weeks_in_window=weeks_in_window,
            computed_at=now,
            slots=ref_stats,
            regime_break=True,
            regime_detail=regime_detail,
        )

    quiet: set[tuple[int, int]] = set()
    moderate: set[tuple[int, int]] = set()
    busy: set[tuple[int, int]] = set()
    for dow in range(1, 8):
        for hour in range(24):
            if _smoothed_busy(ref_mobile_stats, dow, hour):
                busy.add((dow, hour))
                continue
            kind = _smoothed_calm_class(ref_stats, dow, hour)
            if kind == "quiet":
                quiet.add((dow, hour))
            elif kind == "moderate":
                moderate.add((dow, hour))

    # Night band fill: calm from full-activity map; busy from mobility map.
    _fill_night_band_from_weeks(ref_active_map, quiet, moderate, set())
    _fill_night_band_from_weeks(ref_mobile_map, set(), set(), busy)
    # Busy wins over calm if both claimed a slot
    for key in list(busy):
        quiet.discard(key)
        moderate.discard(key)

    if not quiet and not moderate and not busy:
        return HabitModel(
            vin=vin,
            trusted=False,
            reason="no_habit_slots",
            weeks_in_window=weeks_in_window,
            computed_at=now,
            slots=ref_stats,
        )

    return HabitModel(
        vin=vin,
        trusted=True,
        reason="ok",
        weeks_in_window=weeks_in_window,
        computed_at=now,
        slots=ref_stats,
        quiet_hours=quiet,
        moderate_hours=moderate,
        busy_hours=busy,
        regime_break=False,
    )


def get_habit_model(vin: str, *, now: datetime | None = None, force: bool = False) -> HabitModel:
    """Cached compute_habit_model."""
    vin = (vin or "").strip()
    if not vin:
        return compute_habit_model(vin, now=now)
    key = CACHE_KEY_PREFIX + vin
    if not force:
        cached = cache.get(key)
        if isinstance(cached, HabitModel):
            return cached
    model = compute_habit_model(vin, now=now)
    cache.set(key, model, CACHE_TTL_SECONDS)
    return model


def invalidate_habit_cache(vin: str | None = None) -> None:
    if vin:
        cache.delete(CACHE_KEY_PREFIX + vin.strip())
    # no global clear — rare
