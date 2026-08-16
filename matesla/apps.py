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
    # ~762 MB snapshot DB: keep a larger page cache and mmap so six parallel
    # personal-stats thumbs do not re-read the same hashedVin pages from disk.
    cursor.execute("PRAGMA cache_size=-131072;")  # 128 MiB
    cursor.execute("PRAGMA mmap_size=268435456;")  # 256 MiB
    cursor.execute("PRAGMA temp_store=MEMORY;")


class MateslaConfig(AppConfig):
    name = "matesla"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        connection_created.connect(_set_sqlite_pragma)
