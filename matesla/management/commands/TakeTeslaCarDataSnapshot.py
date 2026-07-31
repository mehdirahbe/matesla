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
        # Human lines already printed by capture; echo summary status for manage.py users
        access = stats.get("tesla_access_detail") or ""
        flag = stats.get("tesla_access")
        style = self.style.SUCCESS if stats.get("tesla_access_ok") else self.style.WARNING
        if stats.get("tesla_access_ok") is False:
            style = self.style.ERROR
        self.stdout.write(
            style(
                f"Capture: access={flag} | {access} | "
                f"saved={stats.get('saved', 0)} "
                f"offline={stats.get('skipped_offline', 0)} "
                f"wait={stats.get('skipped_wait', 0)} "
                f"error={stats.get('skipped_error', 0)} "
                f"token_error={stats.get('token_error', 0)} "
                f"fleet_limit={stats.get('fleet_limit', 0)}"
            )
        )
