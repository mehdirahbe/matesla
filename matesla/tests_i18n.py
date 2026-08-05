"""
Translation coverage tests — fail if a configured language is incomplete.

Uses settings.LANGUAGES (skips English source). Empty msgstr and fuzzy entries
are hard failures. "msgstr == msgid" is reported as a soft list only when
strict mode is enabled via env (default: soft, not fail).

Unextracted strings (in templates but not in .po) are hard failures so
forgetting makemessages is visible in CI/tests.
"""

from django.test import SimpleTestCase

from matesla.translation_check import (
    check_all_translations,
    target_language_codes,
)


class TranslationCoverageTests(SimpleTestCase):
    def test_non_english_languages_are_configured(self):
        # Sanity: fr/es + DE/NL/NB (top Tesla EU markets beyond EN)
        codes = target_language_codes()
        for code in ("fr", "es", "de", "nl", "nb"):
            self.assertIn(code, codes)

    def test_po_files_exist_without_empty_or_fuzzy(self):
        """
        Hard gate: every non-English LANGUAGES entry has a .po with no empty
        msgstr and no fuzzy entries. (msgstr == msgid is review-only.)

        Workflow after adding a new {% trans %}:
          makemessages -a → fill msgstr → check_translations → compilemessages
        """
        reports, ok = check_all_translations(
            scan_source=False,
            fail_on_same_as_source=False,
            fail_on_unextracted=False,
        )
        details = []
        for report in reports:
            details.extend(report.summary_lines())
        message = "\n".join(details)
        self.assertTrue(
            ok,
            "Empty or fuzzy translations in locale/*/django.po.\n"
            "Run: python manage.py makemessages -a\n"
            "Then fill msgstr and: python manage.py check_translations\n"
            + message,
        )

    def test_check_translations_command_reports_source_scan(self):
        """Source scan runs without crashing; used by management command."""
        reports, _ok = check_all_translations(
            scan_source=True,
            fail_on_same_as_source=False,
            fail_on_unextracted=False,
        )
        self.assertTrue(reports)
        for report in reports:
            self.assertFalse(report.missing_po_file, report.language)
