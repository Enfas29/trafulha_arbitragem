"""Catálogo, resumos e ciclo reativo: testes offline, sem mensagens reais."""
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import monitor_v11 as monitor
from monitor_workers_v13 import SnapshotRefresher, AsyncNoticeSender


class AuxiliaryWorkerTests(unittest.TestCase):
    def test_retry_after_respects_seconds_and_http_date(self):
        self.assertEqual(monitor.retry_after_seconds("120", now_epoch=1000), 120)
        date = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
        self.assertEqual(monitor.retry_after_seconds(date, now_epoch=1000), 120)
        for bad in (None, "invalid", "nan", "inf", "-1"):
            with self.subTest(bad=bad):
                self.assertEqual(monitor.retry_after_seconds(bad, now_epoch=1000), 2)

    def test_catalog_loader_does_not_block_and_publishes_only_complete_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        snapshot = ([{"key": "complete"}], 1, 2, 2, {})

        def loader():
            entered.set()
            release.wait(2)
            return snapshot

        worker = SnapshotRefresher(loader)
        worker.start()
        try:
            self.assertTrue(worker.request())
            self.assertTrue(entered.wait(2))
            self.assertIsNone(worker.poll())
            self.assertFalse(worker.request())
            release.set()
            deadline = time.monotonic() + 2
            result = None
            while result is None and time.monotonic() < deadline:
                result = worker.poll()
                if result is None:
                    time.sleep(.001)
            self.assertEqual(result, (True, snapshot))
        finally:
            release.set()
            worker.stop()

    def test_catalog_failure_does_not_publish_partial_data_or_exception_contents(self):
        def failed():
            raise RuntimeError("sensitive URL must not propagate")

        worker = SnapshotRefresher(failed)
        worker.start()
        try:
            worker.request()
            deadline = time.monotonic() + 2
            result = None
            while result is None and time.monotonic() < deadline:
                result = worker.poll()
                if result is None:
                    time.sleep(.001)
            self.assertEqual(result, (False, "RuntimeError"))
        finally:
            worker.stop()

    def test_blocked_summary_sender_leaves_bounded_nonblocking_queue(self):
        entered, release = threading.Event(), threading.Event()

        def sender(_message):
            entered.set()
            release.wait(2)

        worker = AsyncNoticeSender(sender, max_pending=1)
        worker.start()
        try:
            self.assertTrue(worker.submit("first"))
            self.assertTrue(entered.wait(2))
            self.assertTrue(worker.submit("second"))
            self.assertFalse(worker.submit("third"))
            self.assertEqual(worker.pending.qsize(), 1)
        finally:
            release.set()
            worker.stop()

    def test_main_drains_new_ws_event_between_two_market_reads(self):
        now = time.time()
        pairs = [{
            "key": str(i), "poly_asset": "asset-" + str(i),
            "poly": {"id": "p-" + str(i)}, "sx_target": 1,
            "poly_label": "A", "sx_label": "B",
            "sx": {"hash": "s-" + str(i), "start": now + 3600,
                   "live_enabled": False, "sport": "tennis", "teams": ("A", "B")},
        } for i in range(4)]

        class Feed:
            def __init__(self):
                self.events = {}
            def start(self):
                return True
            def stop(self):
                pass
            def update(self, _assets):
                pass
            def take_events(self):
                events, self.events = self.events, {}
                return events

        feed = Feed()
        actual_scan = monitor.scan_pass
        observed = []

        def scan(*args, **kwargs):
            observed.append(kwargs["work_item"][0]["key"])
            result = actual_scan(*args, **kwargs)
            if len(observed) == 1:
                feed.events["asset-3"] = time.monotonic()
            else:
                raise KeyboardInterrupt()
            return result

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(monitor.os.environ, {"EUR_PER_USD": "0", "TRAFLUHA_STORAGE_MAINTENANCE": "0", "RAILWAY_VOLUME_MOUNT_PATH": ""}), \
             patch.object(monitor, "DB", str(Path(directory) / "monitor.sqlite3")), \
             patch.object(monitor, "PAPER_V12_ENABLED", False), \
             patch.object(monitor, "SX_WS_V12_ENABLED", False), \
             patch.object(monitor, "POLY_WS_V12_ENABLED", True), \
             patch.object(monitor.poly_hints, "PolyBookHints", return_value=feed), \
             patch.object(monitor, "catalog_snapshot", return_value=(pairs, 4, 4, 4, {"tennis": 4})), \
             patch.object(monitor.bot, "poly_book", side_effect=lambda _: ([(.6, 200)], time.monotonic())), \
             patch.object(monitor.bot, "sx_book", side_effect=lambda *_: ([(.6, 200)], time.monotonic())), \
             patch.object(monitor, "telegram"), \
             patch.object(monitor, "scan_pass", side_effect=scan):
            monitor.main()
        self.assertEqual(observed, ["0", "3"])

    def test_complete_empty_catalog_still_emits_periodic_summary(self):
        observed = []

        def summary(_conn, count, **kwargs):
            observed.append(count)
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(monitor.os.environ, {"EUR_PER_USD": "0", "TRAFLUHA_STORAGE_MAINTENANCE": "0", "RAILWAY_VOLUME_MOUNT_PATH": ""}), \
             patch.object(monitor, "DB", str(Path(directory) / "monitor.sqlite3")), \
             patch.object(monitor, "PAPER_V12_ENABLED", False), \
             patch.object(monitor, "SX_WS_V12_ENABLED", False), \
             patch.object(monitor, "POLY_WS_V12_ENABLED", False), \
             patch.object(monitor, "catalog_snapshot", return_value=([], 0, 0, 0, {})), \
             patch.object(monitor, "send_summary", side_effect=summary), \
             patch.object(monitor.bot, "get_json", side_effect=AssertionError("offline test")):
            monitor.main()
        self.assertEqual(observed, [0])


if __name__ == "__main__":
    unittest.main()
