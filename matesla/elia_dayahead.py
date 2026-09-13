"""
Belgian day-ahead prices from Elia Grid Data (no API token).

Same series as the public page
https://www.elia.be/fr/donnees-de-reseau/transport/prix-de-reference-day-ahead
served as JSON:

  …/auctionresultsqh/{YYYY-MM-DD}   15-min MTU (96 rows when published)
  …/auctionresults/{YYYY-MM-DD}     hourly MTU (24 / 23 / 25 on DST)

The date in the URL is the Brussels civil day. Timestamps in the payload are UTC.
Prefer quarter-hour; fall back to hourly when QH is empty.

Call ``ensure_spot_coverage`` when the user saves a dynamic tariff period.
Do not fetch during DayMap render.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from django.db import transaction

from matesla.models.ChargeCost import DayAheadSpotPrice

logger = logging.getLogger(__name__)

ELIA_QH_URL = (
    "https://griddata.elia.be/eliabecontrols.prod/interface/"
    "Interconnections/daily/auctionresultsqh/{date}"
)
ELIA_HOURLY_URL = (
    "https://griddata.elia.be/eliabecontrols.prod/interface/"
    "Interconnections/daily/auctionresults/{date}"
)
FETCH_TIMEOUT_S = 25
USER_AGENT = "MaTesla/1.0"


def _parse_elia_rows(raw: Any, resolution_minutes: int) -> list[dict]:
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        stamp = item.get("dateTime")
        price = item.get("price")
        if stamp is None or price is None:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_timezone.utc)
        else:
            when = when.astimezone(dt_timezone.utc)
        try:
            eur_mwh = float(price)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "mtu_start": when,
                "resolution_minutes": resolution_minutes,
                "price_eur_mwh": eur_mwh,
            }
        )
    return rows


def fetch_elia_day_ahead(
    civil_day: date,
    *,
    session: requests.Session | None = None,
) -> list[dict]:
    """
    Download one Brussels civil day's auction results.

    Returns parsed dicts (may be empty if Elia has not published yet).
    Raises on HTTP/network errors so the caller can log and skip.
    """
    http = session or requests
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    day_s = civil_day.isoformat()
    qh = http.get(
        ELIA_QH_URL.format(date=day_s),
        headers=headers,
        timeout=FETCH_TIMEOUT_S,
    )
    qh.raise_for_status()
    rows = _parse_elia_rows(qh.json(), 15)
    if rows:
        return rows
    hourly = http.get(
        ELIA_HOURLY_URL.format(date=day_s),
        headers=headers,
        timeout=FETCH_TIMEOUT_S,
    )
    hourly.raise_for_status()
    return _parse_elia_rows(hourly.json(), 60)


def store_spot_rows(rows: list[dict]) -> int:
    """Upsert parsed Elia rows. Returns number of rows written."""
    if not rows:
        return 0
    written = 0
    with transaction.atomic():
        for row in rows:
            _obj, created = DayAheadSpotPrice.objects.update_or_create(
                mtu_start=row["mtu_start"],
                resolution_minutes=row["resolution_minutes"],
                defaults={"price_eur_mwh": row["price_eur_mwh"]},
            )
            written += 1
    return written


def fetch_and_store_day(
    civil_day: date,
    *,
    session: requests.Session | None = None,
) -> int:
    rows = fetch_elia_day_ahead(civil_day, session=session)
    return store_spot_rows(rows)


def fetch_and_store_range(start: date, end: date) -> dict[str, int]:
    """Inclusive civil-day range. Continues after a single-day failure."""
    ok = 0
    failed = 0
    stored = 0
    day = start
    while day <= end:
        try:
            stored += fetch_and_store_day(day)
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("Elia day-ahead fetch failed for %s: %s", day, exc)
        day += timedelta(days=1)
    return {"days_ok": ok, "days_failed": failed, "rows": stored}


_BRUSSELS = ZoneInfo("Europe/Brussels")
# A complete hourly day is 23–25 MTUs; QH is 92–100. Below this → refetch.
_MIN_MTU_FOR_CACHED_DAY = 20
# One HTTP GET per civil day; cap so saving a tariff stays inside gunicorn timeout.
MAX_FETCH_DAYS = 400


def _brussels_day(when: datetime) -> date:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_timezone.utc)
    return when.astimezone(_BRUSSELS).date()


def cached_civil_days(start: date, end: date) -> set[date]:
    """Brussels civil days that already have enough Elia MTUs in cache."""
    if end < start:
        return set()
    window_start = datetime.combine(start, datetime.min.time(), tzinfo=_BRUSSELS)
    window_end = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), tzinfo=_BRUSSELS
    )
    counts: dict[date, int] = {}
    for mtu_start in DayAheadSpotPrice.objects.filter(
        mtu_start__gte=window_start - timedelta(hours=4),
        mtu_start__lt=window_end + timedelta(hours=4),
    ).values_list("mtu_start", flat=True):
        day = _brussels_day(mtu_start)
        if start <= day <= end:
            counts[day] = counts.get(day, 0) + 1
    return {day for day, n in counts.items() if n >= _MIN_MTU_FOR_CACHED_DAY}


def ensure_spot_coverage(
    start: date,
    end: date,
    *,
    session: requests.Session | None = None,
    max_days: int = MAX_FETCH_DAYS,
) -> dict[str, int]:
    """
    Fetch missing Elia days in [start, end] (inclusive), newest first.

    Skips days already cached. Caps at max_days HTTP calls per invocation.
    """
    today = datetime.now(_BRUSSELS).date()
    if end > today + timedelta(days=1):
        end = today + timedelta(days=1)
    if end < start:
        return {
            "days_ok": 0,
            "days_failed": 0,
            "days_skipped": 0,
            "days_truncated": 0,
            "rows": 0,
        }
    already = cached_civil_days(start, end)
    missing = []
    day = end
    while day >= start:
        if day not in already:
            missing.append(day)
        day -= timedelta(days=1)
    truncated = 0
    if len(missing) > max_days:
        truncated = len(missing) - max_days
        missing = missing[:max_days]
    ok = failed = stored = 0
    http = session
    for civil in missing:
        try:
            stored += fetch_and_store_day(civil, session=http)
            ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("Elia day-ahead fetch failed for %s: %s", civil, exc)
    return {
        "days_ok": ok,
        "days_failed": failed,
        "days_skipped": len(already),
        "days_truncated": truncated,
        "rows": stored,
    }


def ensure_spots_for_dynamic_period(valid_from: date, valid_to: date | None) -> dict[str, int]:
    """Used when the user saves a dynamic home tariff."""
    today = datetime.now(_BRUSSELS).date()
    end = valid_to if valid_to is not None else today + timedelta(days=1)
    return ensure_spot_coverage(valid_from, end)
