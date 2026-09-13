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
MAX_PAGES = 6
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


def ensure_tesla_invoices_for_window(
    hashed_vin: str,
    window_start: datetime,
    window_end: datetime,
    *,
    http_get=None,
    force: bool = False,
) -> int:
    """Fetch Tesla history for this window unless cached recently. Never raises."""
    if not hashed_vin:
        return 0
    now = timezone.now()
    fetch_from = _as_utc(window_start) - timedelta(hours=12)
    fetch_to = _as_utc(window_end) + timedelta(hours=12)
    sync, _ = TeslaChargingHistorySync.objects.get_or_create(hashed_vin=hashed_vin)
    if (
        not force
        and sync.last_ok_at is not None
        and (now - sync.last_ok_at).total_seconds() < CACHE_SECONDS
        and sync.last_from is not None
        and sync.last_to is not None
        and _as_utc(sync.last_from) <= _as_utc(window_start)
        and _as_utc(sync.last_to) >= _as_utc(window_end)
    ):
        return 0
    vin, token = _vin_and_token(hashed_vin)
    if not vin or token is None:
        return 0
    try:
        from matesla.TeslaOAuth import ensure_fresh_access_token

        token = ensure_fresh_access_token(token)
        records = fetch_charging_history(
            token.access_token,
            vin,
            fetch_from,
            fetch_to,
            http_get=http_get,
        )
        stored = store_history_records(hashed_vin, records)
        sync.last_ok_at = now
        sync.last_from = fetch_from
        sync.last_to = fetch_to
        sync.last_error = ""
        sync.save(update_fields=["last_ok_at", "last_from", "last_to", "last_error"])
        logger.info(
            "Tesla charging history: %s session(s) stored for %s…",
            stored,
            hashed_vin[:8],
        )
        return stored
    except Exception as exc:
        sync.last_error = str(exc)[:240]
        sync.save(update_fields=["last_error"])
        logger.warning("Tesla charging history fetch failed: %s", exc)
        return 0


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
