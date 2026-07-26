"""
Import a TeslaFi monthly raw CSV into TeslaCarDataSnapshot.

Example:
  python manage.py ImportTeslaFiCSV /home/mehdi/Téléchargements/72026.csv
  python manage.py ImportTeslaFiCSV 72026.csv --vin LRW3E7EK6RC076090 --tz Europe/Brussels
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot


class Command(BaseCommand):
    help = (
        "Import TeslaFi monthly CSV (all columns). Dates interpreted as --tz "
        "(default Europe/Brussels) and stored in UTC. Dedupe: nearest minute "
        "per VIN — merge into existing snapshot or create new."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to TeslaFi monthly CSV")
        parser.add_argument(
            "--vin",
            type=str,
            default="",
            help="Force VIN (default: read from CSV rows)",
        )
        parser.add_argument(
            "--tz",
            type=str,
            default="Europe/Brussels",
            help="Timezone of TeslaFi Date column (default Europe/Brussels)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report counts without writing",
        )

    def handle(self, *args, **options):
        path = Path(options["csv_path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"File not found: {path}")

        try:
            tz = ZoneInfo(options["tz"])
        except Exception as exc:
            raise CommandError(f"Invalid timezone {options['tz']}: {exc}") from exc

        force_vin = (options["vin"] or "").strip()
        dry = options["dry_run"]

        created = updated = skipped = errors = 0
        sample_vin = force_vin

        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "Date" not in reader.fieldnames:
                raise CommandError("CSV missing Date column (not a TeslaFi monthly export?)")

            # Process in batches for speed
            batch_updates = []
            with transaction.atomic():
                for i, row in enumerate(reader, start=1):
                    try:
                        vin = force_vin or (row.get("vin") or "").strip()
                        if not vin:
                            skipped += 1
                            continue
                        sample_vin = sample_vin or vin

                        raw_date = (row.get("Date") or "").strip()
                        if not raw_date:
                            skipped += 1
                            continue
                        # TeslaFi: "2026-07-01 00:00:05"
                        local_dt = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")
                        aware = local_dt.replace(tzinfo=tz)
                        utc_dt = aware.astimezone(ZoneInfo("UTC"))

                        minute = utc_dt.replace(second=0, microsecond=0)
                        minute_end = minute + timedelta(minutes=1)

                        existing = (
                            TeslaCarDataSnapshot.objects.filter(
                                vin=vin,
                                Date__gte=minute,
                                Date__lt=minute_end,
                            )
                            .order_by("Date")
                            .first()
                        )

                        if dry:
                            if existing:
                                updated += 1
                            else:
                                created += 1
                            continue

                        if existing:
                            changed = existing.merge_from_flat_row(row)
                            # Also refresh core telemetry from TF (historical source of truth)
                            snap = TeslaCarDataSnapshot()
                            snap.apply_flat_row(vin, row, utc_dt)
                            for field in TeslaCarDataSnapshot._meta.fields:
                                name = field.name
                                if name in ("id", "vin", "hashedVin", "Date", "DateOnlyDay"):
                                    continue
                                new_val = getattr(snap, name)
                                if new_val is not None:
                                    setattr(existing, name, new_val)
                            existing.Date = utc_dt
                            existing.DateOnlyDay = utc_dt.date()
                            existing._recompute_derived()
                            existing.save()
                            updated += 1
                        else:
                            snap = TeslaCarDataSnapshot()
                            snap.apply_flat_row(vin, row, utc_dt)
                            if snap.charging_state is None:
                                snap.charging_state = "Unknown"
                            snap.save()
                            created += 1

                        if i % 2000 == 0:
                            self.stdout.write(f"  … {i} rows processed")

                    except Exception as exc:
                        errors += 1
                        if errors <= 5:
                            self.stderr.write(f"Row {i}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"{'DRY-RUN ' if dry else ''}Import {path.name}: "
                f"created={created} updated={updated} skipped={skipped} errors={errors} "
                f"vin={sample_vin} tz={options['tz']}→UTC"
            )
        )
