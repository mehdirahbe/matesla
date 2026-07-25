from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _set_sqlite_pragma(sender, connection, **kwargs):
    """WAL allows concurrent readers while one writer runs (web + rare side tools)."""
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")


class MateslaConfig(AppConfig):
    name = "matesla"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        connection_created.connect(_set_sqlite_pragma)
