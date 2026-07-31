"""
Backfill fractional SoC for snapshots that only have whole-percent battery_level.

Typical case: Fleet REST capture after TeslaFi history (integer API SoC).
Uses battery_range / pack_rated_miles (median implied full range per VIN).

  python manage.py RefineIntegerSoc --dry-run
  python manage.py RefineIntegerSoc --since 2026-07-26T08:00:00+00:00
  python manage.py RefineIntegerSoc --vin LRW3E7EK6RC076090
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.soc_refine import (
    apply_soc_refinement,
    invalidate_pack_cache,
    is_whole_percent,
)


# First Fleet-only integer samples for robotbleu ~ 2026-07-26 10:00 Europe/Brussels
DEFAULT_SINCE = "2026-07-26T08:00:00+00:00"


class Command(BaseCommand):
    help = "Refine integer battery_level/usable_battery_level from battery_range"

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

        snapshot_queryset = TeslaCarDataSnapshot.objects.filter(
            Date__gte=since_datetime,
            battery_level__isnull=False,
            battery_range__isnull=False,
            battery_range__gt=50,
        ).order_by("vin", "Date")
        if options["vin"]:
            snapshot_queryset = snapshot_queryset.filter(vin=options["vin"])
        if options["hashed_vin"]:
            snapshot_queryset = snapshot_queryset.filter(
                hashedVin=options["hashed_vin"]
            )

        # Whole-percent filter in Python (portable across DBs)
        dry_run = options["dry_run"]
        update_limit = options["limit"]
        scanned_count = 0
        whole_percent_candidates = 0
        updated_count = 0
        unchanged_count = 0
        example_rows = []

        # Process per VIN so the pack estimate stays warm and consistent
        current_vin = None
        pending_batch = []

        def flush_batch(vin, rows):
            nonlocal updated_count, unchanged_count, example_rows
            if not vin or not rows:
                return
            invalidate_pack_cache(vin)
            for snapshot in rows:
                new_battery_level, new_usable_level = apply_soc_refinement(
                    snapshot.battery_level,
                    snapshot.usable_battery_level,
                    snapshot.battery_range,
                    vin,
                )
                if new_battery_level is None:
                    unchanged_count += 1
                    continue
                battery_level_changed = (
                    abs(float(new_battery_level) - float(snapshot.battery_level))
                    > 1e-6
                )
                usable_old = snapshot.usable_battery_level
                usable_changed = False
                if new_usable_level is not None:
                    if usable_old is None:
                        usable_changed = True
                    else:
                        usable_changed = (
                            abs(float(new_usable_level) - float(usable_old)) > 1e-6
                        )
                if not battery_level_changed and not usable_changed:
                    unchanged_count += 1
                    continue
                if len(example_rows) < 8:
                    example_rows.append(
                        (
                            vin,
                            snapshot.Date.isoformat(),
                            snapshot.battery_level,
                            new_battery_level,
                            snapshot.battery_range,
                        )
                    )
                if dry_run:
                    updated_count += 1
                    continue
                update_fields = []
                if battery_level_changed:
                    snapshot.battery_level = new_battery_level
                    update_fields.append("battery_level")
                if usable_changed:
                    snapshot.usable_battery_level = new_usable_level
                    update_fields.append("usable_battery_level")
                # Recompute degradation with refined usable SoC
                from matesla.BatteryDegradation import (
                    ComputeBatteryDegradationFromEPARange,
                    GetEPARangeFromCache,
                )

                epa_miles = GetEPARangeFromCache(vin)
                if (
                    snapshot.battery_range is not None
                    and snapshot.usable_battery_level is not None
                    and epa_miles
                ):
                    snapshot.battery_degradation = (
                        ComputeBatteryDegradationFromEPARange(
                            snapshot.battery_range,
                            snapshot.usable_battery_level,
                            epa_miles,
                        )
                    )
                    update_fields.append("battery_degradation")
                snapshot.save(update_fields=update_fields)
                updated_count += 1

        for snapshot in snapshot_queryset.iterator(chunk_size=500):
            scanned_count += 1
            if not is_whole_percent(snapshot.battery_level):
                continue
            whole_percent_candidates += 1
            if update_limit and updated_count >= update_limit and not dry_run:
                break
            if update_limit and dry_run and updated_count >= update_limit:
                break
            if snapshot.vin != current_vin:
                flush_batch(current_vin, pending_batch)
                current_vin = snapshot.vin
                pending_batch = []
            pending_batch.append(snapshot)

        flush_batch(current_vin, pending_batch)

        self.stdout.write(
            f"since={since_datetime.isoformat()} scanned={scanned_count} "
            f"whole_pct={whole_percent_candidates} "
            f"{'would_update' if dry_run else 'updated'}={updated_count} "
            f"unchanged={unchanged_count}"
        )
        for (
            example_vin,
            example_date,
            old_level,
            new_level,
            battery_range,
        ) in example_rows:
            self.stdout.write(
                f"  ex {example_vin} {example_date} bl {old_level} -> "
                f"{round(new_level, 3)} (range={battery_range})"
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no rows written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
