"""Reactive dispatch keeps the full paper checks; no network or real orders."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import monitor_v11 as monitor
import paper_v12 as paper
from latency_metrics_v13 import LatencyMetrics
from reactive_scheduler_v13 import ReactiveScheduler


class FakeClock:
    def __init__(self):
        self.mono = 1000.0
        self.epoch = 1800000000.0
        self.sleeps = []

    def monotonic(self):
        return self.mono

    def time(self):
        return self.epoch

    def advance(self, seconds):
        self.mono += seconds
        self.epoch += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class ReactiveMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.monitor_db = monitor.open_database(str(self.path / "monitor.sqlite3"))
        self.ledger_path = self.path / "paper.sqlite3"
        self.ledger = paper.open_database(self.ledger_path)
        self.addCleanup(self.monitor_db.close)
        self.addCleanup(self.ledger.close)
        self.clock = FakeClock()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(monitor.time, "time", self.clock.time))
        self.patches.enter_context(patch.object(monitor.time, "monotonic", self.clock.monotonic))
        self.metrics = LatencyMetrics(clock=self.clock.monotonic)
        self.patches.enter_context(patch.object(monitor, "LATENCIES", self.metrics))
        self.patches.enter_context(patch.object(monitor.bot, "FX", 0.91))
        self.patches.enter_context(patch.object(paper, "ALLOW_LIVE_UNVERIFIED", False))
        # Keep the real confirmation implementation and replace only its wait.
        real_confirmation = monitor.confirm_paper_snapshot
        def confirm_without_wall_sleep(*args, **kwargs):
            kwargs["sleep_fn"] = self.clock.sleep
            return real_confirmation(*args, **kwargs)
        self.patches.enter_context(patch.object(
            monitor, "confirm_paper_snapshot", side_effect=confirm_without_wall_sleep,
        ))
        self.send_inline = self.patches.enter_context(patch.object(paper, "send_open_once"))
        self.telegram = self.patches.enter_context(patch.object(paper, "send_telegram_notice"))

    def pair(self, suffix="one", start_in=3600):
        start = self.clock.time() + start_in
        return {
            "key": f"pair-{suffix}",
            "poly": {"id": f"poly-{suffix}", "type": "moneyline",
                     "sport": "basketball", "start": start},
            "sx": {"hash": f"sx-{suffix}", "sport": "basketball",
                   "teams": ("Example A", "Example B"), "start": start,
                   "live_enabled": False},
            "poly_asset": f"asset-{suffix}", "sx_target": 1,
            "poly_label": "Example A", "sx_label": "Example B",
        }

    def books(self, _pair, poly_price=0.4, sx_price=0.5, elapsed=0.1):
        self.clock.advance(elapsed)
        stamp = self.clock.monotonic()
        return ([(poly_price, 200.0)], stamp), ([(sx_price, 200.0)], stamp)

    def scan(self, pair, event=None, notify=None):
        return monitor.scan_pass(
            self.monitor_db, [pair], 0, 0.91, paper_conn=self.ledger,
            work_item=(pair, event, "ws"),
            notify_open=notify or (lambda: None), verbose=False,
        )

    def assert_untouched(self):
        self.assertEqual(paper.free_balances(self.ledger), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)
        self.send_inline.assert_not_called()
        self.telegram.assert_not_called()

    def test_reactive_entry_reads_both_apis_twice_and_commits_before_notify(self):
        pair = self.pair()
        committed = []

        def after_commit():
            # An independent reader must see both debits and the saved message.
            reader = sqlite3.connect(self.ledger_path)
            try:
                committed.append({
                    "in_transaction": self.ledger.in_transaction,
                    "trades": reader.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0],
                    "notice": reader.execute(
                        "SELECT message,sent_epoch FROM paper_v12_open_notifications"
                    ).fetchone(),
                    "balances": dict(reader.execute("SELECT platform,free_cents FROM paper_v12_wallets")),
                })
            finally:
                reader.close()

        def poly(_asset):
            return [(0.4, 200.0)], self.clock.monotonic()
        def sx(_market, _side):
            return [(0.5, 200.0)], self.clock.monotonic()

        with patch.object(monitor.bot, "poly_book", side_effect=poly) as poly_api, \
             patch.object(monitor.bot, "sx_book", side_effect=sx) as sx_api:
            result = self.scan(pair, (self.clock.monotonic() - 0.2, {"POLY"}), after_commit)

        self.assertEqual(result["paper_reasons"], {"registada": 1})
        self.assertEqual(poly_api.call_count, 2)
        self.assertEqual(sx_api.call_count, 2)
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(len(committed), 1)
        self.assertFalse(committed[0]["in_transaction"])
        self.assertEqual(committed[0]["trades"], 1)
        self.assertIn("APOSTA VIRTUAL", committed[0]["notice"][0])
        self.assertIsNone(committed[0]["notice"][1])
        self.assertLess(committed[0]["balances"]["poly"], 100000)
        self.assertLess(committed[0]["balances"]["sx"], 100000)
        self.send_inline.assert_not_called()
        self.telegram.assert_not_called()

    def test_out_of_window_event_does_not_read_or_debit(self):
        pair = self.pair(start_in=(monitor.bot.MAX_FUTURE_H + 1) * 3600)
        with patch.object(monitor, "fetch_books") as fetch:
            result = self.scan(pair, (self.clock.monotonic(), {"SX"}))
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["ws_skipped"], 1)
        fetch.assert_not_called()
        self.assert_untouched()

    def test_first_or_confirmation_timeout_never_debits(self):
        pair = self.pair()
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on):
                calls = []
                self.clock.sleeps.clear()
                def fetch(candidate):
                    calls.append(candidate["key"])
                    if len(calls) == fail_on:
                        raise TimeoutError("offline simulated read")
                    return self.books(candidate)
                with patch.object(monitor, "fetch_books", side_effect=fetch):
                    self.scan(pair, (self.clock.monotonic(), {"POLY", "SX"}))
                self.assertEqual(len(calls), fail_on)
                self.assertEqual(self.clock.sleeps, [] if fail_on == 1 else [0.25])
                self.assert_untouched()

    def test_stale_or_future_first_snapshot_is_rejected_without_recheck(self):
        pair = self.pair()
        for stale_sides in ((0,), (1,), (0, 1)):
            for delta in (-monitor.MAX_QUOTE_AGE_SECONDS - 0.1, 0.1):
                with self.subTest(sides=stale_sides, delta=delta):
                    stamps = [self.clock.monotonic(), self.clock.monotonic()]
                    for side in stale_sides:
                        stamps[side] += delta
                    self.clock.sleeps.clear()
                    with patch.object(monitor, "fetch_books") as fetch:
                        result = monitor.confirm_paper_snapshot(
                            pair, [(0.4, 200)], [(0.5, 200)], *stamps,
                            0.91, self.ledger,
                        )
                    self.assertIsNone(result)
                    fetch.assert_not_called()
                    self.assertEqual(self.clock.sleeps, [])
                    self.assert_untouched()

    def test_stale_or_future_second_snapshot_is_rejected(self):
        pair = self.pair()
        for invalid_sides in ((0,), (1,), (0, 1)):
            for delta in (-monitor.MAX_QUOTE_AGE_SECONDS - 0.1, 0.1):
                with self.subTest(sides=invalid_sides, delta=delta):
                    self.clock.sleeps.clear()
                    first_stamp = self.clock.monotonic()
                    def invalid_second(_pair):
                        stamps = [self.clock.monotonic(), self.clock.monotonic()]
                        for side in invalid_sides:
                            stamps[side] += delta
                        return ([(0.4, 200)], stamps[0]), ([(0.5, 200)], stamps[1])
                    with patch.object(monitor, "fetch_books", side_effect=invalid_second) as fetch:
                        result = monitor.confirm_paper_snapshot(
                            pair, [(0.4, 200)], [(0.5, 200)], first_stamp, first_stamp,
                            0.91, self.ledger,
                        )
                    self.assertIsNone(result)
                    self.assertEqual(fetch.call_count, 1)
                    self.assertEqual(self.clock.sleeps, [0.25])
                    self.assert_untouched()

    def test_start_crossed_during_second_read_cannot_reuse_pregame_even_with_live_allowed(self):
        pair = self.pair(start_in=0.4)
        pair["sx"]["live_enabled"] = True
        with patch.object(paper, "ALLOW_LIVE_UNVERIFIED", True), \
             patch.object(monitor, "fetch_books", side_effect=self.books) as fetch:
            result = self.scan(pair, (self.clock.monotonic(), {"POLY"}))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertGreater(self.clock.time(), pair["sx"]["start"])
        self.assertEqual(result["paper_reasons"]["inicio_durante_validacao"], 1)
        self.assert_untouched()

    def test_rotation_after_reactive_entry_does_not_repeat_debits_or_notification(self):
        pair = self.pair()
        notifications = []
        with patch.object(monitor, "fetch_books", side_effect=self.books) as fetch:
            self.scan(pair, (self.clock.monotonic(), {"SX"}), lambda: notifications.append(1))
            balances = paper.free_balances(self.ledger)
            fallback = monitor.scan_pass(
                self.monitor_db, [pair], 0, 0.91, paper_conn=self.ledger,
                notify_open=lambda: notifications.append(1), verbose=False,
            )
        self.assertEqual(fetch.call_count, 3)  # two confirmed reads, then one observation
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(fallback["paper_reasons"], {"ja_registada": 1})
        self.assertEqual(notifications, [1])
        self.assertEqual(paper.free_balances(self.ledger), balances)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)
        self.send_inline.assert_not_called()

    def test_next_scheduler_choice_handles_new_hint_and_keeps_original_queue_time(self):
        pairs = [self.pair(str(number)) for number in range(3)]
        scheduler = monitor.ReactiveScheduler(pairs, normal_interval=0, min_key_interval=0)
        self.assertIsInstance(scheduler, ReactiveScheduler)
        first = scheduler.choose(now=self.clock.monotonic())
        self.assertEqual(first[0]["key"], pairs[0]["key"])
        seen = []

        def read_with_incoming_event(candidate):
            seen.append(candidate["key"])
            if len(seen) == 1:
                self.clock.advance(0.05)
                scheduler.ingest({pairs[2]["poly_asset"]: self.clock.monotonic()}, now=self.clock.monotonic())
                self.clock.advance(0.03)
                scheduler.ingest({pairs[2]["poly_asset"]: self.clock.monotonic()},
                                 {pairs[2]["sx"]["hash"]: self.clock.monotonic()},
                                 now=self.clock.monotonic())
                return self.books(candidate, poly_price=0.55, sx_price=0.6, elapsed=0.02)
            return self.books(candidate, poly_price=0.55, sx_price=0.6)

        with patch.object(monitor, "fetch_books", side_effect=read_with_incoming_event):
            monitor.scan_pass(self.monitor_db, pairs, 0, 0.91, paper_conn=self.ledger,
                              work_item=first, notify_open=lambda: None, verbose=False)
            scheduler.completed(first[0]["key"], now=self.clock.monotonic())
            following = scheduler.choose(now=self.clock.monotonic())
            self.assertEqual(following[0]["key"], pairs[2]["key"])
            self.assertEqual(following[2], "ws")
            self.assertAlmostEqual(following[1][0], 1000.05)
            self.assertEqual(following[1][1], {"POLY", "SX"})
            result = monitor.scan_pass(
                self.monitor_db, pairs, 0, 0.91, paper_conn=self.ledger,
                work_item=following, notify_open=lambda: None, verbose=False,
            )
        self.assertEqual(seen, [pairs[0]["key"], pairs[2]["key"]])
        timing = result["ws_latencies"][0]
        self.assertAlmostEqual(timing["queue"], 50.0)
        self.assertAlmostEqual(timing["rest"], 100.0)
        self.assertAlmostEqual(timing["decision"], 150.0)
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["decision_calls"], 1)
        self.assertAlmostEqual(snapshot["queue"]["p50_ms"], 50.0)
        self.assertAlmostEqual(snapshot["decision"]["p50_ms"], 150.0)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main(verbosity=2)
