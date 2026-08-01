"""
Diagnose per-vehicle poll habit model (quiet / moderate / busy hours).

Example:
  python manage.py ShowPollHabits
  python manage.py ShowPollHabits --vin LRW3E7EK6RC076090 --force
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from matesla.capture import (
    INTERVAL_NIGHT_DEFAULT_MIN,
    INTERVAL_ONLINE_IDLE_MIN,
    is_night,
)
from matesla.models.TeslaToken import TeslaVehicle
from matesla.poll_habits import (
    HABIT_TZ,
    INTERVAL_HABIT_BUSY_MIN,
    INTERVAL_HABIT_MODERATE_MIN,
    INTERVAL_HABIT_QUIET_MIN,
    NIGHT_HOURS,
    get_habit_model,
    invalidate_habit_cache,
)

_DOW = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


class Command(BaseCommand):
    help = "Show habit-based poll spacing model for each vehicle (or one VIN)."

    def add_arguments(self, parser):
        parser.add_argument("--vin", default="", help="Limit to one VIN")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Bypass cache and recompute",
        )

    def handle(self, *args, **options):
        vin_filter = (options.get("vin") or "").strip()
        force = bool(options.get("force"))
        now = timezone.now()
        local = now.astimezone(HABIT_TZ)

        vehicles = TeslaVehicle.objects.all().order_by("display_name", "vin")
        if vin_filter:
            vehicles = vehicles.filter(vin=vin_filter)

        if not vehicles.exists():
            self.stdout.write(self.style.WARNING("No vehicles found."))
            return

        self.stdout.write(
            f"Now local {local.strftime('%A %Y-%m-%d %H:%M %Z')}  "
            f"(dow={local.isoweekday()} hour={local.hour})"
        )
        self.stdout.write(
            "Legend: busy→%s min (denser, can beat night baseline) · "
            "moderate→%s min · quiet→%s min · "
            "None→baseline (night idle %s / day idle %s). "
            "When a habit is set it *replaces* baseline for idle (not max)."
            % (
                INTERVAL_HABIT_BUSY_MIN,
                INTERVAL_HABIT_MODERATE_MIN,
                INTERVAL_HABIT_QUIET_MIN,
                INTERVAL_NIGHT_DEFAULT_MIN,
                INTERVAL_ONLINE_IDLE_MIN,
            )
        )
        for vehicle in vehicles:
            vin = vehicle.vin or ""
            label = (vehicle.display_name or vin or "?").strip()
            if force:
                invalidate_habit_cache(vin)
            model = get_habit_model(vin, now=now, force=force)
            habit_now = model.suggested_idle_interval_minutes(local)
            night = is_night(now)
            baseline_now = (
                INTERVAL_NIGHT_DEFAULT_MIN if night else INTERVAL_ONLINE_IDLE_MIN
            )
            if habit_now is None:
                effective = baseline_now
                habit_note = "no habit for this hour → baseline"
            else:
                effective = habit_now
                habit_note = f"habit={habit_now} (baseline would be {baseline_now})"

            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"=== {label} ==="))
            self.stdout.write(f"  vin=…{vin[-8:] if len(vin) >= 8 else vin}")
            self.stdout.write(f"  trusted={model.trusted}  reason={model.reason}")
            self.stdout.write(f"  weeks_in_window={model.weeks_in_window}")
            if model.regime_break:
                self.stdout.write(
                    self.style.WARNING(f"  REGIME BREAK: {model.regime_detail}")
                )
            self.stdout.write(
                f"  busy_slots={len(model.busy_hours)} (→ {INTERVAL_HABIT_BUSY_MIN} min)  "
                f"moderate_slots={len(model.moderate_hours)} "
                f"(→ {INTERVAL_HABIT_MODERATE_MIN} min)  "
                f"quiet_slots={len(model.quiet_hours)} (→ {INTERVAL_HABIT_QUIET_MIN} min)"
            )
            self.stdout.write(
                f"  NOW idle spacing: effective={effective} min ({habit_note})"
            )

            def _count(slots, night_only):
                if night_only:
                    return sum(1 for _d, h in slots if h in NIGHT_HOURS)
                return sum(1 for _d, h in slots if h not in NIGHT_HOURS)

            self.stdout.write(
                f"  night 22–05: busy={_count(model.busy_hours, True)} "
                f"mod={_count(model.moderate_hours, True)} "
                f"quiet={_count(model.quiet_hours, True)}  | "
                f"day: busy={_count(model.busy_hours, False)} "
                f"mod={_count(model.moderate_hours, False)} "
                f"quiet={_count(model.quiet_hours, False)}"
            )

            # Compact night slot list
            by_hour = defaultdict(list)
            for dow, hour in sorted(
                model.quiet_hours | model.moderate_hours | model.busy_hours
            ):
                if hour not in NIGHT_HOURS:
                    continue
                if (dow, hour) in model.busy_hours:
                    tag = "B"
                elif (dow, hour) in model.quiet_hours:
                    tag = "Q"
                else:
                    tag = "M"
                by_hour[hour].append(f"{_DOW[dow]}{tag}")
            if by_hour:
                parts = [
                    f"{hour:02d}h=[{','.join(names)}]"
                    for hour, names in sorted(by_hour.items())
                ]
                self.stdout.write(f"  night habit slots: {'; '.join(parts)}")
