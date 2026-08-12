#!/usr/bin/env python3
"""
MaTesla site performance crawl → CSV.

Hits personal pages for a given hashed VIN and period (years → weeks),
times the HTML document plus each sub-request (PNG graphs, LifetimeMap JSON,
day-map charge graphs, etc.), and writes a tidy CSV.

Examples:

  # Corentin, 10 years (520 weeks) — default period options match the UI
  python scripts/perf_crawl.py \\
    --base-url http://127.0.0.1:8001/fr \\
    --hashed-vin f7431bdbb4f75beb6ed6ff42dc331f14e542764b4d96fb004f29b30f \\
    --years 10 \\
    --out /tmp/matesla-perf.csv

  # Also exercise every chart variant (not only the defaults loaded by the page)
  python scripts/perf_crawl.py ... --deep

  # Two sequential passes (2nd often faster: LocMem PNG / map caches)
  python scripts/perf_crawl.py ... --runs 2

  # Optional Django login (status homepage; personal pages work without)
  python scripts/perf_crawl.py ... --user me@example.com --password '…'

Period mapping (UI weeks): 1y→52, 2y→104, 5y→260, 10y→520.
Override with --period-weeks if needed.

Notes:
  - Measures server response time (not browser paint). Sub-requests are sequential
    (worst-case sum ≈ page_total; a browser loads many in parallel).
  - LocMem caches (PNG graphs, drives list, lifetime map) make run 2+ much faster.
    Restart gunicorn for a cold pass, or compare run 1 after a quiet period.
  - Same-origin only: skips Google Maps links, GitHub, CSV "download my data"
    (use --deep for CSV exports and every chart variant).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests

# Weeks offered by #DesiredPeriod (must stay in sync with personalstats.views).
STATS_PERIOD_WEEKS = frozenset({1, 2, 4, 13, 26, 52, 104, 260, 520})

# Full chart catalogue for --deep (all select options).
STATS_DEEP_GRAPH_FIELDS = (
    ("BatteryDegradationGraph", "odometer"),
    ("StatsOnCarGraph", "battery_degradation"),
    ("BatteryDegradationGraph", "range_at_100_odometer"),
    ("StatsOnCarGraph", "range_at_100"),
    ("StatsOnCarGraph", "charger_power"),
    ("StatsOnCarGraph", "charge_limit_soc"),
    ("StatsOnCarGraph", "charge_rate"),
    ("StatsOnCarGraph", "battery_level"),
    ("StatsOnCarGraph", "battery_range"),
    ("StatsOnCarGraph", "efficiency_by_speed"),
    ("StatsOnCarGraph", "efficiency_by_temp"),
    ("StatsOnCarGraph", "outside_temp"),
    ("StatsOnCarGraph", "inside_temp"),
    ("StatsOnCarGraph", "odometer"),
    ("StatsOnCarGraph", "speed"),
    ("StatsOnCarGraph", "power"),
    ("StatsOnCarGraph", "fleet_poll_cost"),
)

DC_CHARTS = ("power_vs_soc", "soc_vs_time")

# HTML attributes that may hold lazy / async app endpoints.
_ATTR_RE = re.compile(
    r"""(?P<attr>
            data-src
            |data-url
            |data-graph-url(?:-[\w-]+)?
            |src
            |href
        )
        \s*=\s*
        (?P<q>["'])
        (?P<val>.*?)
        (?P=q)
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

@dataclass
class TimingRow:
    run: int
    page: str
    kind: str  # html | graph_png | json | static | other | page_total
    url: str
    status: int | str
    time_ms: float
    bytes: int
    content_type: str
    error: str = ""
    parent: str = ""
    note: str = ""


@dataclass
class CrawlConfig:
    base_url: str
    hashed_vin: str
    period_weeks: int
    years: float
    timeout: float
    include_static: bool
    deep: bool
    unit: str
    runs: int
    user: str | None
    password: str | None
    out: Path
    skip_csv_exports: bool


def years_to_weeks(years: float) -> int:
    """Map years to the nearest UI period in weeks."""
    raw = max(1, int(round(years * 52)))
    if raw in STATS_PERIOD_WEEKS:
        return raw
    # Snap to closest allowed value (UI only offers discrete choices).
    return min(STATS_PERIOD_WEEKS, key=lambda w: abs(w - raw))


def normalize_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        raise SystemExit(f"base-url must start with http(s): {url!r}")
    return url


def page_catalog(cfg: CrawlConfig) -> list[tuple[str, str, dict]]:
    """
    Named HTML pages to measure.

    Returns list of (page_name, path_with_query, extra_note).
    Paths are relative to base_url origin (may include /fr prefix via base).
    """
    hv = cfg.hashed_vin
    p = cfg.period_weeks
    # Paths under the i18n base (base_url already ends with /fr or similar).
    return [
        ("home", "/", {}),
        ("stats", f"/personalstats/Stats/{hv}?period={p}", {}),
        ("daymap", f"/personalstats/DayMap/{hv}", {}),
        ("drives", f"/personalstats/Drives/{hv}?period={p}", {}),
        ("dc_charge", f"/personalstats/DCCharge/{hv}?period={p}", {}),
        ("firmware", f"/personalstats/FirmwareHistory/{hv}", {}),
        ("poll_details", f"/personalstats/PollDetails/{hv}", {}),
    ]


def deep_graph_urls(cfg: CrawlConfig) -> list[tuple[str, str]]:
    """Extra graph/JSON endpoints not necessarily default-loaded."""
    hv = cfg.hashed_vin
    p = cfg.period_weeks
    unit = cfg.unit
    out: list[tuple[str, str]] = []
    for endpoint, field in STATS_DEEP_GRAPH_FIELDS:
        path = (
            f"/personalstats/{endpoint}/{hv}/{field}/{p}"
            f"?size=thumb&unit={unit}"
        )
        out.append((f"deep:{endpoint}:{field}", path))
    out.append(
        (
            "deep:LifetimeMapData",
            f"/personalstats/LifetimeMapData/{hv}?period={p}",
        )
    )
    for chart in DC_CHARTS:
        out.append(
            (
                f"deep:DCChargeGraph:{chart}",
                f"/personalstats/DCChargeGraph/{hv}/{chart}/{p}"
                f"?filter=robust&envelope=p10_p90&size=thumb",
            )
        )
    if not cfg.skip_csv_exports:
        out.append(
            ("deep:AllMyDataAsCSV", f"/personalstats/AllMyDataAsCSV/{hv}")
        )
        out.append(
            (
                "deep:FirmwareHistoryCSV",
                f"/personalstats/FirmwareHistoryCSV/{hv}",
            )
        )
    return out


def join_url(base: str, path: str) -> str:
    """Join base (…/fr) with a path that may start with /personalstats or /fr/…."""
    parsed = urlparse(base)
    # base path is e.g. /fr — strip trailing slash handled by normalize
    base_path = parsed.path.rstrip("/")  # /fr
    if path.startswith("http://") or path.startswith("https://"):
        return path
    # Absolute site path: /fr/personalstats/… or /static/…
    if path.startswith("/"):
        # If path already includes locale prefix matching base, use origin only
        if base_path and (path == base_path or path.startswith(base_path + "/")):
            return urlunparse(
                (parsed.scheme, parsed.netloc, path, "", "", "")
            )
        if path.startswith("/static/") or path.startswith("/media/"):
            return urlunparse(
                (parsed.scheme, parsed.netloc, path, "", "", "")
            )
        # /personalstats/… → prepend locale
        full_path = f"{base_path}{path}" if base_path else path
        return urlunparse(
            (parsed.scheme, parsed.netloc, full_path, "", "", "")
        )
    # Relative path
    return urljoin(base + "/", path)


def classify_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/static/" in path or path.startswith("/static"):
        return "static"
    if "/media/" in path:
        return "static"
    if any(
        x in path
        for x in (
            "/statsoncargraph/",
            "/batterydegradationgraph/",
            "/dcchargegraph/",
            "/daychargesessiongraph/",
        )
    ) or path.endswith(".png"):
        return "graph_png"
    if "lifetimemapdata" in path or path.endswith(".json"):
        return "json"
    if "ascsv" in path or path.endswith(".csv") or "firmwarehistorycsv" in path:
        return "csv_export"
    if any(
        x in path
        for x in (
            "/resolveaddress",
            "/matchsupercharger",
            "/statusjson",
        )
    ):
        return "api"
    return "other"


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


def is_page_load_subresource(url: str, base_url: str, *, include_csv: bool) -> bool:
    """
    Keep only same-origin endpoints a browser would load as page content
    (graphs, map JSON, optional static) — not navigation links or Google Maps.
    """
    if _origin(url) != _origin(base_url):
        return False
    path = urlparse(url).path.lower()
    kind = classify_url(url)
    if kind == "static":
        return True  # caller decides include_static
    if kind in ("graph_png", "json", "api"):
        return True
    if kind == "csv_export":
        return include_csv
    # Everything else (chrome nav, unit toggle, external-ish paths) → skip
    return False


def extract_app_urls(html: str, page_url: str, cfg: CrawlConfig) -> list[str]:
    """Pull same-origin app sub-request URLs from HTML attributes."""
    found: list[str] = []
    seen: set[str] = set()
    # CSV downloads are intentional user actions, not page-load assets.
    include_csv = False
    for m in _ATTR_RE.finditer(html):
        raw = unescape(m.group("val").strip())
        if not raw or raw.startswith(("data:", "javascript:", "mailto:", "#", "//")):
            continue
        if raw.startswith("http://") or raw.startswith("https://"):
            full = raw
        elif raw.startswith("/"):
            full = join_url(cfg.base_url, raw)
        else:
            full = urljoin(page_url, raw)

        # LifetimeMap needs period query when only data-url base is present
        if "LifetimeMapData" in full and "period=" not in full:
            sep = "&" if "?" in full else "?"
            full = f"{full}{sep}period={cfg.period_weeks}"

        if not is_page_load_subresource(full, cfg.base_url, include_csv=include_csv):
            continue
        kind = classify_url(full)
        if kind == "static" and not cfg.include_static:
            continue
        if full not in seen:
            seen.add(full)
            found.append(full)
    return found


def timed_get(
    session: requests.Session,
    url: str,
    timeout: float,
) -> tuple[int | str, float, int, str, bytes, str]:
    """Return status, time_ms, nbytes, content_type, body, error."""
    t0 = time.perf_counter()
    try:
        r = session.get(url, timeout=timeout)
        elapsed = (time.perf_counter() - t0) * 1000.0
        ctype = r.headers.get("Content-Type", "")
        body = r.content
        return r.status_code, elapsed, len(body), ctype, body, ""
    except requests.RequestException as exc:
        elapsed = (time.perf_counter() - t0) * 1000.0
        return "ERR", elapsed, 0, "", b"", str(exc)


def login_if_needed(session: requests.Session, cfg: CrawlConfig) -> None:
    if not cfg.user or not cfg.password:
        return
    login_url = join_url(cfg.base_url, "/accounts/login/")
    # GET form for CSRF
    status, _, _, _, body, err = timed_get(session, login_url, cfg.timeout)
    if err:
        print(f"  login GET failed: {err}", file=sys.stderr)
        return
    html = body.decode("utf-8", errors="replace")
    m = re.search(
        r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)',
        html,
    )
    if not m:
        print("  login: no CSRF token found", file=sys.stderr)
        return
    token = m.group(1)
    t0 = time.perf_counter()
    r = session.post(
        login_url,
        data={
            "username": cfg.user,
            "password": cfg.password,
            "csrfmiddlewaretoken": token,
        },
        headers={"Referer": login_url},
        timeout=cfg.timeout,
        allow_redirects=True,
    )
    ms = (time.perf_counter() - t0) * 1000.0
    ok = r.status_code == 200 and "logout" in r.text.lower()
    print(
        f"  login {'OK' if ok else 'maybe-failed'} status={r.status_code} "
        f"{ms:.0f} ms → {r.url}",
        file=sys.stderr,
    )


def crawl_once(session: requests.Session, cfg: CrawlConfig, run: int) -> list[TimingRow]:
    rows: list[TimingRow] = []
    pages = page_catalog(cfg)

    for page_name, rel_path, _meta in pages:
        page_url = join_url(cfg.base_url, rel_path)
        status, ms, nbytes, ctype, body, err = timed_get(session, page_url, cfg.timeout)
        rows.append(
            TimingRow(
                run=run,
                page=page_name,
                kind="html",
                url=page_url,
                status=status,
                time_ms=ms,
                bytes=nbytes,
                content_type=ctype,
                error=err,
                parent="",
            )
        )
        html_ms = ms
        sub_ms = 0.0
        sub_count = 0

        if isinstance(status, int) and status == 200 and body:
            html = body.decode("utf-8", errors="replace")
            sub_urls = extract_app_urls(html, page_url, cfg)

            # Stats: ensure lifetime map period is always measured if deep not used
            if page_name == "stats":
                map_url = join_url(
                    cfg.base_url,
                    f"/personalstats/LifetimeMapData/{cfg.hashed_vin}"
                    f"?period={cfg.period_weeks}",
                )
                if map_url not in sub_urls:
                    sub_urls.append(map_url)

            for sub in sub_urls:
                kind = classify_url(sub)
                st, t_ms, n, ct, _b, e = timed_get(session, sub, cfg.timeout)
                rows.append(
                    TimingRow(
                        run=run,
                        page=page_name,
                        kind=kind,
                        url=sub,
                        status=st,
                        time_ms=t_ms,
                        bytes=n,
                        content_type=ct,
                        error=e,
                        parent=page_name,
                    )
                )
                sub_ms += t_ms
                sub_count += 1

        # Sequential “full page” cost: HTML + all discovered sub-requests
        rows.append(
            TimingRow(
                run=run,
                page=page_name,
                kind="page_total",
                url=page_url,
                status=status,
                time_ms=html_ms + sub_ms,
                bytes=nbytes,
                content_type="",
                error="",
                parent="",
                note=f"html_ms={html_ms:.1f}; sub_ms={sub_ms:.1f}; n_sub={sub_count}",
            )
        )

    if cfg.deep:
        for label, rel in deep_graph_urls(cfg):
            url = join_url(cfg.base_url, rel)
            kind = classify_url(url)
            st, t_ms, n, ct, _b, e = timed_get(session, url, cfg.timeout)
            rows.append(
                TimingRow(
                    run=run,
                    page="deep",
                    kind=kind,
                    url=url,
                    status=st,
                    time_ms=t_ms,
                    bytes=n,
                    content_type=ct,
                    error=e,
                    parent="deep",
                    note=label,
                )
            )

    return rows


def write_csv(path: Path, rows: Iterable[TimingRow], meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp_utc",
        "run",
        "page",
        "kind",
        "status",
        "time_ms",
        "bytes",
        "content_type",
        "url",
        "parent",
        "note",
        "error",
        "base_url",
        "hashed_vin",
        "period_weeks",
        "years",
    ]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "timestamp_utc": ts,
                    "run": r.run,
                    "page": r.page,
                    "kind": r.kind,
                    "status": r.status,
                    "time_ms": f"{r.time_ms:.1f}",
                    "bytes": r.bytes,
                    "content_type": r.content_type,
                    "url": r.url,
                    "parent": r.parent,
                    "note": r.note,
                    "error": r.error,
                    "base_url": meta["base_url"],
                    "hashed_vin": meta["hashed_vin"],
                    "period_weeks": meta["period_weeks"],
                    "years": meta["years"],
                }
            )


