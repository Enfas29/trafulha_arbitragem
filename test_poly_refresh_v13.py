"""Offline tests: refreshing Polymarket hints cannot wait on a WS close."""

import json
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import poly_ws_v12 as poly


class BlockingSocket:
    def __init__(self, entered=None, release=None, activity=None):
        self.entered = entered or threading.Event()
        self.release = release or threading.Event()
        self.closed = threading.Event()
        self.close_calls = 0
        self.activity = activity

    def close(self):
        self.close_calls += 1
        if self.activity is not None:
            with self.activity["lock"]:
                self.activity["current"] += 1
                self.activity["maximum"] = max(self.activity["maximum"], self.activity["current"])
        self.entered.set()
        try:
            if not self.release.wait(5):
                raise TimeoutError("test close was not released")
        finally:
            if self.activity is not None:
                with self.activity["lock"]:
                    self.activity["current"] -= 1
            self.closed.set()


class PolyRefreshTests(unittest.TestCase):
    def test_refresh_returns_during_close_and_reconnect_uses_final_assets(self):
        watcher = poly.PolyBookHints()
        watcher.update(["a"])
        watcher.mark("a")
        first_event = watcher.hot["a"]
        applications = []
        initial_open = threading.Event()
        final_open = threading.Event()

        class FakeWebSocketApp(BlockingSocket):
            def __init__(self, url, on_open, on_message, on_close, on_error):
                super().__init__()
                self.keep_running = False  # no background PING loop in this test
                self.on_open = on_open
                self.on_close = on_close
                self.subscriptions = []
                applications.append(self)

            def send(self, message):
                self.subscriptions.append(json.loads(message))

            def run_forever(self, **_):
                self.on_open(self)
                if len(applications) == 1:
                    initial_open.set()
                    self.closed.wait(5)
                else:
                    watcher.running = False
                    final_open.set()
                self.on_close(self)

        feed_thread = threading.Thread(target=watcher.run, daemon=True)
        updated = threading.Event()
        updater = None
        with patch.dict(sys.modules, {"websocket": SimpleNamespace(WebSocketApp=FakeWebSocketApp)}), \
             patch.object(poly.time, "sleep", return_value=None):
            try:
                feed_thread.start()
                self.assertTrue(initial_open.wait(2))
                old = applications[0]

                def refresh():
                    watcher.update(["b"])
                    updated.set()

                updater = threading.Thread(target=refresh, daemon=True)
                updater.start()
                self.assertTrue(updated.wait(1), "update waited for the close handshake")
                self.assertTrue(old.entered.wait(1))
                self.assertFalse(old.release.is_set())
                self.assertEqual(watcher.assets, ("b",))
                watcher.update(["b", "b"])
                watcher.update(["c", "d", "c"])
                self.assertEqual(watcher.assets, ("c", "d"))
                self.assertEqual(old.close_calls, 1)
                self.assertEqual(watcher.take_events(), {"a": first_event})

                old.release.set()
                self.assertTrue(final_open.wait(2))
                feed_thread.join(2)
                self.assertFalse(feed_thread.is_alive())
                self.assertEqual(applications[1].subscriptions, [
                    {"type": "market", "assets_ids": ["c", "d"]}])
                self.assertEqual(old.close_calls, 1)
            finally:
                watcher.running = False
                for socket in applications:
                    socket.release.set()
                if updater is not None:
                    updater.join(2)
                feed_thread.join(2)
                closer = watcher._close_thread
                if closer is not None:
                    closer.join(2)

    def test_new_socket_refresh_is_not_lost_while_old_socket_closes(self):
        watcher = poly.PolyBookHints()
        watcher.update(["a"])
        activity = {"lock": threading.Lock(), "current": 0, "maximum": 0}
        old, newer = BlockingSocket(activity=activity), BlockingSocket(activity=activity)
        watcher.socket = old
        try:
            watcher.update(["b"])
            self.assertTrue(old.entered.wait(1))
            # A network loop can reconnect while close() on the old socket is
            # still completing. Refreshes on that newer socket must survive.
            with watcher.lock:
                watcher.socket = newer
            watcher.update(["c"])
            watcher.update(["d"])
            watcher.stop()  # coalesces the same pending close; no parallel close
            self.assertEqual(watcher.assets, ("d",))
            self.assertFalse(newer.entered.is_set())
            old.release.set()
            self.assertTrue(newer.entered.wait(2))
            newer.release.set()
            closer = watcher._close_thread
            if closer is not None:
                closer.join(2)
                self.assertFalse(closer.is_alive())
            self.assertEqual(old.close_calls, 1)
            self.assertEqual(newer.close_calls, 1)
            self.assertEqual(activity["maximum"], 1)
            self.assertIsNone(watcher._close_thread)
        finally:
            old.release.set()
            newer.release.set()
            closer = watcher._close_thread
            if closer is not None:
                closer.join(2)

    def test_unchanged_selection_never_starts_close(self):
        watcher = poly.PolyBookHints()
        watcher.update(["a", "b"])
        socket = BlockingSocket()
        watcher.socket = socket
        watcher.update(["a", "b", "a", None])
        self.assertEqual(socket.close_calls, 0)
        self.assertIsNone(watcher._close_thread)
        self.assertEqual(watcher.assets, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
