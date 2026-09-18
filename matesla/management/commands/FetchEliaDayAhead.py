"""
Fetch Belgian day-ahead auction prices from Elia Grid Data (no token).

  python manage.py FetchEliaDayAhead
  python manage.py FetchEliaDayAhead --date 2026-09-01
  python manage.py FetchEliaDayAhead --from 2025-11-01 --to 2025-11-30

No-arg form (evening cron): fill missing days from today-lookback through
tomorrow. Elia publishes D+1 around 13:00 Brussels, so a 20:00 run has
tonight and tomorrow. Already-cached days are skipped.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from matesla.elia_dayahead import (
    CRON_LOOKBACK_DAYS,
    ensure_spot_coverage,
    fetch_and_store_day,
    fetch_and_store_range,
)

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
            help="Single Brussels civil day YYYY-MM-DD (default: lookback through tomorrow)",
        )
        parser.add_argument("--from", dest="date_from", type=str, default="")
        parser.add_argument("--to", dest="date_to", type=str, default="")
        parser.add_argument(
            "--lookback",
            type=int,
            default=CRON_LOOKBACK_DAYS,
            help=(
                "With no --date/--from: also fill missing days this many days "
                f"back (default {CRON_LOOKBACK_DAYS})"
            ),
        )

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
        lookback = options.get("lookback")
        if lookback is None:
            lookback = CRON_LOOKBACK_DAYS
        lookback = max(0, int(lookback))
        start = today - timedelta(days=lookback)
        end = today + timedelta(days=1)
        summary = ensure_spot_coverage(start, end)
        self.stdout.write(
            self.style.SUCCESS(
                f"{start}→{end}: {summary['rows']} rows "
                f"({summary['days_ok']} fetched, "
                f"{summary['days_skipped']} cached, "
                f"{summary['days_failed']} failed)"
            )
        )
