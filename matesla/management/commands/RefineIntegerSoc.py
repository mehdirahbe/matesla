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
        since = parse_datetime(since_raw)
        if since is None:
            self.stderr.write(self.style.ERROR(f"Invalid --since: {since_raw}"))
            return
        if timezone.is_naive(since):
            since = timezone.make_aware(since, timezone.utc)

        qs = TeslaCarDataSnapshot.objects.filter(
            Date__gte=since,
            battery_level__isnull=False,
            battery_range__isnull=False,
            battery_range__gt=50,
        ).order_by("vin", "Date")
        if options["vin"]:
            qs = qs.filter(vin=options["vin"])
        if options["hashed_vin"]:
            qs = qs.filter(hashedVin=options["hashed_vin"])

        # Whole-percent filter in Python (portable across DBs)
        dry = options["dry_run"]
        limit = options["limit"]
        scanned = 0
        candidates = 0
        updated = 0
        skipped = 0
        examples = []

        # Process per VIN so pack estimate is warm and consistent
        current_vin = None
        batch = []

        def flush_batch(vin, rows):
            nonlocal updated, skipped, examples
            if not vin or not rows:
                return
            invalidate_pack_cache(vin)
            for row in rows:
                new_bl, new_ubl = apply_soc_refinement(
                    row.battery_level,
                    row.usable_battery_level,
                    row.battery_range,
                    vin,
                )
                if new_bl is None:
                    skipped += 1
                    continue
                bl_changed = abs(float(new_bl) - float(row.battery_level)) > 1e-6
                ubl_old = row.usable_battery_level
                ubl_changed = False
                if new_ubl is not None:
                    if ubl_old is None:
                        ubl_changed = True
                    else:
                        ubl_changed = abs(float(new_ubl) - float(ubl_old)) > 1e-6
                if not bl_changed and not ubl_changed:
                    skipped += 1
                    continue
                if len(examples) < 8:
                    examples.append(
                        (
                            vin,
                            row.Date.isoformat(),
                            row.battery_level,
                            new_bl,
                            row.battery_range,
                        )
                    )
                if dry:
                    updated += 1
                    continue
                update_fields = []
                if bl_changed:
                    row.battery_level = new_bl
                    update_fields.append("battery_level")
                if ubl_changed:
                    row.usable_battery_level = new_ubl
                    update_fields.append("usable_battery_level")
                # Recompute degradation with refined usable SoC
                from matesla.BatteryDegradation import (
                    ComputeBatteryDegradationFromEPARange,
                    GetEPARangeFromCache,
                )

                epa = GetEPARangeFromCache(vin)
                if (
                    row.battery_range is not None
                    and row.usable_battery_level is not None
                    and epa
                ):
                    row.battery_degradation = ComputeBatteryDegradationFromEPARange(
                        row.battery_range, row.usable_battery_level, epa
                    )
                    update_fields.append("battery_degradation")
                row.save(update_fields=update_fields)
                updated += 1

        for snap in qs.iterator(chunk_size=500):
            scanned += 1
            if not is_whole_percent(snap.battery_level):
                continue
            candidates += 1
            if limit and updated >= limit and not dry:
                break
            if limit and dry and updated >= limit:
                break
            if snap.vin != current_vin:
                flush_batch(current_vin, batch)
                current_vin = snap.vin
                batch = []
            batch.append(snap)
            if dry and limit and (updated + len(batch)) > limit:
                # flush partial
                pass

        flush_batch(current_vin, batch)

        self.stdout.write(
            f"since={since.isoformat()} scanned={scanned} whole_pct={candidates} "
            f"{'would_update' if dry else 'updated'}={updated} unchanged={skipped}"
        )
        for vin, dt, old, new, br in examples:
            self.stdout.write(
                f"  ex {vin} {dt} bl {old} -> {round(new, 3)} (range={br})"
            )
        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no rows written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
