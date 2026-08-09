"""
Human-readable diagnostics for adaptive Fleet poll spacing.

Why this module exists
----------------------
``capture.poll_interval_minutes`` and ``poll_habits`` decide *when* to call
Tesla next. Operators and owners only saw the daily request-count graph, which
does not explain:

  * whether the habit model is trusted (or why not);
  * which idle spacing applies right now and why;
  * what idle spacing the model would use over the next days/weeks.

This module builds one structured report from the same code paths the capture
loop uses. It is shared by:

  * ``manage.py ShowPollHabits`` (CLI)
  * the personal-stats “Polling details” page

Design rules
------------
  * No second policy: always call capture / poll_habits helpers.
  * Live activity (drive / charge / cabin) always wins over habits.
  * Habit intervals replace idle baseline (not max) — busy nights can go denser.
  * Forecast shows *idle* policy only; live activity shortens intervals in real time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext as _

from matesla.capture import (
    CAPTURE_TZ,
    INTERVAL_NIGHT_DEFAULT_MIN,
    INTERVAL_ONLINE_IDLE_MIN,
    LIVE_ACTIVITY_KINDS,
    activity_kind,
    base_poll_interval_minutes,
    is_night,
    poll_interval_minutes,
)
from matesla.models.VinHash import HashTheVin
from matesla.poll_habits import (
    HABIT_MIN_WEEKS,
    HABIT_RECENT_DAYS,
    HABIT_TZ,
    HABIT_WINDOW_DAYS,
    INTERVAL_HABIT_BUSY_MIN,
    INTERVAL_HABIT_MODERATE_MIN,
    INTERVAL_HABIT_QUIET_MIN,
    NIGHT_HOURS,
    HabitModel,
    get_habit_model,
)

if TYPE_CHECKING:
    from matesla.models.TeslaToken import TeslaVehicle

# Habit class → idle minutes when the class actually replaces the baseline.
HABIT_CLASS_INTERVAL_MINUTES = {
    "busy": INTERVAL_HABIT_BUSY_MIN,
    "moderate": INTERVAL_HABIT_MODERATE_MIN,
    "quiet": INTERVAL_HABIT_QUIET_MIN,
}


def weekday_short_label(isoweekday: int) -> str:
    """Translated short weekday (ISO 1=Mon … 7=Sun)."""
    return {
        1: _("Mon"),
        2: _("Tue"),
        3: _("Wed"),
        4: _("Thu"),
        5: _("Fri"),
        6: _("Sat"),
        7: _("Sun"),
    }.get(int(isoweekday), str(isoweekday))


def weekday_full_label(isoweekday: int) -> str:
    """Translated full weekday name (ISO 1=Mon … 7=Sun)."""
    return {
        1: _("Monday"),
        2: _("Tuesday"),
        3: _("Wednesday"),
        4: _("Thursday"),
        5: _("Friday"),
        6: _("Saturday"),
        7: _("Sunday"),
    }.get(int(isoweekday), str(isoweekday))


def format_local_now_label(local_now: datetime) -> str:
    """Local timestamp with translated weekday (not OS locale strftime %A)."""
    return (
        f"{weekday_full_label(local_now.isoweekday())} "
        f"{local_now.strftime('%Y-%m-%d %H:%M %Z')}"
    )


def label_list_state(state: str) -> str:
    """Human label for Tesla list connectivity state."""
    key = (state or "").strip().lower() or "unknown"
    mapping = {
        "online": _("online"),
        "asleep": _("asleep"),
        "offline": _("offline"),
        "unknown": _("unknown"),
    }
    return mapping.get(key, state or mapping["unknown"])


def label_activity_kind(kind: str) -> str:
    """Human label for capture activity_kind codes."""
    key = (kind or "").strip()
    mapping = {
        "driving": _("driving"),
        "dc_charge": _("DC charging"),
        "ac_charge": _("AC charging"),
        "cabin": _("cabin activity"),
        "dogcamp": _("dog / camp mode"),
        "sentry": _("sentry mode"),
        "online_idle": _("online, idle"),
        "asleep": _("asleep"),
    }
    return mapping.get(key, key or _("unknown"))


def label_habit_class(habit_class: str | None) -> str:
    """Human label for busy / moderate / quiet (or none)."""
    if not habit_class:
        return _("None")
    mapping = {
        "busy": _("busy"),
        "moderate": _("moderate"),
        "quiet": _("quiet"),
    }
    return mapping.get(habit_class, habit_class)


def label_reason_code(reason_code: str) -> str:
    """Short translated label for habit trust machine codes (UI, not raw codes)."""
    mapping = {
        "ok": _("OK"),
        "no_vin": _("No VIN"),
        "insufficient_ref_weeks": _("Insufficient reference weeks"),
        "regime_break": _("Regime break"),
        "no_habit_slots": _("No habit slots"),
        "unknown": _("Unknown"),
    }
    return mapping.get(reason_code, reason_code or mapping["unknown"])


# Back-compat alias used by CLI night summaries (English codes only).
WEEKDAY_SHORT_NAMES = {
    1: "Mon",
    2: "Tue",
    3: "Wed",
    4: "Thu",
    5: "Fri",
    6: "Sat",
    7: "Sun",
}


@dataclass(frozen=True)
class CurrentPollStatus:
    """What the capture loop would do for this vehicle *right now*."""

    list_state: str
    list_state_label: str
    activity_kind: str
    activity_kind_label: str
    is_night: bool
    local_time_label: str
    last_polled_at: datetime | None
    next_poll_due_at: datetime | None
    is_due_now: bool
    baseline_interval_minutes: int
    habit_interval_minutes: int | None
    habit_class: str | None
    habit_class_label: str
    effective_interval_minutes: int
    # "live_activity" | "habit" | "baseline"
    decision_source: str
    decision_summary: str


@dataclass(frozen=True)
class HabitModelStatus:
    """Trust state and slot counts for the per-VIN habit model."""

    trusted: bool
    reason_code: str
    reason_label: str
    reason_summary: str
    regime_break: bool
    regime_detail: str
    regime_summary: str
    # Distinct ISO weeks that had *any* telemetry inside the max lookback.
    weeks_with_samples: int
    # Weeks used to *train* slots (lookback minus the recent regime window).
    reference_weeks: int
    minimum_reference_weeks: int
    # Configured maximum search depth (not “days of data we have”).
    max_lookback_days: int
    # Last N days excluded from training; used only for regime checks.
    recent_check_days: int
    quiet_slot_count: int
    moderate_slot_count: int
    busy_slot_count: int
    busy_interval_minutes: int
    moderate_interval_minutes: int
    quiet_interval_minutes: int
    computed_at: datetime | None


@dataclass(frozen=True)
class IdleScheduleCell:
    """One (weekday, hour) idle policy cell for the habit week grid."""

    isoweekday: int
    hour: int
    habit_class: str | None
    # Minutes used if the car is idle/asleep/sentry in this slot.
    idle_interval_minutes: int
    # True when habit replaces day/night baseline for this slot.
    habit_overrides_baseline: bool


@dataclass(frozen=True)
class IdleForecastSegment:
    """Consecutive hours on one local day with the same idle interval."""

    local_date_label: str
    weekday_name: str
    start_hour: int
    end_hour_exclusive: int
    habit_class: str | None
    idle_interval_minutes: int


@dataclass(frozen=True)
class PollDiagnosticReport:
    """Full diagnostic payload for UI and CLI."""

    vin: str
    vin_tail: str
    display_name: str
    hashed_vin: str | None
    now_utc: datetime
    now_local_label: str
    current: CurrentPollStatus | None
    habits: HabitModelStatus
    week_grid: list[IdleScheduleCell] = field(default_factory=list)
    forecast_days: list[list[IdleForecastSegment]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    vehicle_found: bool = False


def resolve_vehicle_for_hashed_vin(hashed_vin: str):
    """
    Find a ``TeslaVehicle`` whose VIN hashes to ``hashed_vin``.

    Personal-stats URLs never carry the raw VIN. We match HashTheVin(vehicle.vin).
    If no fleet row exists yet, returns None (habits may still be built from
    snapshot history when a VIN is known from another path).
    """
    from matesla.models.TeslaToken import TeslaVehicle

    hashed = (hashed_vin or "").strip()
    if not hashed:
        return None
    for vehicle in TeslaVehicle.objects.exclude(vin="").iterator():
        if HashTheVin(vehicle.vin) == hashed:
            return vehicle
    return None


def resolve_vin_for_hashed_vin(hashed_vin: str) -> str:
    """VIN from TeslaVehicle or latest snapshot for this hashed URL token."""
    vehicle = resolve_vehicle_for_hashed_vin(hashed_vin)
    if vehicle and (vehicle.vin or "").strip():
        return vehicle.vin.strip()
    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot

    vin = (
        TeslaCarDataSnapshot.objects.filter(hashedVin=hashed_vin)
        .exclude(vin="")
        .values_list("vin", flat=True)
        .first()
    )
    return (vin or "").strip()


def _reason_code_from_model(model: HabitModel) -> str:
    reason = (model.reason or "").strip()
    if reason == "ok":
        return "ok"
    if reason == "no_vin":
        return "no_vin"
    if reason == "regime_break" or model.regime_break:
        return "regime_break"
    if reason == "no_habit_slots":
        return "no_habit_slots"
    if reason.startswith("insufficient_ref_weeks"):
        return "insufficient_ref_weeks"
    return reason or "unknown"


def explain_habit_reason(model: HabitModel) -> str:
    """Translated one-paragraph why the habit model is trusted or not."""
    code = _reason_code_from_model(model)
    if code == "ok":
        return _(
            "Habit model is trusted. For idle/asleep hours it can set 5, 15 or "
            "30 minute spacing from historical weekday+hour patterns. Live "
            "drive, charge and cabin activity always keep the short reactive intervals."
        )
    if code == "no_vin":
        return _("No VIN is available, so habit-based spacing cannot run.")
    if code == "insufficient_ref_weeks":
        return _(
            "Not enough reference weeks of telemetry before the last "
            "%(recent)s days (have %(have)s, need at least %(need)s). "
            "Capture uses only the default day/night spacing until history is "
            "long enough."
        ) % {
            "recent": HABIT_RECENT_DAYS,
            "have": model.reference_weeks,
            "need": HABIT_MIN_WEEKS,
        }
    if code == "regime_break":
        return _(
            "Recent mobility does not match the reference weeks (regime break). "
            "Habits are disabled so a school↔holiday or trip change cannot "
            "stretch polls incorrectly. Default day/night spacing is used until "
            "patterns look consistent again."
        )
    if code == "no_habit_slots":
        return _(
            "There are enough reference weeks, but no weekday+hour slot was "
            "classified quiet, moderate or busy. Default spacing only."
        )
    return _("Habit model is not trusted (%(reason)s). Default spacing only.") % {
        "reason": label_reason_code(code)
    }


def explain_regime_detail(regime_detail: str) -> str:
    """Translate machine regime_detail into a short human sentence."""
    detail = (regime_detail or "").strip()
    if not detail:
        return ""
    if detail.startswith("volume_drop"):
        return _(
            "Recent mobile hours per day dropped sharply versus the reference "
            "period (typical of school → holiday). Detail: %(detail)s"
        ) % {"detail": detail}
    if detail.startswith("volume_rise"):
        return _(
            "Recent mobile hours per day rose sharply versus the reference "
            "period (typical of holiday → school or a trip). Detail: %(detail)s"
        ) % {"detail": detail}
    if detail.startswith("active_mismatch"):
        return _(
            "Several recent drives occurred in weekday+hour slots that were "
            "historically almost never mobile. Detail: %(detail)s"
        ) % {"detail": detail}
    return detail


def _decision_source(
    *,
    activity: str,
    habit_interval_minutes: int | None,
    effective_interval_minutes: int,
    baseline_interval_minutes: int,
) -> str:
    if activity in LIVE_ACTIVITY_KINDS:
        return "live_activity"
    if (
        habit_interval_minutes is not None
        and effective_interval_minutes == habit_interval_minutes
    ):
        return "habit"
    if effective_interval_minutes == baseline_interval_minutes:
        return "baseline"
    return "baseline"


def _decision_summary(
    *,
    activity: str,
    decision_source: str,
    habit_class: str | None,
    habit_interval_minutes: int | None,
    baseline_interval_minutes: int,
    effective_interval_minutes: int,
    is_night_now: bool,
) -> str:
    night_label = _("night") if is_night_now else _("day")
    activity_label = label_activity_kind(activity)
    habit_label = label_habit_class(habit_class)
    if decision_source == "live_activity":
        return _(
            "Live activity “%(activity)s” → reactive default spacing "
            "%(minutes)s min (%(day_or_night)s). Habits do not apply while "
            "the car is active."
        ) % {
            "activity": activity_label,
            "minutes": effective_interval_minutes,
            "day_or_night": night_label,
        }
    if decision_source == "habit":
        return _(
            "Idle/asleep policy: habit class “%(habit_class)s” sets %(minutes)s min "
            "for this weekday+hour (default would be %(baseline)s min, %(day_or_night)s)."
        ) % {
            "habit_class": habit_label,
            "minutes": habit_interval_minutes or effective_interval_minutes,
            "baseline": baseline_interval_minutes,
            "day_or_night": night_label,
        }
    if habit_class == "moderate" and is_night_now:
        return _(
            "Idle/asleep policy: slot is historically moderate, but at night "
            "moderate does not densify — keep night default %(minutes)s min."
        ) % {"minutes": effective_interval_minutes}
    return _(
        "Idle/asleep policy: no habit for this weekday+hour → "
        "%(day_or_night)s default %(minutes)s min."
    ) % {
        "day_or_night": night_label,
        "minutes": effective_interval_minutes,
    }


def build_habit_model_status(model: HabitModel) -> HabitModelStatus:
    reason_code = _reason_code_from_model(model)
    return HabitModelStatus(
        trusted=bool(model.trusted and not model.regime_break),
        reason_code=reason_code,
        reason_label=label_reason_code(reason_code),
        reason_summary=explain_habit_reason(model),
        regime_break=bool(model.regime_break),
        regime_detail=model.regime_detail or "",
        regime_summary=explain_regime_detail(model.regime_detail or ""),
        weeks_with_samples=int(model.weeks_in_window or 0),
        reference_weeks=int(model.reference_weeks or 0),
        minimum_reference_weeks=HABIT_MIN_WEEKS,
        max_lookback_days=HABIT_WINDOW_DAYS,
        recent_check_days=HABIT_RECENT_DAYS,
        quiet_slot_count=len(model.quiet_hours),
        moderate_slot_count=len(model.moderate_hours),
        busy_slot_count=len(model.busy_hours),
        busy_interval_minutes=INTERVAL_HABIT_BUSY_MIN,
        moderate_interval_minutes=INTERVAL_HABIT_MODERATE_MIN,
        quiet_interval_minutes=INTERVAL_HABIT_QUIET_MIN,
        computed_at=model.computed_at,
    )


def idle_interval_for_slot(
    model: HabitModel,
    *,
    isoweekday: int,
    hour: int,
) -> tuple[str | None, int, bool]:
    """
    Idle spacing for one civil (weekday, hour) if the car is not live-active.

    Returns (habit_class, idle_interval_minutes, habit_overrides_baseline).
    """
    night = hour in NIGHT_HOURS
    baseline = (
        INTERVAL_NIGHT_DEFAULT_MIN if night else INTERVAL_ONLINE_IDLE_MIN
    )
    if not model.trusted or model.regime_break:
        return None, baseline, False

    # Synthetic local time on a fixed week (only weekday+hour matter).
    # 2024-01-01 is Monday → isoweekday 1.
    from datetime import date, time as time_cls

    monday = date(2024, 1, 1)
    day = monday + timedelta(days=isoweekday - 1)
    local_when = datetime.combine(day, time_cls(hour=hour), tzinfo=HABIT_TZ)
    habit_class = model.habit_class_for_local_time(local_when)
    habit_interval = model.suggested_idle_interval_minutes(local_when)
    if habit_interval is None:
        return habit_class, baseline, False
    return habit_class, habit_interval, True


def build_week_grid(model: HabitModel) -> list[IdleScheduleCell]:
    """7×24 idle policy grid (Monday first)."""
    cells: list[IdleScheduleCell] = []
    for isoweekday in range(1, 8):
        for hour in range(24):
            habit_class, idle_minutes, overrides = idle_interval_for_slot(
                model, isoweekday=isoweekday, hour=hour
            )
            cells.append(
                IdleScheduleCell(
                    isoweekday=isoweekday,
                    hour=hour,
                    habit_class=habit_class,
                    idle_interval_minutes=idle_minutes,
                    habit_overrides_baseline=overrides,
                )
            )
    return cells


def build_idle_forecast(
    model: HabitModel,
    *,
    now_local: datetime,
    days: int = 7,
) -> list[list[IdleForecastSegment]]:
    """
    Compact idle-interval segments for the next ``days`` local calendar days.

    Each day is a list of consecutive hour bands with the same interval.
    Live activity is *not* simulated — this is the idle plan only.
    """
    days = max(1, min(int(days), 14))
    start_date = now_local.astimezone(HABIT_TZ).date()
    result: list[list[IdleForecastSegment]] = []

    for day_offset in range(days):
        day = start_date + timedelta(days=day_offset)
        isoweekday = day.isoweekday()
        weekday_name = weekday_short_label(isoweekday)
        date_label = day.isoformat()

        segments: list[IdleForecastSegment] = []
        segment_start = 0
        current_class: str | None = None
        current_minutes: int | None = None

        def flush(end_hour: int) -> None:
            nonlocal segment_start, current_class, current_minutes
            if current_minutes is None:
                return
            segments.append(
                IdleForecastSegment(
                    local_date_label=date_label,
                    weekday_name=weekday_name,
                    start_hour=segment_start,
                    end_hour_exclusive=end_hour,
                    habit_class=current_class,
                    idle_interval_minutes=current_minutes,
                )
            )

        for hour in range(24):
            habit_class, idle_minutes, _overrides = idle_interval_for_slot(
                model, isoweekday=isoweekday, hour=hour
            )
            if current_minutes is None:
                segment_start = hour
                current_class = habit_class
                current_minutes = idle_minutes
                continue
            if habit_class == current_class and idle_minutes == current_minutes:
                continue
            flush(hour)
            segment_start = hour
            current_class = habit_class
            current_minutes = idle_minutes
        flush(24)
        result.append(segments)

    return result


def build_current_poll_status(
    vehicle: TeslaVehicle,
    model: HabitModel,
    *,
    now: datetime,
) -> CurrentPollStatus:
    """Mirror capture's current decision for this vehicle."""
    kind = activity_kind(vehicle, now=now)
    night = is_night(now)
    baseline = base_poll_interval_minutes(kind, night=night)
    local_now = now.astimezone(CAPTURE_TZ)
    habit_class = None
    habit_interval = None
    if kind not in LIVE_ACTIVITY_KINDS:
        habit_class = model.habit_class_for_local_time(local_now)
        habit_interval = model.suggested_idle_interval_minutes(local_now)
    effective = poll_interval_minutes(vehicle, now=now)
    source = _decision_source(
        activity=kind,
        habit_interval_minutes=habit_interval,
        effective_interval_minutes=effective,
        baseline_interval_minutes=baseline,
    )
    last_polled = vehicle.last_polled_at
    if last_polled is not None and timezone.is_naive(last_polled):
        last_polled = timezone.make_aware(last_polled, timezone.utc)
    next_due = None
    is_due = True
    if last_polled is not None:
        next_due = last_polled + timedelta(minutes=effective)
        is_due = now >= next_due

    list_state = (vehicle.state or "").strip() or "unknown"
    return CurrentPollStatus(
        list_state=list_state,
        list_state_label=label_list_state(list_state),
        activity_kind=kind,
        activity_kind_label=label_activity_kind(kind),
        is_night=night,
        local_time_label=format_local_now_label(local_now),
        last_polled_at=last_polled,
        next_poll_due_at=next_due,
        is_due_now=is_due,
        baseline_interval_minutes=baseline,
        habit_interval_minutes=habit_interval,
        habit_class=habit_class,
        habit_class_label=label_habit_class(habit_class),
        effective_interval_minutes=effective,
        decision_source=source,
        decision_summary=_decision_summary(
            activity=kind,
            decision_source=source,
            habit_class=habit_class,
            habit_interval_minutes=habit_interval,
            baseline_interval_minutes=baseline,
            effective_interval_minutes=effective,
            is_night_now=night,
        ),
    )


