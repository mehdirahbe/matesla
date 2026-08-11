"""Capture anti-overlap lock (multi-threaded gunicorn + cron)."""

from __future__ import annotations

import threading

from django.test import Client, SimpleTestCase

from matesla import capture as capture_mod


class CaptureOverlapLockTests(SimpleTestCase):
    def test_second_capture_skipped_while_first_holds_lock(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            capture_mod._capture_lock.acquire(blocking=True)
            held.set()
            release.wait(timeout=5)
            capture_mod._capture_lock.release()

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(held.wait(timeout=2))

        stats = capture_mod.capture_all_online_vehicles()
        self.assertTrue(stats.get("skipped_already_running"))
        self.assertEqual(stats.get("fleet_calls"), 0)
        self.assertTrue(any("déjà en cours" in m for m in stats.get("messages") or []))

        release.set()
        t.join(timeout=2)

    def test_endpoint_returns_json_when_busy(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            capture_mod._capture_lock.acquire(blocking=True)
            held.set()
            release.wait(timeout=5)
            capture_mod._capture_lock.release()

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(held.wait(timeout=2))

        client = Client()
        response = client.get("/matesla/internal/capture")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("skipped_already_running"))

        release.set()
        t.join(timeout=2)
