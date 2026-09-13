"""
Backfill Tesla Supercharger invoices (GET /api/1/dx/charging/history).

Tesla rejects a range longer than 1 year; this command walks ≤1-year chunks
from the oldest known charge snapshot for each vehicle until now.

  python manage.py FetchTeslaChargingHistory
  python manage.py FetchTeslaChargingHistory --hashed-vin <hash>
  python manage.py FetchTeslaChargingHistory --force
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from matesla.models.TeslaToken import TeslaToken, TeslaVehicle
from matesla.models.VinHash import HashTheVin
from matesla.tesla_charging_history import sync_tesla_invoices


def _hashed_vins() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for vehicle in TeslaVehicle.objects.exclude(vin="").select_related("user"):
        if not TeslaToken.objects.filter(user_id=vehicle.user_id).exists():
            continue
        hashed = HashTheVin(vehicle.vin)
        if hashed in seen:
            continue
        seen.add(hashed)
        out.append(hashed)
    return out


class Command(BaseCommand):
    help = (
        "Download Tesla charging-history invoices into the local cache, "
        "chunked to Tesla's 1-year API limit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hashed-vin",
            default="",
            help="One hashed VIN (default: every vehicle with a Tesla token)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Refetch chunks even if they were already covered",
        )

    def handle(self, *args, **options):
        hashed = (options.get("hashed_vin") or "").strip()
        force = bool(options.get("force"))
        targets = [hashed] if hashed else _hashed_vins()
        if hashed and len(hashed) < 16:
            raise CommandError("hashed-vin is too short")
        if not targets:
            self.stdout.write(self.style.WARNING("No vehicles with a Tesla token."))
            return
        for item in targets:
            stored = sync_tesla_invoices(item, force=force)
            self.stdout.write(
                self.style.SUCCESS(f"{item[:8]}… stored={stored} force={force}")
            )
