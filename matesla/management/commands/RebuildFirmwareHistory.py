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


def _as_calendar_date(value):
    """Normalize a datetime/date to a naive local calendar date for storage."""
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.date() if hasattr(value, "date") else value


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
        dry_run = options["dry_run"]
        vin_filter = options["vin"]

        distinct_vins = (
            TeslaCarDataSnapshot.objects.exclude(vin__isnull=True)
            .exclude(vin="")
            .values_list("vin", flat=True)
            .distinct()
        )
        if vin_filter:
            distinct_vins = [vin_filter]

        total_written = 0
        for vin in distinct_vins:
            car_info = TeslaCarInfo.objects.filter(vin=vin).first()
            car_model = (
                (car_info.car_type if car_info and car_info.car_type else None)
                or "model3"
            )
            hashed_vin = HashTheVin(vin)

            version_first_seen_rows = list(
                TeslaCarDataSnapshot.objects.filter(vin=vin)
                .exclude(car_version__isnull=True)
                .exclude(car_version="")
                .values("car_version")
                .annotate(first_seen=Min("Date"))
                .order_by("first_seen")
            )
            if not version_first_seen_rows:
                self.stdout.write(
                    f"{vin[-8:]}: no car_version in snapshots — skip"
                )
                continue

            # Collapse consecutive identical versions (should already be unique by values())
            timeline = []
            for version_row in version_first_seen_rows:
                version_string = (version_row["car_version"] or "").strip()
                if not version_string:
                    continue
                first_seen_date = _as_calendar_date(version_row["first_seen"])
                if first_seen_date is None:
                    continue
                if timeline and timeline[-1][0] == version_string:
                    continue
                timeline.append((version_string, first_seen_date))

            self.stdout.write(
                f"{vin[-8:]}: {len(timeline)} version(s) from snapshots "
                f"(was {TeslaFirmwareHistory.objects.filter(vin=vin).count()} row(s))"
            )
            if dry_run:
                for version_string, first_seen_date in timeline[:5]:
                    self.stdout.write(f"  {first_seen_date}  {version_string}")
                if len(timeline) > 5:
                    self.stdout.write(f"  … +{len(timeline) - 5} more")
                continue

            TeslaFirmwareHistory.objects.filter(vin=vin).delete()
            for version_index, (version_string, first_seen_date) in enumerate(
                timeline
            ):
                # IsArchive=True for every version except the newest
                TeslaFirmwareHistory.objects.create(
                    vin=vin,
                    hashedVin=hashed_vin,
                    Version=version_string,
                    Date=first_seen_date,
                    CarModel=car_model,
                    IsArchive=(version_index < len(timeline) - 1),
                )
            total_written += len(timeline)

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Wrote {total_written} firmware history row(s)."
                )
            )
