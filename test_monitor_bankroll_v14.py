"""Production monitor wiring for reserves/reset; no live APIs or Telegram."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monitor_v11 as monitor
import paper_v12 as paper
from test_monitor_reactive_v13 import FakeClock


class MonitorBankrollTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.ledger_path = self.path / "paper.sqlite3"
        self.ledger = paper.open_database(self.ledger_path)
        self.observations = monitor.open_database(str(self.path / "monitor.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.observations.close)
        self.clock = FakeClock()

    def pair(self, number):
        start = self.clock.time() + 3600
        return {"key": f"bank-{number}",
                "poly": {"id": f"poly-bank-{number}", "type": "moneyline",
                         "sport": "basketball", "start": start},
                "sx": {"hash": f"sx-bank-{number}", "sport": "basketball",
                       "teams": ("Team A", "Team B"), "start": start,
                       "live_enabled": True},
                "poly_asset": f"token-{number}", "poly_label": "Team A",
                "sx_target": 1, "sx_label": "Team B"}

    def test_monitor_sizes_multiple_opportunities_without_consuming_either_reserve(self):
        def books(_pair):
            self.clock.advance(.01)
            stamp = self.clock.monotonic()
            return (([(.4, 5000)], stamp), ([(.5, 5000)], stamp))
        real_confirm = monitor.confirm_paper_snapshot
        def confirm(*args, **kwargs):
            return real_confirm(*args, **kwargs, sleep_fn=self.clock.sleep)
        with patch.object(monitor.time, "time", self.clock.time), \
             patch.object(monitor.time, "monotonic", self.clock.monotonic), \
             patch.object(monitor.bot, "FX", .91), \
             patch.object(monitor, "fetch_books", side_effect=books), \
             patch.object(monitor, "confirm_paper_snapshot", side_effect=confirm), \
             patch.object(paper, "send_telegram_notice") as send:
            for number in range(8):
                pair = self.pair(number)
                monitor.scan_pass(self.observations, [pair], 0, .91,
                                  paper_conn=self.ledger, notify_open=lambda: None,
                                  verbose=False)
                free = paper.free_balances(self.ledger)
                self.assertGreaterEqual(free["poly"], 30000)
                self.assertGreaterEqual(free["sx"], 30000)
                self.assertLessEqual(paper.open_exposure(self.ledger), 140000)
            send.assert_not_called()
        totals = [row[0] for row in self.ledger.execute(
            "SELECT poly_debit_cents+sx_debit_cents FROM paper_v12_trades ORDER BY id")]
        self.assertGreaterEqual(len(totals), 2)
        self.assertEqual(totals[0], 40000)
        self.assertTrue(all(value <= 40000 for value in totals))
        self.assertLess(totals[-1], 40000)  # The most constrained wallet shrinks the entry.
        self.assertLess(paper.allocation_budget(self.ledger).sx_available_cents, 100)

    def test_startup_applies_reset_before_constructing_or_starting_workers(self):
        pair = self.pair("old")
        opened = paper.try_paper_entry(self.ledger, pair, [( .4, 300)], [(.5, 300)],
                                       .91, "PRE_JOGO", .01)
        self.assertIsNotNone(opened)
        observed = []
        real_open = paper.open_database
        test = self

        class PaperWorker:
            def __init__(self, *_args, **_kwargs):
                observed.append(("constructed", paper.free_balances(test.ledger),
                                 paper.paper_summary(test.ledger)["open_trades"]))
            def start(self):
                observed.append(("started", paper.free_balances(test.ledger),
                                 paper.paper_summary(test.ledger)["open_trades"]))
            def is_alive(self):
                return True
            def stop(self):
                pass

        class Background:
            def __init__(self, *_args, **_kwargs):
                self.thread = SimpleNamespace(is_alive=lambda: True)
            def start(self):
                pass
            def stop(self):
                pass
            def poll(self):
                raise KeyboardInterrupt  # Exit before any market/API work.
            def submit(self, _message):
                raise AssertionError("No outgoing message expected")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "PAPER_RESET_REQUEST_ID": "reset-startup-test", "EUR_PER_USD": ".91",
                "TRAFLUHA_STORAGE_MAINTENANCE": "0",
            }))
            stack.enter_context(patch.object(monitor, "DB", str(self.path / "startup-monitor.sqlite3")))
            stack.enter_context(patch.object(paper, "PAPER_DB", str(self.ledger_path)))
            stack.enter_context(patch.object(paper, "open_database", side_effect=lambda: real_open(self.ledger_path)))
            for name, value in (("PAPER_V12_ENABLED", True), ("SX_WS_V12_ENABLED", False),
                                ("POLY_WS_V12_ENABLED", False)):
                stack.enter_context(patch.object(monitor, name, value))
            stack.enter_context(patch.object(monitor, "PaperLifecycleWorker", PaperWorker))
            stack.enter_context(patch.object(monitor, "SnapshotRefresher", Background))
            stack.enter_context(patch.object(monitor, "AsyncNoticeSender", Background))
            stack.enter_context(patch.object(monitor, "telegram", side_effect=AssertionError("No network")))
            monitor.main()
        self.assertEqual(observed, [
            ("constructed", {"poly": 100000, "sx": 100000}, 0),
            ("started", {"poly": 100000, "sx": 100000}, 0),
        ])
        self.assertEqual(self.ledger.execute("SELECT status FROM paper_v12_trades").fetchone()[0],
                         "RESET_ARCHIVED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
