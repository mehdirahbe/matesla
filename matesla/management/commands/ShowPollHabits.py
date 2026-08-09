"""
Diagnose per-vehicle poll habit model and current idle spacing.

Shared logic lives in ``matesla.poll_diagnostics`` (same payload as the
personal-stats Polling details page).

Examples:
  python manage.py ShowPollHabits
  python manage.py ShowPollHabits --vin LRW3E7EK6RC076090 --force
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from matesla.capture import INTERVAL_NIGHT_DEFAULT_MIN, INTERVAL_ONLINE_IDLE_MIN
from matesla.models.TeslaToken import TeslaVehicle
from matesla.poll_diagnostics import (
    build_poll_diagnostic_report,
    format_report_for_cli,
)
from matesla.poll_habits import (
    INTERVAL_HABIT_BUSY_MIN,
    INTERVAL_HABIT_MODERATE_MIN,
    INTERVAL_HABIT_QUIET_MIN,
    invalidate_habit_cache,
)


class Command(BaseCommand):
    help = (
        "Show habit-based poll spacing and current decision for each vehicle "
        "(or one VIN)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--vin", default="", help="Limit to one VIN")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Bypass habit cache and recompute",
        )

    def handle(self, *args, **options):
        vin_filter = (options.get("vin") or "").strip()
        force = bool(options.get("force"))
        now = timezone.now()

        vehicles = TeslaVehicle.objects.all().order_by("display_name", "vin")
        if vin_filter:
            vehicles = vehicles.filter(vin=vin_filter)

        if not vehicles.exists():
            # Still allow VIN-only diagnosis when no fleet row exists.
            if vin_filter:
                if force:
                    invalidate_habit_cache(vin_filter)
                report = build_poll_diagnostic_report(
                    vin=vin_filter, now=now, force_recompute=force
                )
                self.stdout.write(
                    "Legend: busy→%s min · moderate→%s min · quiet→%s min · "
                    "None→baseline (night idle %s / day idle %s)."
                    % (
                        INTERVAL_HABIT_BUSY_MIN,
                        INTERVAL_HABIT_MODERATE_MIN,
                        INTERVAL_HABIT_QUIET_MIN,
                        INTERVAL_NIGHT_DEFAULT_MIN,
                        INTERVAL_ONLINE_IDLE_MIN,
                    )
                )
                for line in format_report_for_cli(report):
                    self.stdout.write(line)
                return
            self.stdout.write(self.style.WARNING("No vehicles found."))
            return

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
            vin = (vehicle.vin or "").strip()
            if force and vin:
                invalidate_habit_cache(vin)
            report = build_poll_diagnostic_report(
                vehicle=vehicle,
                vin=vin,
                now=now,
                force_recompute=force,
            )
            self.stdout.write("")
            for line in format_report_for_cli(report):
                if line.startswith("==="):
                    self.stdout.write(self.style.MIGRATE_HEADING(line))
                elif "REGIME BREAK" in line:
                    self.stdout.write(self.style.WARNING(line))
                else:
                    self.stdout.write(line)
