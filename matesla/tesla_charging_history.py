"""
Tesla Supercharger invoices from GET /api/1/dx/charging/history.

Cached per vehicle. Matched onto derived charge sessions by start time.
Never raises into the Charges page: a Tesla outage keeps the fallback rate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from django.utils import timezone

from matesla.GetProxyToUse import GetProxyToUse
from matesla.models.ChargeCost import TeslaChargingHistorySync, TeslaChargingInvoice
from matesla.models.VinHash import HashTheVin

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")
FETCH_TIMEOUT_S = 12
CACHE_SECONDS = 3600
PAGE_SIZE = 50
# Safety cap only: stop if Tesla keeps returning full pages.
MAX_PAGES = 40
# Tesla: "Date range cannot exceed 1 year". Keep each HTTP call under that
# *after* the ±12 h match padding (a civil year + padding is what broke).
WINDOW_PAD = timedelta(hours=12)
MAX_API_SPAN = timedelta(days=364)
RECENT_REFRESH = timedelta(days=14)
MATCH_START_MAX_S = 30 * 60
EUR_CODES = {"EUR", "€", ""}


def _as_utc(when: datetime) -> datetime:
    if timezone.is_naive(when):
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def _parse_dt(raw) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(when)


def fees_total_eur(fees) -> tuple[float | None, str]:
    """Sum Tesla fee totalDue (invoice total, including idle). EUR only."""
    if not isinstance(fees, list) or not fees:
        return None, ""
    total = 0.0
    currency = ""
    found = False
    for fee in fees:
        if not isinstance(fee, dict):
            continue
        amount = fee.get("totalDue")
        if amount is None:
            amount = fee.get("netDue")
        if amount is None:
            continue
        try:
            total += float(amount)
        except (TypeError, ValueError):
            continue
        found = True
        if not currency:
            currency = str(fee.get("currencyCode") or "").strip().upper()
    if not found:
        return None, currency
    if currency and currency not in EUR_CODES:
        return None, currency
    return total, currency or "EUR"


def charging_kwh(fees) -> float | None:
    if not isinstance(fees, list):
        return None
    total = 0.0
    found = False
    for fee in fees:
        if not isinstance(fee, dict):
            continue
        if str(fee.get("feeType") or "").upper() != "CHARGING":
            continue
        usage = fee.get("usageBase")
        if usage is None:
            continue
        uom = str(fee.get("uom") or "").lower()
        try:
            value = float(usage)
        except (TypeError, ValueError):
            continue
        if uom in ("kwh", "kwhr", ""):
            total += value
            found = True
    return total if found else None


def history_records(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    inner = payload.get("response", payload)
    if isinstance(inner, list):
        return [row for row in inner if isinstance(row, dict)]
    if not isinstance(inner, dict):
        return []
    for key in ("data", "records", "chargingHistory", "history"):
        rows = inner.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def invoice_from_record(record: dict, hashed_vin: str) -> dict | None:
    session_id = record.get("sessionId")
    if session_id is None:
        return None
    start = _parse_dt(record.get("chargeStartDateTime"))
    stop = _parse_dt(record.get("chargeStopDateTime")) or _parse_dt(
        record.get("unlatchDateTime")
    )
    if start is None:
        return None
    if stop is None:
        stop = start + timedelta(minutes=1)
    total, currency = fees_total_eur(record.get("fees"))
    if total is None:
        return None
    vin = (record.get("vin") or "").strip()
    hashed = hashed_vin
    if vin:
        hashed = HashTheVin(vin)
    return {
        "hashed_vin": hashed,
        "tesla_session_id": str(session_id),
        "start": start,
        "end": stop,
        "site_name": (record.get("siteLocationName") or "")[:256],
        "total_eur": total,
        "kwh": charging_kwh(record.get("fees")),
        "currency": currency or "EUR",
    }


def _invoice_id(invoice) -> str:
    return str(
        getattr(invoice, "tesla_session_id", None) or invoice.get("tesla_session_id")
    )


def _invoice_bounds(invoice) -> tuple[datetime | None, datetime | None]:
    inv_start = getattr(invoice, "start", None) or invoice.get("start")
    inv_end = getattr(invoice, "end", None) or invoice.get("end")
    if inv_start is None:
        return None, None
    inv_start = _as_utc(inv_start)
    inv_end = _as_utc(inv_end) if inv_end is not None else inv_start
    return inv_start, inv_end


def match_invoices(session_start, session_end, invoices, used_ids: set[str] | None = None):
    """Tesla invoices that overlap this plug-in (Tesla sometimes splits one stop)."""
    start = _as_utc(session_start)
    end = _as_utc(session_end)
    pad_start = start - timedelta(minutes=20)
    pad_end = end + timedelta(minutes=20)
    used_ids = used_ids or set()
    overlapping = []
    close_start = []
    for invoice in invoices:
        sid = _invoice_id(invoice)
        if not sid or sid in used_ids:
            continue
        inv_start, inv_end = _invoice_bounds(invoice)
        if inv_start is None:
            continue
        if inv_start <= pad_end and inv_end >= pad_start:
            overlapping.append(invoice)
        elif abs((inv_start - start).total_seconds()) <= MATCH_START_MAX_S:
            close_start.append(invoice)
    return overlapping or close_start[:1]


def match_invoice(session_start, session_end, invoices, used_ids: set[str] | None = None):
    matched = match_invoices(session_start, session_end, invoices, used_ids)
    return matched[0] if matched else None


def _vin_and_token(hashed_vin: str):
    from matesla.models.TeslaCarInfo import TeslaCarInfo
    from matesla.models.TeslaToken import TeslaToken, TeslaVehicle

    info = TeslaCarInfo.objects.filter(hashedVin=hashed_vin).first()
    vin = (info.vin if info else "") or ""
    if not vin:
        return "", None
    vehicle = TeslaVehicle.objects.filter(vin=vin).select_related("user").first()
    if not vehicle:
        return vin, None
    token = TeslaToken.objects.filter(user_id=vehicle.user_id).first()
    return vin, token


def fetch_charging_history(
    access_token: str,
    vin: str,
    start: datetime,
    end: datetime,
    *,
    http_get=None,
) -> list[dict]:
    """GET /api/1/dx/charging/history pages. http_get is injected in tests."""
    from matesla.TeslaConnect import api_url

    getter = http_get or requests.get
    headers = {"Authorization": "Bearer " + access_token}
    params = {
        "vin": vin,
        "startTime": _as_utc(start).isoformat(),
        "endTime": _as_utc(end).isoformat(),
        "pageSize": PAGE_SIZE,
    }
    records: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params["pageNo"] = page
        response = getter(
            api_url("/api/1/dx/charging/history"),
            headers=headers,
            params=params,
            proxies=GetProxyToUse(),
            timeout=FETCH_TIMEOUT_S,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"charging/history HTTP {response.status_code}: {response.text[:200]}"
            )
        page_rows = history_records(response.json())
        records.extend(page_rows)
        if len(page_rows) < PAGE_SIZE:
            break
    else:
        logger.warning(
            "Tesla charging history: hit %s-page cap, some sessions may be missing",
            MAX_PAGES,
        )
    return records


def store_history_records(hashed_vin: str, records: list[dict]) -> int:
    stored = 0
    for record in records:
        parsed = invoice_from_record(record, hashed_vin)
        if parsed is None:
            continue
        TeslaChargingInvoice.objects.update_or_create(
            tesla_session_id=parsed["tesla_session_id"],
            defaults={
                key: value
                for key, value in parsed.items()
                if key != "tesla_session_id"
            },
        )
        stored += 1
    return stored


def padded_api_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """±12 h padding, clamped so the HTTP range stays ≤ MAX_API_SPAN."""
    start = _as_utc(start)
    end = _as_utc(end)
    if end < start:
        start, end = end, start
    fetch_from = start - WINDOW_PAD
    fetch_to = end + WINDOW_PAD
    if fetch_to - fetch_from > MAX_API_SPAN:
        fetch_from = start
        fetch_to = min(end, fetch_from + MAX_API_SPAN)
    return fetch_from, fetch_to


def history_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Inner windows whose padded API calls stay ≤ 1 year. Oldest first."""
    start = _as_utc(start)
    end = _as_utc(end)
    if end <= start:
        return []
    inner_max = MAX_API_SPAN - 2 * WINDOW_PAD
    if inner_max <= timedelta(0):
        inner_max = MAX_API_SPAN
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + inner_max, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def _coerce_stamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    parsed = _parse_dt(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str):
        text = value.replace("T", " ", 1)
        try:
            when = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
        return when.replace(tzinfo=UTC)
    return None