def build_poll_diagnostic_report(
    *,
    vehicle: TeslaVehicle | None = None,
    vin: str = "",
    hashed_vin: str | None = None,
    now: datetime | None = None,
    force_recompute: bool = False,
    forecast_days: int = 7,
) -> PollDiagnosticReport:
    """
    Build a full diagnostic report for a vehicle and/or VIN.

    Prefer passing ``vehicle`` (has list state + last_polled_at). If only
    ``vin`` / ``hashed_vin`` is known, habit status and the idle forecast still
    work; current poll status is omitted.
    """
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.utc)

    resolved_vin = (vin or "").strip()
    if vehicle is not None and not resolved_vin:
        resolved_vin = (vehicle.vin or "").strip()
    if not resolved_vin and hashed_vin:
        resolved_vin = resolve_vin_for_hashed_vin(hashed_vin)
        if vehicle is None:
            vehicle = resolve_vehicle_for_hashed_vin(hashed_vin)

    display_name = ""
    if vehicle is not None:
        display_name = (
            (vehicle.display_name or "").strip()
            or (vehicle.vin or "").strip()
            or vehicle.api_id
        )
    elif resolved_vin:
        display_name = resolved_vin

    model = get_habit_model(
        resolved_vin, now=now, force=force_recompute
    )
    habits = build_habit_model_status(model)
    local_now = now.astimezone(HABIT_TZ)
    week_grid = build_week_grid(model)
    forecast = build_idle_forecast(
        model, now_local=local_now, days=forecast_days
    )

    current = None
    if vehicle is not None:
        current = build_current_poll_status(vehicle, model, now=now)

    notes = [
        _(
            "Forecast and week grid describe idle/asleep/sentry spacing only. "
            "If the car drives, DC/AC charges or has cabin activity, capture "
            "switches to the short reactive default until that ends."
        ),
        _(
            "Habits use at most the last %(lookback)s days, require "
            "%(min_weeks)s reference weeks before the last %(recent)s days, "
            "and disable themselves on a regime break."
        )
        % {
            "lookback": HABIT_WINDOW_DAYS,
            "min_weeks": HABIT_MIN_WEEKS,
            "recent": HABIT_RECENT_DAYS,
        },
        _(
            "When habits are trusted, every weekday+hour is busy (%(busy)s min), "
            "quiet (%(quiet)s min), or moderate (%(moderate)s min by day; night "
            "moderate keeps the %(night)s min night default). There is no "
            "idle “default hole” in the grid — only untrusted models fall "
            "back to day %(day)s / night %(night)s everywhere."
        )
        % {
            "busy": INTERVAL_HABIT_BUSY_MIN,
            "moderate": INTERVAL_HABIT_MODERATE_MIN,
            "quiet": INTERVAL_HABIT_QUIET_MIN,
            "night": INTERVAL_NIGHT_DEFAULT_MIN,
            "day": INTERVAL_ONLINE_IDLE_MIN,
        },
    ]

    hashed = hashed_vin
    if not hashed and resolved_vin:
        hashed = HashTheVin(resolved_vin)

    return PollDiagnosticReport(
        vin=resolved_vin,
        vin_tail=resolved_vin[-8:] if len(resolved_vin) >= 8 else resolved_vin,
        display_name=display_name or resolved_vin or "?",
        hashed_vin=hashed,
        now_utc=now,
        now_local_label=format_local_now_label(local_now),
        current=current,
        habits=habits,
        week_grid=week_grid,
        forecast_days=forecast,
        notes=notes,
        vehicle_found=vehicle is not None,
    )


