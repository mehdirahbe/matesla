"""
Report missing / incomplete translations for every non-English LANGUAGES entry.

Usage:
  python manage.py check_translations
  python manage.py check_translations --strict-same   # also fail if msgstr == msgid
  python manage.py check_translations --no-scan       # only validate existing .po

Workflow after adding {% trans %} / _("…"):
  1) python manage.py makemessages -a --no-wrap
  2) fill empty msgstr in locale/*/LC_MESSAGES/django.po
  3) python manage.py check_translations
  4) python manage.py compilemessages
"""

from django.core.management.base import BaseCommand, CommandError

from matesla.translation_check import check_all_translations, target_language_codes


class Command(BaseCommand):
    help = (
        "Verify French/Spanish/… .po files cover source strings and have no "
        "empty or fuzzy translations (languages from settings.LANGUAGES)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-scan",
            action="store_true",
            help="Do not scan templates/Python; only check existing .po entries.",
        )
        parser.add_argument(
            "--strict-same",
            action="store_true",
            help="Fail when msgstr equals English msgid (except allowlisted terms).",
        )
        parser.add_argument(
            "--strict-unextracted",
            action="store_true",
            help="Fail when source has {% trans %}/_() strings missing from .po.",
        )

    def handle(self, *args, **options):
        languages = target_language_codes()
        if not languages:
            self.stdout.write(
                self.style.WARNING(
                    "No non-English languages in settings.LANGUAGES — nothing to check."
                )
            )
            return

        self.stdout.write(
            f"Checking translations for: {', '.join(languages)} "
            f"(source language = English msgids)"
        )
        reports, ok = check_all_translations(
            scan_source=not options["no_scan"],
            fail_on_same_as_source=options["strict_same"],
            fail_on_unextracted=options["strict_unextracted"],
        )
        for report in reports:
            for line in report.summary_lines():
                if line.startswith("  ERROR") or line.startswith("  empty") or line.startswith("  fuzzy") or line.startswith("  in source"):
                    self.stdout.write(self.style.ERROR(line) if "ERROR" in line or line.startswith("  empty") or line.startswith("  fuzzy") or line.startswith("  in source") else line)
                elif line.startswith("  same"):
                    style = (
                        self.style.ERROR
                        if options["strict_same"]
                        else self.style.WARNING
                    )
                    self.stdout.write(style(line))
                elif line.strip() == "OK":
                    self.stdout.write(self.style.SUCCESS(line))
                else:
                    self.stdout.write(line)

        if not ok:
            raise CommandError(
                "Translation check failed. Run makemessages, fill msgstr, "
                "then re-run check_translations."
            )
        self.stdout.write(self.style.SUCCESS("All translation checks passed."))