def oldest_charge_datetime(hashed_vin: str) -> datetime | None:
    """Earliest charging snapshot for this VIN (source of derived sessions)."""
    if not hashed_vin:
        return None
    from django.db import connection

    from matesla.models.TeslaCarDataSnapshot import TeslaCarDataSnapshot
    from matesla.sqlite_guard import heavy_snapshot_read

    table = TeslaCarDataSnapshot._meta.db_table
    with heavy_snapshot_read():
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT MIN("Date")
                FROM {table}
                WHERE hashedVin = %s
                  AND (charging_state IN ('Charging', 'Starting')
                       OR charger_power > 0.5)
                """,
                [hashed_vin],
            )
            row = cursor.fetchone()
    if not row:
        return None
    return _coerce_stamp(row[0])


def _merge_coverage(sync: TeslaChargingHistorySync, fetch_from: datetime, fetch_to: datetime) -> None:
    fetch_from = _as_utc(fetch_from)
    fetch_to = _as_utc(fetch_to)
    if sync.last_from is None or fetch_from < _as_utc(sync.last_from):
        sync.last_from = fetch_from
    if sync.last_to is None or fetch_to > _as_utc(sync.last_to):
        sync.last_to = fetch_to


def _chunk_is_cached(
    sync: TeslaChargingHistorySync,
    chunk_start: datetime,
    chunk_end: datetime,
    now: datetime,
    force: bool,
) -> bool:
    if force:
        return False
    if sync.last_from is None or sync.last_to is None:
        return False
    if _as_utc(sync.last_from) > _as_utc(chunk_start):
        return False
    if _as_utc(sync.last_to) < _as_utc(chunk_end):
        return False
    if _as_utc(chunk_end) >= now - RECENT_REFRESH:
        if sync.last_ok_at is None:
            return False
        return (now - sync.last_ok_at).total_seconds() < CACHE_SECONDS
    return True


def _fetch_chunks(
    hashed_vin: str,
    start: datetime,
    end: datetime,
    *,
    http_get=None,
    force: bool = False,
    max_chunks: int | None = None,
) -> int:
    """Newest-first. Clears last_error on the first successful chunk. Never raises."""
    if not hashed_vin:
        return 0
    chunks = history_chunks(start, end)
    if not chunks:
        return 0
    now = timezone.now()
    sync, _ = TeslaChargingHistorySync.objects.get_or_create(hashed_vin=hashed_vin)
    pending = [
        pair
        for pair in reversed(chunks)
        if not _chunk_is_cached(sync, pair[0], pair[1], now, force)
    ]
    if not pending:
        return 0
    vin, token = _vin_and_token(hashed_vin)
    if not vin or token is None:
        return 0
    try:
        from matesla.TeslaOAuth import ensure_fresh_access_token

        token = ensure_fresh_access_token(token)
    except Exception as exc:
        sync.last_error = str(exc)[:240]
        sync.save(update_fields=["last_error"])
        logger.warning("Tesla charging history token refresh failed: %s", exc)
        return 0

    stored_total = 0
    fetched = 0
    for chunk_start, chunk_end in pending:
        if max_chunks is not None and fetched >= max_chunks:
            break
        fetch_from, fetch_to = padded_api_bounds(chunk_start, chunk_end)
        try:
            records = fetch_charging_history(
                token.access_token,
                vin,
                fetch_from,
                fetch_to,
                http_get=http_get,
            )
            stored = store_history_records(hashed_vin, records)
            stored_total += stored
            fetched += 1
            now = timezone.now()
            sync.last_ok_at = now
            sync.last_error = ""
            _merge_coverage(sync, fetch_from, fetch_to)
            sync.save(
                update_fields=["last_ok_at", "last_from", "last_to", "last_error"]
            )
            logger.info(
                "Tesla charging history: %s session(s) stored for %s… (%s → %s)",
                stored,
                hashed_vin[:8],
                fetch_from.date(),
                fetch_to.date(),
            )
        except Exception as exc:
            sync.last_error = str(exc)[:240]
            sync.save(update_fields=["last_error"])
            logger.warning("Tesla charging history fetch failed: %s", exc)
            fetched += 1
            continue
    return stored_total


def ensure_tesla_invoices_for_window(
    hashed_vin: str,
    window_start: datetime,
    window_end: datetime,
    *,
    http_get=None,
    force: bool = False,
    backfill: bool = True,
    max_extra_chunks: int | None = 1,
) -> int:
    """
    Fetch Tesla history for this window in ≤1-year chunks. Never raises.

    When backfill is true, also walk older uncovered years (from the oldest
    matesla charge snapshot) newest-first, limited by max_extra_chunks.
    """
    if not hashed_vin:
        return 0
    stored = _fetch_chunks(
        hashed_vin,
        window_start,
        window_end,
        http_get=http_get,
        force=force,
        max_chunks=None,
    )
    if not backfill:
        return stored
    oldest = oldest_charge_datetime(hashed_vin)
    if oldest is None:
        return stored
    sync = TeslaChargingHistorySync.objects.filter(hashed_vin=hashed_vin).first()
    covered_from = (
        _as_utc(sync.last_from)
        if sync is not None and sync.last_from is not None
        else _as_utc(window_start)
    )
    if oldest >= covered_from:
        return stored
    extra = _fetch_chunks(
        hashed_vin,
        oldest,
        covered_from,
        http_get=http_get,
        force=force,
        max_chunks=max_extra_chunks,
    )
    return stored + extra


def sync_tesla_invoices(
    hashed_vin: str,
    *,
    http_get=None,
    force: bool = False,
) -> int:
    """Fetch invoices from the oldest known charge until now (all chunks)."""
    now = timezone.now()
    oldest = oldest_charge_datetime(hashed_vin)
    start = oldest if oldest is not None else now - MAX_API_SPAN
    if start > now:
        start = now - timedelta(days=1)
    return ensure_tesla_invoices_for_window(
        hashed_vin,
        start,
        now,
        http_get=http_get,
        force=force,
        backfill=False,
    )


def apply_tesla_invoices(sessions) -> None:
    """Set tesla_invoice_eur on sessions that match a cached Tesla invoice."""
    if not sessions:
        return
    hashed = sessions[0].hashed_vin
    starts = [_as_utc(s.start) for s in sessions]
    ends = [_as_utc(s.end) for s in sessions]
    invoices = list(
        TeslaChargingInvoice.objects.filter(
            hashed_vin=hashed,
            start__lt=max(ends) + timedelta(hours=1),
            end__gt=min(starts) - timedelta(hours=1),
        ).order_by("start")
    )
    if not invoices:
        return
    used: set[str] = set()
    for session in sessions:
        matched = match_invoices(session.start, session.end, invoices, used)
        if not matched:
            continue
        total = 0.0
        names: list[str] = []
        for invoice in matched:
            used.add(invoice.tesla_session_id)
            total += float(invoice.total_eur)
            name = (invoice.site_name or "").strip()
            if name and name not in names:
                names.append(name)
        session.tesla_invoice_eur = total
        session.tesla_session_id = matched[0].tesla_session_id
        session.tesla_site_name = " · ".join(names)
