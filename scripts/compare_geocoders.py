#!/usr/bin/env python3
"""
Compare reverse-geocoders offline (no Django, no matesla site).

Criterion for matesla: prefer car-driveable roads (Tesla GPS), not park
footways / pedestrian paths (e.g. Allée Jacques Brel = highway=footway).

Usage:
  .venv/bin/python scripts/compare_geocoders.py
  .venv/bin/python scripts/compare_geocoders.py --providers nominatim,photon
  LOCATIONIQ_API_KEY=... GEOAPIFY_API_KEY=... \\
    .venv/bin/python scripts/compare_geocoders.py --providers nominatim,locationiq,geoapify

Env keys (optional — provider skipped if missing):
  LOCATIONIQ_API_KEY   (or LOCATIONIQ_KEY)
  GEOAPIFY_API_KEY     (or GEOAPIFY_KEY)
  OPENCAGE_API_KEY     (or OPENCAGE_KEY)

Always free (no key):
  nominatim  — public OSM Nominatim (1 req/s, polite User-Agent)
  photon     — Komoot Photon public (OSM, no key)

Writes a markdown table to stdout and optionally --out FILE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES = SCRIPT_DIR / "geocode_fixtures.json"
USER_AGENT = "matesla-geocode-compare/1.0 (personal offline test; not production)"
ACCEPT_LANG = "fr,en"
TIMEOUT = 15


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


# OSM / geocoder tags that are NOT car-driveable (parks, bike, stairs…).
# matesla tracks Tesla GPS → we want roads a car can use, not park footways.
NON_CAR_WAY_TAGS = frozenset(
    {
        "footway",
        "path",
        "steps",
        "pedestrian",
        "cycleway",
        "bridleway",
        "corridor",
        "elevator",
        "platform",
        "crossing",
    }
)
# Prefer these address keys first (car roads / buildings on roads).
CAR_ROAD_ADDRESS_KEYS = (
    "road",
    "residential",
    "service",
    "industrial",
    "unclassified",
)
# Explicitly avoided when a real road is available (current matesla still uses these).
PEDESTRIAN_ADDRESS_KEYS = (
    "pedestrian",
    "footway",
    "path",
    "cycleway",
)


@dataclass
class ProviderResult:
    provider: str
    display: str
    latency_ms: float | None = None
    error: str | None = None
    raw_summary: str = ""
    # e.g. highway=footway, building, road — for human review
    kind: str = ""
    # False = clearly a non-car way (footpath etc.); None = unknown
    car_ok: bool | None = None


def _env(*names: str) -> str | None:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return None


def _short(text: str, max_len: int = 120) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _car_ok_from_highway(highway: str | None) -> bool | None:
    if not highway:
        return None
    h = highway.lower().strip()
    if h in NON_CAR_WAY_TAGS:
        return False
    # motorway…residential, service, living_street, track (farm) — treat as car-ish
    return True


def _format_osm_style_address(
    addr: dict | None, fallback: str = "", *, car_roads_only: bool = True
) -> tuple[str, str, bool | None]:
    """
    Street-style line. Returns (display, kind, car_ok).

    If car_roads_only: do not fall back to pedestrian/footway/path/cycleway
    (those are fine for hiking apps, wrong for Tesla parked on a street).
    """
    if not addr:
        return _short(fallback), "unknown", None

    road = None
    road_key = None
    for key in CAR_ROAD_ADDRESS_KEYS:
        if addr.get(key):
            road = addr[key]
            road_key = key
            break
    used_ped = False
    if not road and not car_roads_only:
        for key in PEDESTRIAN_ADDRESS_KEYS:
            if addr.get(key):
                road = addr[key]
                road_key = key
                used_ped = True
                break

    place = (
        addr.get("park")
        or addr.get("leisure")
        or addr.get("amenity")
        or addr.get("tourism")
        or addr.get("building")
    )
    house = addr.get("house_number")
    if road and house:
        street = f"{house}, {road}"
    elif road:
        street = road
    elif place:
        street = place
    else:
        street = None
    locality = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("suburb")
    )
    postcode = addr.get("postcode")
    country = addr.get("country")
    bits = [
        b
        for b in (
            street,
            place if place and place != street else None,
            locality,
            postcode,
            country,
        )
        if b
    ]
    display = _short(", ".join(bits)) if bits else _short(fallback)

    if house and road_key == "road":
        kind = "building/road"
        car_ok = True
    elif road_key == "road" or road_key in ("residential", "service", "industrial", "unclassified"):
        kind = f"address:{road_key}"
        car_ok = True
    elif used_ped:
        kind = f"address:{road_key}"
        car_ok = False
    elif place:
        kind = "place"
        car_ok = None
    else:
        kind = "unknown"
        car_ok = None
    return display, kind, car_ok


def reverse_nominatim(lat: float, lon: float, session: requests.Session) -> ProviderResult:
    t0 = time.perf_counter()
    try:
        r = session.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "addressdetails": 1,
                "extratags": 1,
                "zoom": 18,
                "accept-language": ACCEPT_LANG,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        display, kind, car_ok = _format_osm_style_address(
            data.get("address"),
            fallback=data.get("display_name") or "",
            car_roads_only=True,
        )
        # Prefer structured OSM class/type when available
        osm_type = data.get("type") or data.get("addresstype") or ""
        osm_class = data.get("category") or data.get("class") or ""
        if osm_class or osm_type:
            kind = f"{osm_class}/{osm_type}".strip("/") if osm_class else str(osm_type)
        extratags = data.get("extratags") or {}
        highway = extratags.get("highway")
        if highway:
            kind = f"highway={highway}"
            car_ok = _car_ok_from_highway(highway)
        elif data.get("addresstype") == "road" or osm_type in (
            "residential",
            "primary",
            "secondary",
            "tertiary",
            "unclassified",
            "service",
            "living_street",
        ):
            car_ok = True
        elif osm_class == "building" or data.get("addresstype") == "building":
            car_ok = True  # house number on a street → fine for Tesla
        return ProviderResult(
            "nominatim",
            display or "(empty)",
            latency_ms=(time.perf_counter() - t0) * 1000,
            raw_summary=_short(data.get("display_name") or "", 80),
            kind=kind,
            car_ok=car_ok,
        )
    except Exception as exc:
        return ProviderResult(
            "nominatim",
            "",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:120],
        )


def reverse_photon(lat: float, lon: float, session: requests.Session) -> ProviderResult:
    t0 = time.perf_counter()
    try:
        r = session.get(
            "https://photon.komoot.io/reverse",
            params={"lat": lat, "lon": lon, "lang": "fr"},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        feats = data.get("features") or []
        if not feats:
            return ProviderResult(
                "photon",
                "(no result)",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        props = feats[0].get("properties") or {}
        osm_key = (props.get("osm_key") or "").lower()
        osm_value = (props.get("osm_value") or "").lower()
        kind = f"{osm_key}={osm_value}" if osm_key or osm_value else props.get("type") or ""
        car_ok: bool | None = None
        if osm_key == "highway":
            car_ok = _car_ok_from_highway(osm_value)
        elif osm_key == "building":
            car_ok = True

        bits = [
            props.get("housenumber"),
            props.get("street") or props.get("name"),
            props.get("city") or props.get("town") or props.get("village"),
            props.get("postcode"),
            props.get("country"),
        ]
        if props.get("housenumber") and props.get("street"):
            bits[0] = None
            bits[1] = f"{props['housenumber']}, {props['street']}"
        display = _short(", ".join(str(b) for b in bits if b))
        if car_ok is False:
            display = f"{display}  ⚠ non-car ({kind})"
        return ProviderResult(
            "photon",
            display or "(empty)",
            latency_ms=(time.perf_counter() - t0) * 1000,
            raw_summary=_short(props.get("name") or props.get("street") or "", 80),
            kind=kind,
            car_ok=car_ok,
        )
    except Exception as exc:
        return ProviderResult(
            "photon",
            "",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:120],
        )


def reverse_locationiq(
    lat: float, lon: float, session: requests.Session, api_key: str
) -> ProviderResult:
    t0 = time.perf_counter()
    try:
        r = session.get(
            "https://us1.locationiq.com/v1/reverse",
            params={
                "key": api_key,
                "lat": lat,
                "lon": lon,
                "format": "json",
                "addressdetails": 1,
                "accept-language": ACCEPT_LANG,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        display, kind, car_ok = _format_osm_style_address(
            data.get("address"),
            fallback=data.get("display_name") or "",
            car_roads_only=True,
        )
        return ProviderResult(
            "locationiq",
            display or "(empty)",
            latency_ms=(time.perf_counter() - t0) * 1000,
            raw_summary=_short(data.get("display_name") or "", 80),
            kind=kind,
            car_ok=car_ok,
        )
    except Exception as exc:
        return ProviderResult(
            "locationiq",
            "",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:120],
        )


def reverse_geoapify(
    lat: float, lon: float, session: requests.Session, api_key: str
) -> ProviderResult:
    t0 = time.perf_counter()
    try:
        r = session.get(
            "https://api.geoapify.com/v1/geocode/reverse",
            params={
                "lat": lat,
                "lon": lon,
                "apiKey": api_key,
                "lang": "fr",
                "limit": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        feats = data.get("features") or []
        if not feats:
            return ProviderResult(
                "geoapify",
                "(no result)",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        props = feats[0].get("properties") or {}
        # Prefer formatted, else assemble
        display = props.get("formatted") or props.get("address_line1")
        if not display:
            bits = [
                props.get("housenumber"),
                props.get("street"),
                props.get("city") or props.get("town"),
                props.get("postcode"),
                props.get("country"),
            ]
            if props.get("housenumber") and props.get("street"):
                bits[0] = None
                bits[1] = f"{props['housenumber']}, {props['street']}"
            display = ", ".join(str(b) for b in bits if b)
        # Geoapify often exposes result type / street type
        kind = str(props.get("result_type") or props.get("street") or props.get("category") or "")
        car_ok: bool | None = None
        if props.get("street") or props.get("housenumber"):
            car_ok = True
        return ProviderResult(
            "geoapify",
            _short(display or "(empty)"),
            latency_ms=(time.perf_counter() - t0) * 1000,
            raw_summary=_short(props.get("formatted") or "", 80),
            kind=kind[:60],
            car_ok=car_ok,
        )
    except Exception as exc:
        return ProviderResult(
            "geoapify",
            "",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:120],
        )


def reverse_opencage(
    lat: float, lon: float, session: requests.Session, api_key: str
) -> ProviderResult:
    t0 = time.perf_counter()
    try:
        r = session.get(
            "https://api.opencagedata.com/geocode/v1/json",
            params={
                "q": f"{lat}+{lon}",
                "key": api_key,
                "language": "fr",
                "no_annotations": 1,
                "limit": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return ProviderResult(
                "opencage",
                "(no result)",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        first = results[0]
        components = first.get("components") or {}
        # OpenCage uses slightly different keys
        road = components.get("road") or components.get("pedestrian")
        house = components.get("house_number")
        locality = (
            components.get("city")
            or components.get("town")
            or components.get("village")
            or components.get("municipality")
        )
        postcode = components.get("postcode")
        country = components.get("country")
        if road and house:
            street = f"{house}, {road}"
        else:
            street = road
        bits = [b for b in (street, locality, postcode, country) if b]
        display = ", ".join(bits) if bits else first.get("formatted") or ""
        road_type = components.get("road_type") or components.get("_type") or ""
        car_ok: bool | None = None
        if road and (road_type.lower() in NON_CAR_WAY_TAGS if isinstance(road_type, str) else False):
            car_ok = False
        elif road or house:
            car_ok = True
        return ProviderResult(
            "opencage",
            _short(display or "(empty)"),
            latency_ms=(time.perf_counter() - t0) * 1000,
            raw_summary=_short(first.get("formatted") or "", 80),
            kind=str(road_type or first.get("components", {}).get("_category") or "")[:60],
            car_ok=car_ok,
        )
    except Exception as exc:
        return ProviderResult(
            "opencage",
            "",
            latency_ms=(time.perf_counter() - t0) * 1000,
            error=str(exc)[:120],
        )


@dataclass
class ProviderSpec:
    name: str
    min_interval_sec: float
    needs_key: bool
    # callable(lat, lon, session, key|None) -> ProviderResult
    call: Callable[..., ProviderResult] = field(repr=False)
    key_env: tuple[str, ...] = ()


PROVIDERS: dict[str, ProviderSpec] = {
    "nominatim": ProviderSpec(
        name="nominatim",
        min_interval_sec=1.1,
        needs_key=False,
        call=lambda lat, lon, session, key: reverse_nominatim(lat, lon, session),
    ),
    "photon": ProviderSpec(
        name="photon",
        min_interval_sec=0.5,
        needs_key=False,
        call=lambda lat, lon, session, key: reverse_photon(lat, lon, session),
    ),
    "locationiq": ProviderSpec(
        name="locationiq",
        min_interval_sec=0.55,  # free ~2 req/s
        needs_key=True,
        key_env=("LOCATIONIQ_API_KEY", "LOCATIONIQ_KEY"),
        call=lambda lat, lon, session, key: reverse_locationiq(lat, lon, session, key),
    ),
    "geoapify": ProviderSpec(
        name="geoapify",
        min_interval_sec=0.25,  # free ~5 req/s
        needs_key=True,
        key_env=("GEOAPIFY_API_KEY", "GEOAPIFY_KEY"),
        call=lambda lat, lon, session, key: reverse_geoapify(lat, lon, session, key),
    ),
    "opencage": ProviderSpec(
        name="opencage",
        min_interval_sec=1.05,  # free ~1 req/s
        needs_key=True,
        key_env=("OPENCAGE_API_KEY", "OPENCAGE_KEY"),
        call=lambda lat, lon, session, key: reverse_opencage(lat, lon, session, key),
    ),
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def load_points(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    points = data.get("points") if isinstance(data, dict) else data
    if not isinstance(points, list) or not points:
        raise SystemExit(f"No points in {path}")
    return points


def resolve_provider_list(names: list[str]) -> list[tuple[ProviderSpec, str | None]]:
    """Return (spec, api_key_or_None). Skip keyed providers without env key."""
    out: list[tuple[ProviderSpec, str | None]] = []
    for name in names:
        name = name.strip().lower()
        if name not in PROVIDERS:
            print(f"Unknown provider {name!r}, skip. Known: {', '.join(PROVIDERS)}", file=sys.stderr)
            continue
        spec = PROVIDERS[name]
        key = _env(*spec.key_env) if spec.needs_key else None
        if spec.needs_key and not key:
            print(
                f"Skip {name}: set one of {', '.join(spec.key_env)}",
                file=sys.stderr,
            )
            continue
        out.append((spec, key))
    if not out:
        raise SystemExit("No providers available. Try: --providers nominatim,photon")
    return out


def run_compare(
    points: list[dict[str, Any]],
    provider_list: list[tuple[ProviderSpec, str | None]],
) -> list[dict[str, Any]]:
    session = requests.Session()
    last_call: dict[str, float] = {}
    rows: list[dict[str, Any]] = []

    for pt in points:
        lat = float(pt["lat"])
        lon = float(pt["lon"])
        pid = pt.get("id") or f"{lat},{lon}"
        note = pt.get("note") or ""
        expected = pt.get("expected")
        row: dict[str, Any] = {
            "id": pid,
            "lat": lat,
            "lon": lon,
            "note": note,
            "expected": expected,
            "results": {},
        }
        print(f"\n=== {pid} ({lat}, {lon}) ===", file=sys.stderr)
        if note:
            print(f"  note: {note}", file=sys.stderr)

        for spec, key in provider_list:
            # polite rate limit per provider
            prev = last_call.get(spec.name)
            if prev is not None:
                wait = spec.min_interval_sec - (time.monotonic() - prev)
                if wait > 0:
                    time.sleep(wait)
            result = spec.call(lat, lon, session, key)
            last_call[spec.name] = time.monotonic()
            row["results"][spec.name] = {
                "display": result.display,
                "latency_ms": round(result.latency_ms, 1) if result.latency_ms is not None else None,
                "error": result.error,
                "raw_summary": result.raw_summary,
                "kind": result.kind,
                "car_ok": result.car_ok,
            }
            if result.error:
                print(f"  {spec.name}: ERROR {result.error}", file=sys.stderr)
            else:
                ms = f"{result.latency_ms:.0f} ms" if result.latency_ms else "?"
                car = (
                    "car✓"
                    if result.car_ok is True
                    else ("car✗" if result.car_ok is False else "car?")
                )
                kind = f" [{result.kind}]" if result.kind else ""
                print(
                    f"  {spec.name} ({ms}, {car}){kind}: {result.display}",
                    file=sys.stderr,
                )
        rows.append(row)
    return rows


def to_markdown(rows: list[dict[str, Any]], provider_names: list[str]) -> str:
    lines = [
        "# Geocoder reverse comparison (matesla offline)",
        "",
        f"Points: **{len(rows)}** · Providers: **{', '.join(provider_names)}**",
        "",
    ]
    # One section per point (readable labels can be long)
    for row in rows:
        lines.append(f"## `{row['id']}`")
        lines.append("")
        lines.append(f"- **coords**: `{row['lat']}, {row['lon']}`")
        if row.get("note"):
            lines.append(f"- **note**: {row['note']}")
        if row.get("expected"):
            lines.append(f"- **expected**: {row['expected']}")
        lines.append("")
        lines.append("| provider | latency | car? | kind | result |")
        lines.append("|----------|---------|------|------|--------|")
        for name in provider_names:
            res = row["results"].get(name) or {}
            if res.get("error"):
                cell = f"ERROR: {res['error']}"
            else:
                cell = (res.get("display") or "").replace("|", "\\|")
            lat = res.get("latency_ms")
            lat_s = f"{lat:.0f} ms" if lat is not None else "—"
            cok = res.get("car_ok")
            car_s = "yes" if cok is True else ("NO" if cok is False else "?")
            kind = (res.get("kind") or "").replace("|", "\\|")
            lines.append(f"| {name} | {lat_s} | {car_s} | {kind} | {cell} |")
        lines.append("")

    # Compact matrix
    lines.append("## Compact matrix")
    lines.append("")
    header = ["id"] + provider_names
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        cells = [f"`{row['id']}`"]
        for name in provider_names:
            res = row["results"].get(name) or {}
            if res.get("error"):
                cells.append("ERR")
            else:
                cells.append(_short(res.get("display") or "", 60).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare reverse-geocoders offline")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help=f"JSON fixtures (default: {DEFAULT_FIXTURES.name})",
    )
    parser.add_argument(
        "--providers",
        default="nominatim,photon",
        help="Comma list: nominatim,photon,locationiq,geoapify,opencage",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write markdown report (also printed to stdout)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write full JSON results",
    )
    args = parser.parse_args(argv)

    if not args.fixtures.is_file():
        raise SystemExit(f"Fixtures not found: {args.fixtures}")

    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    provider_list = resolve_provider_list(names)
    active_names = [spec.name for spec, _ in provider_list]

    print(f"Fixtures: {args.fixtures}", file=sys.stderr)
    print(f"Providers: {', '.join(active_names)}", file=sys.stderr)

    points = load_points(args.fixtures)
    rows = run_compare(points, provider_list)
    md = to_markdown(rows, active_names)
    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
