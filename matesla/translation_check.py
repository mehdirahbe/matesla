"""
Detect missing / incomplete translations for configured languages.

Why: easy to add {% trans %} / _("…") and forget makemessages + msgstr.
English is the source language (msgid). Every other code in settings.LANGUAGES
must have a .po with a non-empty, non-fuzzy translation.

Also scans templates/Python for simple gettext strings so brand-new
unextracted msgids show up before someone runs makemessages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import polib
from django.conf import settings

# Source language: English strings live in code as msgid (no locale/en required).
SOURCE_LANGUAGE_CODES = frozenset({"en", "en-us", "en-gb"})

# Technical / proper names that may legitimately stay English in all locales.
ALLOW_SAME_AS_SOURCE = frozenset(
    {
        "Client ID",
        "Client secret",
        "Sentry",
        "Firmware",
        "VIN",
        "OAuth",
        "GitHub",
        "MaTesla",
        "Tesla",
        "GPS",
        "SoC",
        "kWh",
        "Wh/km",
        "kWh/100 km",
        "EPA",
        "JSON",
        "CSV",
        "China",
        "Europe",
        "Date",
        "Distance",
        "Distance (km)",
        "Minimum",
        "Maximum",
        "Source",
        "Latitude",
        "Longitude",
        "Software",
        "min",
        "Français",
        "Espanol",
        "English",
        "Deutsch",
        "Nederlands",
        "Norsk",
        "Redirect URI",
        "%(url)s → HTTP %(code)s",  # technical status line
        "02 · SOFTWARE",  # section index; ES may keep loanword
    }
)

# Paths to scan for gettext extraction (relative to BASE_DIR).
SCAN_GLOBS = (
    "templates/**/*.html",
    "*/templates/**/*.html",
    "*/*/templates/**/*.html",
    "matesla/**/*.py",
    "personalstats/**/*.py",
    "accounts/**/*.py",
    "carimage/**/*.py",
    "mysite/**/*.py",
)

# Simple extractors (not a full xgettext — blocktrans plurals need makemessages).
_PY_GETTEXT = re.compile(
    r"""(?x)
    (?:
        \b_\(
        | \bgettext\s*\(
        | \bgettext_lazy\s*\(
        | \bugettext\s*\(
        | \bugettext_lazy\s*\(
        | \bpgettext\s*\(\s*['"][^'"]*['"]\s*,
    )
    \s*
    (?P<q>['"])(?P<s>(?:(?!\1).|\\.)*) (?P=q)
    """
)
_HTML_TRANS = re.compile(
    r"""\{%\s*trans\s+(?P<q>['"])(?P<s>(?:(?!\1).|\\.)*) (?P=q)(?:\s*\|\s*escapejs)?\s*%\}"""
)
_HTML_BLOCKTRANS = re.compile(
    r"""\{%\s*blocktrans(?:\s+[^%]*?)?%\s*\}(?P<body>.*?)\{\%\s*endblocktrans\s*%\}""",
    re.DOTALL,
)


@dataclass
class TranslationReport:
    """Findings for one language or for the whole project."""

    language: str = ""
    missing_po_file: bool = False
    empty_msgstr: list[str] = field(default_factory=list)
    fuzzy: list[str] = field(default_factory=list)
    same_as_source: list[str] = field(default_factory=list)
    unextracted: list[str] = field(default_factory=list)
    # msgids present in this .po but not in another language's .po
    extra_vs_others: list[str] = field(default_factory=list)

    def has_errors(
        self,
        *,
        fail_on_same_as_source: bool = False,
        fail_on_unextracted: bool = True,
    ) -> bool:
        if self.missing_po_file:
            return True
        if self.empty_msgstr or self.fuzzy:
            return True
        if fail_on_unextracted and self.unextracted:
            return True
        if fail_on_same_as_source and self.same_as_source:
            return True
        return False

    def summary_lines(self) -> list[str]:
        lines = []
        if self.language:
            lines.append(f"## {self.language}")
        if self.missing_po_file:
            lines.append("  ERROR: missing locale/.../django.po")
            return lines
        if self.empty_msgstr:
            lines.append(f"  empty msgstr: {len(self.empty_msgstr)}")
            for message_id in self.empty_msgstr[:25]:
                lines.append(f"    - {message_id!r}")
            if len(self.empty_msgstr) > 25:
                lines.append(f"    … +{len(self.empty_msgstr) - 25} more")
        if self.fuzzy:
            lines.append(f"  fuzzy: {len(self.fuzzy)}")
            for message_id in self.fuzzy[:15]:
                lines.append(f"    - {message_id!r}")
        if self.same_as_source:
            lines.append(
                f"  same as English (review): {len(self.same_as_source)}"
            )
            for message_id in self.same_as_source[:15]:
                lines.append(f"    - {message_id!r}")
        if self.unextracted:
            lines.append(
                f"  in source but not in .po (run makemessages): "
                f"{len(self.unextracted)}"
            )
            for message_id in self.unextracted[:25]:
                lines.append(f"    - {message_id!r}")
            if len(self.unextracted) > 25:
                lines.append(f"    … +{len(self.unextracted) - 25} more")
        if not any(
            (
                self.empty_msgstr,
                self.fuzzy,
                self.same_as_source,
                self.unextracted,
                self.missing_po_file,
            )
        ):
            lines.append("  OK")
        return lines


def target_language_codes() -> list[str]:
    """
    Languages that need .po files (everything in LANGUAGES except English).

    Uses the primary subtag (es-mx → es) for the locale/ directory name when
    that folder exists, else the full code.
    """
    base_dir = Path(settings.BASE_DIR)
    codes = []
    for language_code, _label in settings.LANGUAGES:
        primary = language_code.split("-")[0].lower()
        if primary in SOURCE_LANGUAGE_CODES or language_code.lower() in SOURCE_LANGUAGE_CODES:
            continue
        # Prefer locale/fr over locale/fr-fr when both could match
        if (base_dir / "locale" / primary / "LC_MESSAGES").is_dir():
            codes.append(primary)
        else:
            codes.append(language_code)
    # stable unique
    seen = set()
    ordered = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


def po_path_for(language_code: str) -> Path:
    return (
        Path(settings.BASE_DIR)
        / "locale"
        / language_code
        / "LC_MESSAGES"
        / "django.po"
    )


def _unescape_literal(raw: str) -> str:
    """Unescape common Python/Django string escapes without mangling UTF-8."""
    return (
        raw.replace("\\\\", "\0")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\0", "\\")
    )


def extract_source_message_ids(base_dir: Path | None = None) -> set[str]:
    """
    Best-effort scan of templates and Python for gettext strings.

    Complements .po checks: catches {% trans "Foo" %} never run through
    makemessages. Multi-line _() / blocktrans with complex vars still need
    makemessages for full coverage.
    """
    base_dir = base_dir or Path(settings.BASE_DIR)
    found: set[str] = set()
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        files.extend(base_dir.glob(pattern))
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        if any(
            part in {"migrations", ".venv", "staticfiles", "__pycache__"}
            for part in path.parts
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".py":
            for match in _PY_GETTEXT.finditer(text):
                found.add(_unescape_literal(match.group("s")))
        elif path.suffix in {".html", ".txt"}:
            for match in _HTML_TRANS.finditer(text):
                found.add(_unescape_literal(match.group("s")))
            # blocktrans with {{ vars }} needs makemessages (msgid placeholders
            # differ from our naive %s rewrite) — skip to avoid false positives.
    return {
        message_id
        for message_id in found
        if message_id and message_id.strip() and len(message_id.strip()) >= 2
    }

def check_language_po(
    language_code: str,
    *,
    source_message_ids: set[str] | None = None,
    allow_same_as_source: set[str] | None = None,
) -> TranslationReport:
    report = TranslationReport(language=language_code)
    path = po_path_for(language_code)
    if not path.is_file():
        report.missing_po_file = True
        if source_message_ids:
            report.unextracted = sorted(source_message_ids)
        return report

    catalog = polib.pofile(str(path))
    po_ids: set[str] = set()
    allow = allow_same_as_source if allow_same_as_source is not None else ALLOW_SAME_AS_SOURCE

    for entry in catalog:
        if entry.obsolete:
            continue
        message_id = entry.msgid or ""
        if not message_id:
            continue  # header
        po_ids.add(message_id)
        if entry.fuzzy:
            report.fuzzy.append(message_id)
        if not (entry.msgstr or "").strip():
            # plural forms
            if entry.msgid_plural:
                if not any((entry.msgstr_plural or {}).values()):
                    report.empty_msgstr.append(message_id)
            else:
                report.empty_msgstr.append(message_id)
        elif (
            entry.msgstr == message_id
            and message_id not in allow
            and len(message_id) > 2
            and any(character.isalpha() for character in message_id)
        ):
            # Likely forgotten translation (still English)
            report.same_as_source.append(message_id)

    if source_message_ids is not None:
        # Only flag clear single-line UI strings missing from the catalog.
        # Incomplete multi-line extracts are too noisy for hard failures.
        missing = []
        for message_id in source_message_ids:
            if message_id in po_ids:
                continue
            if "\n" in message_id:
                continue
            if message_id.endswith(" ") or message_id.startswith(" "):
                # Often a partial multi-line extract
                continue
            missing.append(message_id)
        report.unextracted = sorted(missing)

    return report


def check_all_translations(
    *,
    scan_source: bool = True,
    fail_on_same_as_source: bool = False,
    fail_on_unextracted: bool = True,
) -> tuple[list[TranslationReport], bool]:
    """
    Returns (reports, ok).

    Hard errors by default: missing .po, empty msgstr, fuzzy, unextracted.
    same_as_source only fails when fail_on_same_as_source=True.
    """
    source_ids = extract_source_message_ids() if scan_source else None
    reports = []
    ok = True
    for language_code in target_language_codes():
        report = check_language_po(
            language_code, source_message_ids=source_ids
        )
        reports.append(report)
        if report.has_errors(
            fail_on_same_as_source=fail_on_same_as_source,
            fail_on_unextracted=fail_on_unextracted,
        ):
            ok = False
    return reports, ok
