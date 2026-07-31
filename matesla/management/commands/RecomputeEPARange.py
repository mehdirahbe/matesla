"""
Recompute TeslaCarInfo.EPARange from the EPA catalog (+ optional live range).

  python manage.py RecomputeEPARange --dry-run
  python manage.py RecomputeEPARange
  python manage.py RecomputeEPARange --vin LRW3E7EK6RC076090 --recompute-degradation
"""

from django.core.management.base import BaseCommand

from matesla.BatteryDegradation import ComputeBatteryDegradationFromEPARange
from matesla.VinAnalysis import GetYearFromVin, IsDualMotor
from matesla.epa_catalog import lookup_epa_miles, project_full_charge_miles
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.soc_refine import invalidate_pack_cache


class Command(BaseCommand):
    help = "Recompute EPA range (when new) for all or one car"

    def add_arguments(self, parser):
        parser.add_argument("--vin", default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--recompute-degradation",
            action="store_true",
            help="Also refresh battery_degradation on snapshots for updated VINs",
        )

    def _best_projected_full_charge_miles(self, vin):
        """Highest projected 100% rated miles from recent high-SoC snapshots."""
        recent_samples = (
            TeslaCarDataSnapshot.objects.filter(
                vin=vin, battery_level__gte=50, battery_range__gt=50
            )
            .order_by("-Date")
            .values_list("battery_range", "battery_level")[:40]
        )
        best_projection = None
        for battery_range_miles, battery_level_percent in recent_samples:
            projected = project_full_charge_miles(
                battery_range_miles, battery_level_percent
            )
            if projected is not None and (
                best_projection is None or projected > best_projection
            ):
                best_projection = projected
        return best_projection

    def handle(self, *args, **options):
        car_info_queryset = TeslaCarInfo.objects.all().order_by("vin")
        if options["vin"]:
            car_info_queryset = car_info_queryset.filter(vin=options["vin"])
        dry_run = options["dry_run"]
        recompute_degradation = options["recompute_degradation"]

        for car_info in car_info_queryset:
            previous_epa = car_info.EPARange
            projected_full_miles = self._best_projected_full_charge_miles(
                car_info.vin
            )
            epa_miles, catalog_meta = lookup_epa_miles(
                car_info.vin,
                wheel_type=car_info.wheel_type,
                projected_full_miles=projected_full_miles,
            )
            model_year = car_info.modelYear or GetYearFromVin(car_info.vin)
            is_dual_motor = (
                car_info.isDualMotor
                if car_info.isDualMotor is not None
                else IsDualMotor(car_info.vin)
            )
            self.stdout.write(
                f"{car_info.vin} {car_info.car_type} y={model_year} "
                f"dual={is_dual_motor} wheels={car_info.wheel_type} "
                f"proj={None if projected_full_miles is None else round(projected_full_miles, 1)} "
                f"EPA {previous_epa} -> {epa_miles}"
            )
            if dry_run or epa_miles is None:
                continue

            if previous_epa != int(round(epa_miles)):
                car_info.EPARange = int(round(epa_miles))
                car_info.save(update_fields=["EPARange"])
                invalidate_pack_cache(car_info.vin)

            if recompute_degradation:
                updated_count = 0
                update_batch = []
                snapshots = TeslaCarDataSnapshot.objects.filter(
                    vin=car_info.vin
                ).only(
                    "id",
                    "battery_range",
                    "usable_battery_level",
                    "battery_level",
                    "battery_degradation",
                )
                for snapshot in snapshots.iterator(chunk_size=1000):
                    level = snapshot.usable_battery_level
                    if level is None:
                        level = snapshot.battery_level
                    degradation = ComputeBatteryDegradationFromEPARange(
                        snapshot.battery_range, level, epa_miles
                    )
                    if degradation is None:
                        continue
                    if snapshot.battery_degradation is None or abs(
                        float(snapshot.battery_degradation) - float(degradation)
                    ) > 0.05:
                        snapshot.battery_degradation = degradation
                        update_batch.append(snapshot)
                    if len(update_batch) >= 200:
                        TeslaCarDataSnapshot.objects.bulk_update(
                            update_batch, ["battery_degradation"]
                        )
                        updated_count += len(update_batch)
                        update_batch = []
                if update_batch:
                    TeslaCarDataSnapshot.objects.bulk_update(
                        update_batch, ["battery_degradation"]
                    )
                    updated_count += len(update_batch)
                self.stdout.write(
                    f"  degradation rows updated: {updated_count}"
                )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — nothing saved."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
