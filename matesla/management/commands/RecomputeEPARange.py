"""
Recompute TeslaCarInfo.EPARange from the EPA catalog (+ optional live range).

  python manage.py RecomputeEPARange --dry-run
  python manage.py RecomputeEPARange
  python manage.py RecomputeEPARange --vin LRW3E7EK6RC076090 --recompute-degradation
"""

from django.core.management.base import BaseCommand

from matesla.BatteryDegradation import ComputeBatteryDegradationFromEPARange
from matesla.epa_catalog import lookup_epa_miles, project_full_charge_miles
from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
from matesla.models.TeslaCarInfo import TeslaCarInfo
from matesla.soc_refine import invalidate_pack_cache
from matesla.VinAnalysis import GetYearFromVin, IsDualMotor


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

    def _projected(self, vin):
        qs = (
            TeslaCarDataSnapshot.objects.filter(
                vin=vin, battery_level__gte=50, battery_range__gt=50
            )
            .order_by("-Date")
            .values_list("battery_range", "battery_level")[:40]
        )
        best = None
        for br, bl in qs:
            p = project_full_charge_miles(br, bl)
            if p is not None and (best is None or p > best):
                best = p
        return best

    def handle(self, *args, **options):
        qs = TeslaCarInfo.objects.all().order_by("vin")
        if options["vin"]:
            qs = qs.filter(vin=options["vin"])
        dry = options["dry_run"]
        recompute_deg = options["recompute_degradation"]

        for info in qs:
            old = info.EPARange
            projected = self._projected(info.vin)
            epa, meta = lookup_epa_miles(
                info.vin,
                wheel_type=info.wheel_type,
                projected_full_miles=projected,
            )
            year = info.modelYear or GetYearFromVin(info.vin)
            dual = info.isDualMotor if info.isDualMotor is not None else IsDualMotor(info.vin)
            self.stdout.write(
                f"{info.vin} {info.car_type} y={year} dual={dual} "
                f"wheels={info.wheel_type} proj={None if projected is None else round(projected, 1)} "
                f"EPA {old} -> {epa}"
            )
            if dry or epa is None:
                continue

            if old != int(round(epa)):
                info.EPARange = int(round(epa))
                info.save(update_fields=["EPARange"])
                invalidate_pack_cache(info.vin)

            if recompute_deg:
                updated = 0
                batch = []
                snaps = TeslaCarDataSnapshot.objects.filter(vin=info.vin).only(
                    "id",
                    "battery_range",
                    "usable_battery_level",
                    "battery_level",
                    "battery_degradation",
                )
                for s in snaps.iterator(chunk_size=1000):
                    level = s.usable_battery_level
                    if level is None:
                        level = s.battery_level
                    deg = ComputeBatteryDegradationFromEPARange(
                        s.battery_range, level, epa
                    )
                    if deg is None:
                        continue
                    if s.battery_degradation is None or abs(
                        float(s.battery_degradation) - float(deg)
                    ) > 0.05:
                        s.battery_degradation = deg
                        batch.append(s)
                    if len(batch) >= 200:
                        TeslaCarDataSnapshot.objects.bulk_update(
                            batch, ["battery_degradation"]
                        )
                        updated += len(batch)
                        batch = []
                if batch:
                    TeslaCarDataSnapshot.objects.bulk_update(
                        batch, ["battery_degradation"]
                    )
                    updated += len(batch)
                self.stdout.write(f"  degradation rows updated: {updated}")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing saved."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
