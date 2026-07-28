"""
Rebuild TeslaFirmwareHistory from TeslaCarDataSnapshot.car_version timeline.

Live capture only stores the *current* version when first seen online, so long
TeslaFi/history series never filled the firmware table. This command rebuilds
from first-seen dates of each distinct car_version string.

  python manage.py RebuildFirmwareHistory
  python manage.py RebuildFirmwareHistory --vin 5YJ3E7EB1KF200150 --dry-run
"""

from django.core.management.base import BaseCommand
from django.db.models import Min
from django.utils import timezone

from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.models.TeslaFirmwareHistory import TeslaFirmwareHistory
from matesla.models.VinHash import HashTheVin


def _as_date(dt):
    if dt is None:
        return None
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.date() if hasattr(dt, "date") else dt


class Command(BaseCommand):
    help = "Rebuild firmware history rows from snapshot car_version changes"

    def add_arguments(self, parser):
        parser.add_argument("--vin", default=None, help="Limit to one VIN")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be written without saving",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        vin_filter = options["vin"]

        vins = (
            TeslaCarDataSnapshot.objects.exclude(vin__isnull=True)
            .exclude(vin="")
            .values_list("vin", flat=True)
            .distinct()
        )
        if vin_filter:
            vins = [vin_filter]

        total_written = 0
        for vin in vins:
            info = TeslaCarInfo.objects.filter(vin=vin).first()
            car_model = (info.car_type if info and info.car_type else None) or "model3"
            hashed = HashTheVin(vin)

            rows = list(
                TeslaCarDataSnapshot.objects.filter(vin=vin)
                .exclude(car_version__isnull=True)
                .exclude(car_version="")
                .values("car_version")
                .annotate(first=Min("Date"))
                .order_by("first")
            )
            if not rows:
                self.stdout.write(f"{vin[-8:]}: no car_version in snapshots — skip")
                continue

            # Collapse consecutive identical versions (should already be unique by values())
            timeline = []
            for r in rows:
                ver = (r["car_version"] or "").strip()
                if not ver:
                    continue
                d = _as_date(r["first"])
                if d is None:
                    continue
                if timeline and timeline[-1][0] == ver:
                    continue
                timeline.append((ver, d))

            self.stdout.write(
                f"{vin[-8:]}: {len(timeline)} version(s) from snapshots "
                f"(was {TeslaFirmwareHistory.objects.filter(vin=vin).count()} row(s))"
            )
            if dry:
                for ver, d in timeline[:5]:
                    self.stdout.write(f"  {d}  {ver}")
                if len(timeline) > 5:
                    self.stdout.write(f"  … +{len(timeline) - 5} more")
                continue

            TeslaFirmwareHistory.objects.filter(vin=vin).delete()
            for i, (ver, d) in enumerate(timeline):
                TeslaFirmwareHistory.objects.create(
                    vin=vin,
                    hashedVin=hashed,
                    Version=ver,
                    Date=d,
                    CarModel=car_model,
                    IsArchive=(i < len(timeline) - 1),
                )
            total_written += len(timeline)

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Wrote {total_written} firmware history row(s).")
            )
