"""
Shared helpers for Django tests.

Keep language loops in sync with settings.LANGUAGES so adding Spanish
(or any locale) does not require hunting hardcoded {"fr", "en"} sets.
"""

from django.conf import settings


def configured_language_codes():
    """
    Language codes activated for i18n_patterns (e.g. en, fr, es).

    Order follows LANGUAGES in settings — same as the language switcher.
    """
    return [code for code, _label in settings.LANGUAGES]
