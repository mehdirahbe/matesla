"""
Undo Fleet SoC rewrite: restore integer API % and recompute degradation.

Fleet vehicle_data sends whole-percent SoC. A previous capture path rewrote it
from battery_range, which locked battery_degradation to a constant. TeslaFi
rows (charge_number set, already fractional) are left untouched.

  python manage.py RestoreRawSoc --dry-run
  python manage.py RestoreRawSoc
  python manage.py RestoreRawSoc --vin 5YJ3E7EB1KF200150
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from matesla.BatteryDegradation import (
    ComputeBatteryDegradationFromEPARange,
    GetEPARangeFromCache,
)
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.soc_refine import is_whole_percent


# First Fleet-only samples ~ 2026-07-26 10:00 Europe/Brussels
DEFAULT_SINCE = "2026-07-26T08:00:00+00:00"


def _restore_whole_percent(value):
    """Round a rewritten SoC back to the Fleet integer bucket."""
    if value is None:
        return None, False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value, False
    if is_whole_percent(numeric):
        return numeric, False
    return float(round(numeric)), True


class Command(BaseCommand):
    help = (
        "Restore raw Fleet SoC (integer %) and recompute degradation "
        "by rule of three. Does not touch TeslaFi rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            default=DEFAULT_SINCE,
            help=f"Only rows with Date >= this (ISO). Default: {DEFAULT_SINCE}",
        )
        parser.add_argument("--vin", default=None, help="Limit to one VIN")
        parser.add_argument(
            "--hashed-vin",
            default=None,
            dest="hashed_vin",
            help="Limit to one hashedVin",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report changes without writing",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max rows to update (0 = no limit)",
        )

    def handle(self, *args, **options):
        since_raw = options["since"]
        since_datetime = parse_datetime(since_raw)
        if since_datetime is None:
            self.stderr.write(self.style.ERROR(f"Invalid --since: {since_raw}"))
            return
        if timezone.is_naive(since_datetime):
            since_datetime = timezone.make_aware(since_datetime, timezone.utc)

        snapshot_queryset = (
            TeslaCarDataSnapshot.objects.filter(
                Date__gte=since_datetime,
                charge_number__isnull=True,
                battery_range__isnull=False,
            )
            .order_by("vin", "Date")
            .only(
                "id",
                "vin",
                "Date",
                "battery_level",
                "usable_battery_level",
                "battery_range",
                "battery_degradation",
            )
        )
        if options["vin"]:
            snapshot_queryset = snapshot_queryset.filter(vin=options["vin"])
        if options["hashed_vin"]:
            snapshot_queryset = snapshot_queryset.filter(
                hashedVin=options["hashed_vin"]
            )

        dry_run = options["dry_run"]
        update_limit = options["limit"]
        scanned_count = 0
        updated_count = 0
        unchanged_count = 0
        example_rows = []
        pending_batch = []
        epa_by_vin = {}

        def epa_for(vin):
            if vin not in epa_by_vin:
                epa_by_vin[vin] = GetEPARangeFromCache(vin)
            return epa_by_vin[vin]

        def flush_batch(rows):
            if not rows or dry_run:
                return
            TeslaCarDataSnapshot.objects.bulk_update(
                rows,
                ["battery_level", "usable_battery_level", "battery_degradation"],
            )

        for snapshot in snapshot_queryset.iterator(chunk_size=500):
            scanned_count += 1
            if update_limit and updated_count >= update_limit:
                break

            new_battery_level, battery_changed = _restore_whole_percent(
                snapshot.battery_level
            )
            new_usable, usable_changed = _restore_whole_percent(
                snapshot.usable_battery_level
            )
            soc = new_usable if new_usable is not None else new_battery_level
            epa_miles = epa_for(snapshot.vin)
            new_degradation = ComputeBatteryDegradationFromEPARange(
                snapshot.battery_range, soc, epa_miles
            )

            degradation_changed = False
            if new_degradation is not None:
                old = snapshot.battery_degradation
                if old is None or abs(float(old) - float(new_degradation)) > 1e-9:
                    degradation_changed = True

            if not battery_changed and not usable_changed and not degradation_changed:
                unchanged_count += 1
                continue

            if len(example_rows) < 8:
                example_rows.append(
                    (
                        snapshot.vin,
                        snapshot.Date.isoformat(),
                        snapshot.battery_level,
                        new_battery_level,
                        snapshot.battery_range,
                        snapshot.battery_degradation,
                        new_degradation,
                    )
                )

            updated_count += 1
            if dry_run:
                continue

            snapshot.battery_level = new_battery_level
            snapshot.usable_battery_level = new_usable
            if new_degradation is not None:
                snapshot.battery_degradation = new_degradation
            pending_batch.append(snapshot)
            if len(pending_batch) >= 200:
                flush_batch(pending_batch)
                pending_batch = []

        flush_batch(pending_batch)

        self.stdout.write(
            f"since={since_datetime.isoformat()} scanned={scanned_count} "
            f"{'would_update' if dry_run else 'updated'}={updated_count} "
            f"unchanged={unchanged_count}"
        )
        for (
            example_vin,
            example_date,
            old_level,
            new_level,
            battery_range,
            old_deg,
            new_deg,
        ) in example_rows:
            old_deg_txt = "None" if old_deg is None else f"{float(old_deg):.3f}"
            new_deg_txt = "None" if new_deg is None else f"{float(new_deg):.3f}"
            self.stdout.write(
                f"  ex {example_vin} {example_date} bl {old_level} -> "
                f"{new_level} (range={battery_range}) deg {old_deg_txt} -> "
                f"{new_deg_txt}"
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no rows written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
