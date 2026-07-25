from django.core.management.base import BaseCommand

from matesla.capture import capture_all_online_vehicles


class Command(BaseCommand):
    help = (
        "TeslaFi-style capture: for every vehicle already online, pull full "
        "vehicle_data and save snapshots for graphs. Never wakes cars. "
        "Safe to run every minute via cron."
    )

    def handle(self, *args, **options):
        stats = capture_all_online_vehicles()
        self.stdout.write(
            self.style.SUCCESS(
                f"Capture done: saved={stats['saved']} "
                f"offline={stats['skipped_offline']} "
                f"error={stats['skipped_error']} "
                f"token_error={stats['token_error']}"
            )
        )