def format_report_for_cli(report: PollDiagnosticReport) -> list[str]:
    """Plain-text lines for ``manage.py ShowPollHabits`` (English-oriented)."""
    lines: list[str] = []
    lines.append(f"=== {report.display_name} ===")
    lines.append(f"  vin=…{report.vin_tail or '?'}")
    lines.append(f"  now local {report.now_local_label}")
    habits = report.habits
    lines.append(
        f"  trusted={habits.trusted}  reason={habits.reason_code}  "
        f"reference_weeks={habits.reference_weeks}/{habits.minimum_reference_weeks}  "
        f"weeks_with_samples={habits.weeks_with_samples}  "
        f"max_lookback={habits.max_lookback_days}d  "
        f"recent_excluded_from_training={habits.recent_check_days}d"
    )
    lines.append(f"  {habits.reason_summary}")
    if habits.regime_break and habits.regime_summary:
        lines.append(f"  REGIME BREAK: {habits.regime_summary}")
    lines.append(
        f"  slots busy={habits.busy_slot_count} (→ {habits.busy_interval_minutes} min)  "
        f"moderate={habits.moderate_slot_count} (→ {habits.moderate_interval_minutes} min)  "
        f"quiet={habits.quiet_slot_count} (→ {habits.quiet_interval_minutes} min)"
    )
    if report.current:
        current = report.current
        lines.append(
            f"  NOW: list={current.list_state} activity={current.activity_kind} "
            f"effective={current.effective_interval_minutes} min "
            f"(source={current.decision_source})"
        )
        lines.append(f"  {current.decision_summary}")
        if current.last_polled_at:
            lines.append(
                f"  last_polled_at={current.last_polled_at.isoformat()}  "
                f"next_due={current.next_poll_due_at.isoformat() if current.next_poll_due_at else '?'}  "
                f"due_now={current.is_due_now}"
            )
    else:
        lines.append("  NOW: no TeslaVehicle row (list state / last poll unknown)")

    # Compact night slot summary (same idea as the old CLI).
    night_parts: list[str] = []
    for cell in report.week_grid:
        if cell.hour not in NIGHT_HOURS or not cell.habit_overrides_baseline:
            continue
        tag = {
            "busy": "B",
            "quiet": "Q",
            "moderate": "M",
        }.get(cell.habit_class or "", "?")
        night_parts.append(
            f"{WEEKDAY_SHORT_NAMES.get(cell.isoweekday, cell.isoweekday)}"
            f"{cell.hour:02d}{tag}"
        )
    if night_parts:
        lines.append(f"  night habit overrides: {', '.join(night_parts[:40])}")
        if len(night_parts) > 40:
            lines.append(f"  … +{len(night_parts) - 40} more")

    if report.forecast_days:
        lines.append("  idle forecast (next days, idle only):")
        for day_segments in report.forecast_days[:7]:
            if not day_segments:
                continue
            first = day_segments[0]
            compact = ", ".join(
                f"{segment.start_hour:02d}–{segment.end_hour_exclusive:02d}h→"
                f"{segment.idle_interval_minutes}m"
                for segment in day_segments
            )
            lines.append(
                f"    {first.weekday_name} {first.local_date_label}: {compact}"
            )
    return lines
