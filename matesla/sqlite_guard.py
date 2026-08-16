"""Serialize heavy SQLite snapshot scans on the threaded web worker.

Gunicorn uses --workers 1 --threads 4. Six parallel Stats thumbs each scan
the same 500k-row snapshot table; WAL still contends and each query blows
out from ~0.3 s to ~2 s. One scan at a time keeps the in-process cost.
Does not change results — only lock around execute + fetchall.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_HEAVY_READ_LOCK = threading.RLock()


@contextmanager
def heavy_snapshot_read():
    with _HEAVY_READ_LOCK:
        yield
