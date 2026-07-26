#!/usr/bin/env python3
"""
Download TeslaFi monthly raw CSV exports month by month.

Preferred auth: login once with username/password, then:
  GET  export2.php     → CSRF (__csrf_magic)
  POST exportMonth.php → CSV

Examples:

  python scripts/download_teslafi_exports.py --from 2025-05 --to 2026-07 \\
    --out ~/Téléchargements/teslafi --skip-existing --exclude 2026-07

  # Cookie from Chrome Network tab (full Cookie header while on export2.php)
  export TESLAFI_COOKIE='PHPSESSID=…; teslafi_rememberMe=…'
  python scripts/download_teslafi_exports.py --cookie-only --from 2025-05 --to 2026-07 \\
    --out ~/Téléchargements/teslafi --skip-existing --exclude 2026-07
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

LOGIN_URL = "https://teslafi.com/userlogin.php"
EXPORT_PAGE = "https://teslafi.com/export2.php"
EXPORT_MONTH = "https://teslafi.com/exportMonth.php"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def parse_ym(s: str) -> tuple[int, int]:
    y, m = s.strip().split("-", 1)
    year, month = int(y), int(m)
    if not (1 <= month <= 12):
        raise ValueError(f"Invalid month in {s}")
    return year, month


def iter_months(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def fname(month: int, year: int) -> str:
    return f"{month}{year}.csv"


def looks_like_csv(data: bytes, ctype: str) -> bool:
    head = data[:300].lstrip(b"\xef\xbb\xbf")
    if head.lower().startswith(b"<!doctype") or head.lower().startswith(b"<html"):
        return False
    if "csv" in ctype.lower() or "octet-stream" in ctype.lower():
        return True
    return (
        head.startswith(b"data_id,")
        or head.startswith(b"Date,")
        or b",vin," in head
        or (b"," in head and b"\n" in data[:2000] and not head.startswith(b"<"))
    )


class TeslaFiClient:
    def __init__(self, debug_dir: Path | None = None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.debug_dir = debug_dir
        self._extra_cookie_header = ""

    def _save_debug(self, name: str, data: bytes) -> None:
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / name
        path.write_bytes(data)
        print(f"    (debug saved {path})", flush=True)

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> tuple[int, bytes, str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        }
        if self._extra_cookie_header:
            # Merge with jar cookies: jar is applied by handler; this adds extras
            headers["Cookie"] = self._merge_cookie_header(
                headers.get("Cookie", ""), self._extra_cookie_header
            )
        if extra_headers:
            headers.update(extra_headers)
        if data is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=180) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                final_url = resp.geturl()
                return resp.status, body, ctype, final_url
        except HTTPError as e:
            body = e.read() if e.fp else b""
            return e.code, body, e.headers.get("Content-Type", "") if e.headers else "", url

    @staticmethod
    def _merge_cookie_header(a: str, b: str) -> str:
        parts = []
        seen = set()
        for chunk in (a, b):
            for part in chunk.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name = part.split("=", 1)[0].strip()
                if name in seen:
                    continue
                seen.add(name)
                parts.append(part)
        return "; ".join(parts)

    def set_cookie_header(self, cookie_header: str) -> None:
        cookie_header = cookie_header.strip()
        if cookie_header and "=" not in cookie_header:
            cookie_header = f"PHPSESSID={cookie_header}"
        self._extra_cookie_header = cookie_header
        # Also seed the jar so subsequent Set-Cookie merges cleanly
        if not cookie_header:
            return
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            self.jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain="teslafi.com",
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None},
                    rfc2109=False,
                )
            )

    @staticmethod
    def _extract_input(html: str, field: str) -> str | None:
        m = re.search(
            rf'name=["\']{re.escape(field)}["\'][^>]*value=["\']([^"\']*)["\']',
            html,
            re.I | re.S,
        )
        if m:
            return m.group(1)
        m = re.search(
            rf'value=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(field)}["\']',
            html,
            re.I | re.S,
        )
        return m.group(1) if m else None

    @staticmethod
    def _is_2fa_page(html: str) -> bool:
        """TeslaFi TOTP step: code field present, username/password gone."""
        low = html.lower()
        has_code = (
            'name="code"' in low
            or 'id="code"' in low
            or "form-row-code" in low
            or "authentication code" in low
            or "two-factor" in low
            or "2fa" in low
        )
        has_password = 'name="password"' in low and 'type="password"' in low
        has_username = 'name="username"' in low
        return has_code and not (has_password and has_username)

    @staticmethod
    def _is_login_page(html: str) -> bool:
        low = html.lower()
        if TeslaFiClient._is_2fa_page(html):
            return False
        if 'type="password"' in low and 'name="username"' in low:
            return True
        if 'id="login_form"' in low and 'name="password"' in low:
            return True
        if "sign in" in low and "userlogin" in low and 'name="token"' in low and 'name="password"' in low:
            return True
        return False

    @staticmethod
    def _login_error_message(html: str) -> str | None:
        # e.g. class="…error…">Incorrect username or password.  4 more attempts…
        m = re.search(
            r'class="[^"]*error[^"]*"[^>]*>\s*([^<]+?)\s*<',
            html,
            re.I | re.S,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(
            r"(Incorrect username or password\.[^<]*)",
            html,
            re.I,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.search(
            r"(Invalid (?:code|token|authentication)[^<]*)",
            html,
            re.I,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return None

    def cookie_summary(self) -> str:
        names = sorted({c.name for c in self.jar})
        return ", ".join(names) if names else "(none)"

    def _post_login_form(self, fields: dict) -> str:
        payload = urlencode(fields).encode("utf-8")
        status, page, _, _ = self._request(
            "POST",
            LOGIN_URL,
            data=payload,
            extra_headers={
                "Referer": LOGIN_URL,
                "Origin": "https://teslafi.com",
            },
        )
        if status not in (200, 302):
            self._save_debug("login_post_err.html", page)
            raise RuntimeError(f"POST login → HTTP {status}")
        return page.decode("utf-8", errors="replace")

    def login(self, username: str, password: str, totp_code: str | None = None) -> None:
        """Password login; if TeslaFi asks for 2FA, prompt (or use totp_code)."""
        status, page, _, _ = self._request("GET", LOGIN_URL)
        if status != 200:
            raise RuntimeError(f"GET login → HTTP {status}")
        html = page.decode("utf-8", errors="replace")
        token = self._extract_input(html, "token")
        if not token:
            self._save_debug("login_get.html", page)
            raise RuntimeError("Login form token not found")

        html = self._post_login_form(
            {
                "username": username,
                "password": password,
                "remember": "1",
                "submit": "Login",
                "token": token,
            }
        )
        self._save_debug("login_post.html", html.encode("utf-8", errors="replace"))

        err = self._login_error_message(html)
        if err and not self._is_2fa_page(html):
            raise RuntimeError(f"Login rejected by TeslaFi: {err}")

        if self._is_2fa_page(html):
            print("  2FA required (TeslaFi authentication code)")
            token2 = self._extract_input(html, "token")
            if not token2:
                self._save_debug("login_2fa.html", html.encode("utf-8", errors="replace"))
                raise RuntimeError("2FA page without token field")
            code = (totp_code or "").strip()
            if not code:
                code = input("TeslaFi 2FA code (app/email): ").strip()
            if not code:
                raise RuntimeError("2FA code required")
            # Try common submit labels TeslaFi may use
            html = self._post_login_form(
                {
                    "code": code,
                    "token": token2,
                    "remember": "1",
                    "submit": "Login",
                }
            )
            self._save_debug("login_2fa_post.html", html.encode("utf-8", errors="replace"))
            err = self._login_error_message(html)
            if err:
                raise RuntimeError(f"2FA rejected: {err}")
            if self._is_2fa_page(html) or self._is_login_page(html):
                raise RuntimeError(
                    "Still on 2FA/login after code — wrong code or form changed. "
                    "Prefer: login in Chrome, then --cookie-only."
                )

        if self._is_login_page(html):
            raise RuntimeError(
                "Login failed (still on Sign in page). "
                "Check username (often the email) and password. "
                f"Cookies now: {self.cookie_summary()}"
            )

        # Strong check: export page must be authenticated
        self.assert_authenticated()
        print(f"  logged in OK (cookies: {self.cookie_summary()})")

    def assert_authenticated(self) -> str:
        """GET export2.php and return CSRF; raise if not logged in."""
        status, page, _, final = self._request("GET", EXPORT_PAGE)
        if status != 200:
            raise RuntimeError(f"GET export2.php → HTTP {status}")
        html = page.decode("utf-8", errors="replace")
        if self._is_login_page(html):
            self._save_debug("export2_get.html", page)
            err = self._login_error_message(html)
            extra = f" TeslaFi says: {err}" if err else ""
            raise RuntimeError(
                "export2.php is the login page — session not authenticated."
                + extra
                + f" Cookies: {self.cookie_summary()}"
            )
        csrf = self._extract_input(html, "__csrf_magic")
        if not csrf:
            self._save_debug("export2_get.html", page)
            # Some layouts may use a different token name — show a hint
            raise RuntimeError(
                "Authenticated page but __csrf_magic missing "
                f"(final_url={final}, cookies={self.cookie_summary()})"
            )
        return csrf

    def download_month(self, year: int, month: int) -> bytes:
        csrf = self.assert_authenticated()
        payload = urlencode(
            {
                "__csrf_magic": csrf,
                "Month": str(month),
                "Year": str(year),
            }
        ).encode("utf-8")
        status, data, ctype, _ = self._request(
            "POST",
            EXPORT_MONTH,
            data=payload,
            extra_headers={
                "Referer": EXPORT_PAGE,
                "Origin": "https://teslafi.com",
            },
        )
        if status != 200:
            self._save_debug(f"export_{year}_{month:02d}.bin", data)
            raise RuntimeError(f"POST exportMonth.php → HTTP {status}")
        if not looks_like_csv(data, ctype):
            self._save_debug(f"export_{year}_{month:02d}.bin", data)
            snippet = data[:240].decode("utf-8", errors="replace").replace("\n", " ")
            raise RuntimeError(f"Not CSV (ctype={ctype!r}). Snippet: {snippet!r}")
        return data


def csv_data_rows(data: bytes) -> int:
    """Non-empty lines after header (rough)."""
    text = data.lstrip(b"\xef\xbb\xbf")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return max(0, len(lines) - 1)


def main() -> int:
    today = date.today()
    ap = argparse.ArgumentParser(
        description="Download TeslaFi monthly CSV exports (supports 2FA + long ranges)"
    )
    ap.add_argument("--user", default=os.environ.get("TESLAFI_USER", ""))
    ap.add_argument("--password", default=os.environ.get("TESLAFI_PASSWORD", ""))
    ap.add_argument(
        "--totp",
        default=os.environ.get("TESLAFI_TOTP", ""),
        help="2FA code if already known (else interactive prompt)",
    )
    ap.add_argument("--cookie", default=os.environ.get("TESLAFI_COOKIE", ""))
    ap.add_argument(
        "--cookie-only",
        action="store_true",
        help="Skip password login; use Cookie header only (best with 2FA)",
    )
    ap.add_argument("--from", dest="from_ym", default="2025-05")
    ap.add_argument("--to", dest="to_ym", default=f"{today.year}-{today.month:02d}")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "Téléchargements" / "teslafi",
    )
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--sleep",
        type=float,
        default=1.5,
        help="Seconds between months (default 1.5; be polite on long ranges)",
    )
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--debug", action="store_true")
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        default=True,
        help="Keep header-only months (holes in history) as small CSVs (default on)",
    )
    args = ap.parse_args()

    out: Path = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    client = TeslaFiClient(debug_dir=out / "debug")

    cookie = (args.cookie or "").strip()

    if args.cookie_only:
        if not cookie:
            print("Need --cookie or TESLAFI_COOKIE with --cookie-only", file=sys.stderr)
            return 2
        client.set_cookie_header(cookie)
        print("Using cookie only. Verifying session…")
        try:
            client.assert_authenticated()
            print(f"  session OK (cookies: {client.cookie_summary()})")
        except Exception as exc:
            print(f"Session check failed: {exc}", file=sys.stderr)
            print(
                "\nWith 2FA, easiest path:\n"
                "  1. Log in to TeslaFi in Chrome (complete 2FA)\n"
                "  2. Select the CORRECT car (Corentin) if multi-vehicle\n"
                "  3. Open https://teslafi.com/export2.php\n"
                "  4. F12 → Network → F5 → export2.php → Request Headers → Cookie\n"
                "  5. export TESLAFI_COOKIE='…full line…'\n",
                file=sys.stderr,
            )
            return 1
    else:
        user = (args.user or "").strip()
        password = args.password
        if not user:
            user = input("TeslaFi username/email: ").strip()
        if not password:
            password = getpass.getpass("TeslaFi password: ")
        if not user or not password:
            print("Username and password required (or use --cookie-only).", file=sys.stderr)
            return 2
        print("Logging in…")
        try:
            client.login(user, password, totp_code=(args.totp or None))
        except Exception as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            print(
                "\nTips:\n"
                "  • Wrong password burns attempts; TeslaFi can lock the account.\n"
                "  • 2FA: enter the app/email code when prompted.\n"
                "  • Or login in Chrome (2FA there) then --cookie-only.\n"
                "  • Multi-car: select Corentin on TeslaFi before export.\n",
                file=sys.stderr,
            )
            return 1

    start = parse_ym(args.from_ym)
    end = parse_ym(args.to_ym)
    exclude = {parse_ym(x) for x in args.exclude}
    months = list(iter_months(start, end))
    print(f"Will download {len(months)} month(s) → {out}")
    print("Tip: leave this terminal open; long ranges take ~2–10+ min.")

    ok = empty = fail = skipped = 0
    for i, (year, month) in enumerate(months):
        path = out / fname(month, year)
        label = f"{year}-{month:02d}"
        if (year, month) in exclude:
            print(f"  skip {label} (excluded)")
            skipped += 1
            continue
        if path.exists() and args.skip_existing and not args.force:
            print(f"  skip {label} (exists {path.name})")
            skipped += 1
            continue
        try:
            print(f"  download {label} …", end="", flush=True)
            data = client.download_month(year, month)
            rows = csv_data_rows(data)
            path.write_bytes(data)
            if rows == 0:
                print(f" EMPTY (hole?) → {path.name} ({len(data)} bytes)")
                empty += 1
            else:
                print(f" OK → {path.name} ({len(data)} bytes, ~{rows} data rows)")
                ok += 1
        except Exception as exc:
            print(f" FAIL: {exc}")
            fail += 1
            low = str(exc).lower()
            if "not authenticated" in low or "login page" in low or "csrf" in low:
                print(
                    "Stopping (session lost). Re-auth and re-run with --skip-existing.",
                    file=sys.stderr,
                )
                break
        if i < len(months) - 1:
            time.sleep(max(0.0, args.sleep))

    print(f"Done: ok={ok} empty={empty} skipped={skipped} fail={fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
