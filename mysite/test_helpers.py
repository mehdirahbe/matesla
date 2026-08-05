"""
Shared helpers for Django tests.

Keep language loops in sync with settings.LANGUAGES so adding a locale
(e.g. de/nl/nb) does not require hunting hardcoded language sets.
"""

from django.conf import settings


def configured_language_codes():
    """
    Language codes activated for i18n_patterns (e.g. en, fr, es).

    Order follows LANGUAGES in settings — same as the language switcher.
    """
    return [code for code, _label in settings.LANGUAGES]
