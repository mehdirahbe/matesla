"""
Fetch Belgian day-ahead auction prices from Elia Grid Data (no token).

  python manage.py FetchEliaDayAhead
  python manage.py FetchEliaDayAhead --date 2026-09-01
  python manage.py FetchEliaDayAhead --from 2025-11-01 --to 2025-11-30
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from matesla.elia_dayahead import fetch_and_store_day, fetch_and_store_range

BRUSSELS = ZoneInfo("Europe/Brussels")


class Command(BaseCommand):
    help = (
        "Download Elia Belgian day-ahead prices (15-min or hourly) into the "
        "local cache. No API token."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default="",
            help="Single Brussels civil day YYYY-MM-DD (default: today + tomorrow)",
        )
        parser.add_argument("--from", dest="date_from", type=str, default="")
        parser.add_argument("--to", dest="date_to", type=str, default="")

    def handle(self, *args, **options):
        single = (options.get("date") or "").strip()
        date_from = (options.get("date_from") or "").strip()
        date_to = (options.get("date_to") or "").strip()
        try:
            if single:
                day = date.fromisoformat(single)
                n = fetch_and_store_day(day)
                self.stdout.write(self.style.SUCCESS(f"{day}: {n} MTU rows"))
                return
            if date_from or date_to:
                if not date_from or not date_to:
                    raise CommandError("Use both --from and --to")
                start = date.fromisoformat(date_from)
                end = date.fromisoformat(date_to)
                summary = fetch_and_store_range(start, end)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{start}→{end}: {summary['rows']} rows "
                        f"({summary['days_ok']} days ok, "
                        f"{summary['days_failed']} failed)"
                    )
                )
                return
        except ValueError as exc:
            raise CommandError(f"Invalid date: {exc}") from exc

        from django.utils import timezone

        today = timezone.now().astimezone(BRUSSELS).date()
        summary = fetch_and_store_range(today, today + timedelta(days=1))
        self.stdout.write(
            self.style.SUCCESS(
                f"{today} and next: {summary['rows']} rows "
                f"({summary['days_ok']} ok, {summary['days_failed']} failed)"
            )
        )