def print_summary(rows: list[TimingRow]) -> None:
    totals = [r for r in rows if r.kind == "page_total"]
    print("\n=== Page totals (HTML + sequential sub-requests) ===")
    for r in sorted(totals, key=lambda x: -x.time_ms):
        print(
            f"  run{r.run}  {r.page:14s}  {r.time_ms:8.0f} ms  "
            f"status={r.status}  {r.note}"
        )

    subs = [r for r in rows if r.kind not in ("html", "page_total")]
    if not subs:
        return
    print("\n=== Slowest sub-requests (top 15) ===")
    for r in sorted(subs, key=lambda x: -x.time_ms)[:15]:
        path = urlparse(r.url).path
        q = urlparse(r.url).query
        short = path + ("?" + q if q else "")
        if len(short) > 90:
            short = short[:87] + "…"
        print(
            f"  run{r.run}  {r.time_ms:8.0f} ms  {r.kind:10s}  "
            f"{r.status!s:>4}  {r.bytes:7d} B  {short}"
        )


def parse_args(argv: list[str] | None = None) -> CrawlConfig:
    p = argparse.ArgumentParser(
        description="Crawl MaTesla pages and time HTML + sub-requests → CSV.",
    )
    p.add_argument(
        "--base-url",
        default="http://127.0.0.1:8001/fr",
        help="Locale base URL (default: http://127.0.0.1:8001/fr)",
    )
    p.add_argument(
        "--hashed-vin",
        required=True,
        help="Vehicle hashed VIN (URL segment under personalstats/…)",
    )
    p.add_argument(
        "--years",
        type=float,
        default=10.0,
        help="History window in years (mapped to UI weeks; default 10 → 520)",
    )
    p.add_argument(
        "--period-weeks",
        type=int,
        default=None,
        help="Override period in weeks (must be a UI value if you want exact match)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("perf-results/matesla-perf.csv"),
        help="Output CSV path (default: perf-results/matesla-perf.csv)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout seconds (default 120)",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=1,
        help="How many full passes (2nd pass shows cache warm-up)",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help="Also hit every known graph field + CSV exports",
    )
    p.add_argument(
        "--include-static",
        action="store_true",
        help="Also time /static/ assets referenced by pages",
    )
    p.add_argument(
        "--unit",
        default="km",
        choices=("km", "mi"),
        help="Distance unit query param for graph PNGs (default km)",
    )
    p.add_argument(
        "--user",
        default=None,
        help="Optional Django username for login",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Optional Django password (prefer env MATESLA_PERF_PASSWORD)",
    )
    p.add_argument(
        "--skip-csv-exports",
        action="store_true",
        help="With --deep, skip heavy AllMyDataAsCSV / FirmwareHistoryCSV",
    )
    args = p.parse_args(argv)

    import os

    password = args.password or os.environ.get("MATESLA_PERF_PASSWORD")
    base = normalize_base(args.base_url)
    if args.period_weeks is not None:
        weeks = args.period_weeks
        if weeks not in STATS_PERIOD_WEEKS:
            print(
                f"warning: period-weeks={weeks} is not a UI choice "
                f"{sorted(STATS_PERIOD_WEEKS)}; server may clamp it",
                file=sys.stderr,
            )
    else:
        weeks = years_to_weeks(args.years)

    hv = args.hashed_vin.strip().lower()
    if len(hv) < 20:
        raise SystemExit("hashed-vin looks too short")

    return CrawlConfig(
        base_url=base,
        hashed_vin=hv,
        period_weeks=weeks,
        years=args.years,
        timeout=args.timeout,
        include_static=args.include_static,
        deep=args.deep,
        unit=args.unit,
        runs=max(1, args.runs),
        user=args.user,
        password=password,
        out=args.out,
        skip_csv_exports=args.skip_csv_exports,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    print(
        f"MaTesla perf crawl\n"
        f"  base     = {cfg.base_url}\n"
        f"  vin hash = {cfg.hashed_vin}\n"
        f"  years    = {cfg.years} → period_weeks={cfg.period_weeks}\n"
        f"  runs     = {cfg.runs}  deep={cfg.deep}  static={cfg.include_static}\n"
        f"  out      = {cfg.out}\n",
        file=sys.stderr,
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "matesla-perf-crawl/1.0 (+local benchmark)",
            "Accept-Language": "fr-FR,fr;q=0.9",
        }
    )

    login_if_needed(session, cfg)

    all_rows: list[TimingRow] = []
    wall0 = time.perf_counter()
    for run in range(1, cfg.runs + 1):
        print(f"— run {run}/{cfg.runs} …", file=sys.stderr)
        all_rows.extend(crawl_once(session, cfg, run))
    wall_ms = (time.perf_counter() - wall0) * 1000.0

    write_csv(
        cfg.out,
        all_rows,
        {
            "base_url": cfg.base_url,
            "hashed_vin": cfg.hashed_vin,
            "period_weeks": cfg.period_weeks,
            "years": cfg.years,
        },
    )
    print_summary(all_rows)
    print(
        f"\nWrote {len(all_rows)} rows → {cfg.out}  "
        f"(wall {wall_ms:.0f} ms)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
