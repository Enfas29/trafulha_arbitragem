"""Reset tests use temporary SQLite files and synthetic trades only."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import paper_v12 as paper
import paper_reset_v14 as reset


def row_dicts(conn, table):
    cursor = conn.execute("SELECT * FROM " + table + " ORDER BY 1")
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper with spaces.sqlite3"
        self.conn = paper.open_database(self.path)
        self.addCleanup(self.conn.close)
        self.levels = ([(.4, 300)], [(.5, 300)])
        self.pairs = []
        for number in range(3):
            pair = {
                "key": f"reset-pair-{number}",
                "poly": {"id": f"poly-{number}", "type": "moneyline",
                         "sport": "basketball", "start": time.time() + 3600},
                "sx": {"hash": f"sx-{number}", "sport": "basketball",
                       "start": time.time() + 3600, "teams": ("Team A", "Team B")},
                "poly_asset": str(1000 + number), "poly_label": "Team A",
                "sx_target": 1, "sx_label": "Team B",
            }
            self.pairs.append(pair)
            opened = paper.try_paper_entry(self.conn, pair, *self.levels, .91, "PRE_JOGO", .01)
            self.assertIsNotNone(opened)
            if number == 1:
                paper.settle_normal(self.conn, opened[0], "poly", verified=True)
            elif number == 2:
                paper.flag_exception(self.conn, opened[0], "Synthetic review pending")
        self.original = self.snapshot()

    def snapshot(self):
        return {table: row_dicts(self.conn, table) for table in (
            "paper_v12_wallets", "paper_v12_trades",
            "paper_v12_open_notifications", "paper_v12_settlement_notifications",
            "paper_v12_notifications",
        )}

    def apply(self, request="reset-test-0001"):
        return reset.reset_virtual_bankroll(self.conn, request)

    def test_atomic_reset_preserves_backup_full_archive_and_pending_notices(self):
        before = paper.free_balances(self.conn)
        result = self.apply()
        self.assertTrue(result["applied"])
        self.assertEqual(result["balances_before_cents"], before)
        self.assertEqual(result["archived_trades"], 3)
        self.assertEqual(result["cancelled_open_trades"], 2)
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        stats = paper.paper_summary(self.conn)
        self.assertEqual((stats["open_trades"], stats["settled"], stats["requires_review"],
                          stats["exposure_eur"], stats["realized_eur"]), (0, 0, 0, 0, 0))
        self.assertEqual(self.conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        originals = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT original_row_json FROM paper_v14_reset_trades ORDER BY trade_id")]
        self.assertEqual(originals, self.original["paper_v12_trades"])
        self.assertEqual({row[0] for row in self.conn.execute("SELECT status FROM paper_v12_trades")},
                         {"RESET_ARCHIVED"})
        for table in ("paper_v12_open_notifications", "paper_v12_settlement_notifications",
                      "paper_v12_notifications"):
            self.assertEqual(row_dicts(self.conn, table), self.original[table])
        path = Path(result["backup_path"])
        self.assertIn("pre-reset", path.name)
        with sqlite3.connect(path) as saved:
            self.assertEqual(paper.free_balances(saved), before)
            self.assertEqual(row_dicts(saved, "paper_v12_trades"), self.original["paper_v12_trades"])
            self.assertEqual(saved.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_old_results_and_old_notifications_cannot_touch_the_new_bankroll(self):
        self.apply()
        with patch.object(paper, "send_telegram_notice") as sender:
            for ident in (1, 2, 3):
                self.assertFalse(paper.send_open_once(self.conn, ident, retry_seconds=0))
                with self.assertRaises(ValueError):
                    paper.settle_normal(self.conn, ident, "poly", verified=True)
            with self.assertRaises(ValueError):
                paper.send_settlement_once(self.conn, 2, retry_seconds=0)
            self.assertEqual(paper.retry_pending_open_notices(self.conn), 0)
            self.assertEqual(paper.retry_pending_settlement_notices(self.conn), 0)
            sender.assert_not_called()
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})

    def test_same_request_after_new_trade_or_gain_never_resets_again(self):
        self.apply()
        opened = paper.try_paper_entry(self.conn, self.pairs[0], *self.levels, .91, "PRE_JOGO", .01)
        self.assertIsNotNone(opened)  # Same market is allowed in the new cycle.
        self.assertGreater(opened[0], 3)  # Old IDs never reused.
        paper.settle_normal(self.conn, opened[0], "poly", verified=True)
        before = self.snapshot()
        with patch.object(reset, "_backup_locked_database", side_effect=AssertionError("must not back up twice")):
            repeated = self.apply()
        self.assertFalse(repeated["applied"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v14_resets").fetchone()[0], 1)

    def test_second_request_archives_only_the_new_cycle(self):
        self.apply()
        first_archived = row_dicts(self.conn, "paper_v12_trades")
        opened = paper.try_paper_entry(self.conn, self.pairs[0], *self.levels, .91, "PRE_JOGO", .01)
        self.assertIsNotNone(opened)
        result = self.apply("reset-test-0002")
        self.assertEqual(result["archived_trades"], 1)
        self.assertEqual(row_dicts(self.conn, "paper_v12_trades")[:3], first_archived)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v14_reset_trades").fetchone()[0], 4)
        with self.conn:
            self.conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents+100 WHERE platform='poly'")
        self.assertFalse(self.apply()["applied"])
        self.assertEqual(paper.free_balances(self.conn)["poly"], 100100)

    def test_backup_failure_never_changes_wallets_or_marks_request(self):
        with patch.object(reset, "_backup_locked_database", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.snapshot(), self.original)
        self.assertNotIn("paper_v14_resets", reset._tables(self.conn))
        self.assertFalse(self.conn.in_transaction)

    def test_archive_failure_rolls_back_every_wallet_trade_and_request(self):
        # A trigger aborts after the first row has already been archived.
        self.conn.execute("""CREATE TRIGGER reject_reset_row BEFORE UPDATE OF status ON paper_v12_trades
            WHEN NEW.id=2 AND NEW.status='RESET_ARCHIVED'
            BEGIN SELECT RAISE(ABORT, 'synthetic archive failure'); END""")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply()
        self.assertEqual(self.snapshot(), self.original)
        self.assertNotIn("paper_v14_resets", reset._tables(self.conn))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(len(list((self.path.parent / "backups").glob("*pre-reset*"))), 1)

    def test_second_wallet_failure_rolls_back_first_wallet_and_archive(self):
        self.conn.execute("""CREATE TRIGGER reject_wallet_reset BEFORE UPDATE OF free_cents ON paper_v12_wallets
            WHEN NEW.platform='sx' AND NEW.free_cents=100000
            BEGIN SELECT RAISE(ABORT, 'synthetic wallet failure'); END""")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply()
        self.assertEqual(self.snapshot(), self.original)
        self.assertNotIn("paper_v14_resets", reset._tables(self.conn))

    def test_concurrent_same_request_applies_exactly_once(self):
        barrier = threading.Barrier(2)
        def contender():
            db = paper.open_database(self.path)
            try:
                barrier.wait(timeout=2)
                return reset.reset_virtual_bankroll(db, "reset-race-0001")
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=5) for future in [pool.submit(contender), pool.submit(contender)]]
        self.assertEqual(sum(result["applied"] for result in results), 1)
        self.assertEqual(len(list((self.path.parent / "backups").glob("*pre-reset*"))), 1)
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v14_reset_trades").fetchone()[0], 3)

    def test_absent_request_does_nothing_and_invalid_request_cannot_start(self):
        self.assertIsNone(reset.apply_requested_reset(self.conn, environ={}))
        for invalid in (None, True, "", "short", "../bad-request", "has spaces", "a" * 97):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reset.reset_virtual_bankroll(self.conn, invalid)
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_request_on_restart_remains_once_only(self):
        self.apply()
        with self.conn:
            self.conn.execute("UPDATE paper_v12_wallets SET free_cents=99999 WHERE platform='sx'")
        connection = paper.open_database(self.path)
        try:
            result = reset.apply_requested_reset(connection, {"PAPER_RESET_REQUEST_ID": "reset-test-0001"})
            self.assertFalse(result["applied"])
            self.assertEqual(paper.free_balances(connection)["sx"], 99999)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
