"""Migração e ciclo virtual: fixtures sintéticas, sem rede ou Telegram real."""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import paper_v12 as paper


LEGACY_SCHEMA = """
CREATE TABLE paper_v12_wallets(platform TEXT PRIMARY KEY, free_cents INTEGER NOT NULL);
CREATE TABLE paper_v12_notifications(trade_id INTEGER PRIMARY KEY,sent_epoch INTEGER NOT NULL);
CREATE TABLE paper_v12_open_notifications(
 trade_id INTEGER PRIMARY KEY,message TEXT NOT NULL,sent_epoch INTEGER,
 attempts INTEGER NOT NULL DEFAULT 0,last_attempt_epoch INTEGER);
CREATE TABLE paper_v12_trades(
 id INTEGER PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,created_epoch INTEGER NOT NULL,
 sport TEXT NOT NULL,event TEXT NOT NULL,poly_market TEXT NOT NULL,sx_market TEXT NOT NULL,
 poly_side TEXT NOT NULL,sx_side TEXT NOT NULL,stage TEXT NOT NULL,payout_usdc REAL NOT NULL,
 fx_eur_per_usd REAL NOT NULL,poly_debit_cents INTEGER NOT NULL,sx_debit_cents INTEGER NOT NULL,
 poly_fee_cents INTEGER NOT NULL,conditional_payout_cents INTEGER NOT NULL,
 conditional_profit_cents INTEGER NOT NULL,status TEXT NOT NULL,settled_side TEXT,
 realized_profit_cents INTEGER,note TEXT NOT NULL);
CREATE TABLE historical_sentinel(value TEXT);
INSERT INTO paper_v12_wallets VALUES('poly',54321),('sx',67890);
INSERT INTO paper_v12_trades VALUES(
 7,'legacy-event',1700000000,'basketball','Example A vs Example B','legacy-poly','legacy-sx',
 'Example A','Example B','PRE_JOGO',100,0.9,4000,4500,50,9000,500,
 'OPEN_CONDITIONAL',NULL,NULL,'historical note to preserve');
INSERT INTO paper_v12_open_notifications VALUES(7,'original notification',1700000001,1,1700000001);
INSERT INTO paper_v12_notifications VALUES(99,1700000002);
INSERT INTO historical_sentinel VALUES('keep every historical row');
"""


def old_database(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    return conn


def new_trade(conn, name="one", open_notice=False, game_start=None):
    start = game_start or int(time.time()) + 3600
    pair = {
        "poly": {"id": f"poly-{name}", "type": "moneyline", "sport": "basketball", "start": start},
        "sx": {"hash": f"sx-{name}", "sport": "basketball", "teams": ("Example A", "Example B"), "start": start},
        "poly_asset": f"token-{name}", "sx_target": 1,
        "poly_label": "Example A", "sx_label": "Example B",
    }
    balances = paper.free_balances(conn)
    quote = paper.select_shadow_quote(
        [(0.4, 300)], [(0.5, 300)], 0.91, balances["poly"], balances["sx"]
    )
    assert quote is not None
    trade_id = paper.book_shadow_trade(
        conn, pair, quote, "PRE_JOGO", now_epoch=start - 3600,
        opening_odds=(2.5, 2.0) if open_notice else None,
    )
    assert trade_id is not None
    return trade_id


def evidence_for(conn, trade_id, poly_won=True, sx_won=False):
    row = paper.trade_details(conn, trade_id)
    return {
        "method": "official_api",
        "poly": {
            "source_url": "https://data-api.polymarket.com/v2/tokens",
            "resolved": True, "resolution_status": "onchain_payout",
            "market_id": row["poly_market"], "token_id": row["poly_token_id"],
            "won": poly_won,
        },
        "sx": {
            "source_url": "https://api.sx.bet/markets/find",
            "resolved": True, "resolution_status": "reported",
            "market_hash": row["sx_market"], "outcome_one": bool(row["sx_outcome_one"]),
            "won": sx_won,
        },
    }


class PaperLifecycleTests(unittest.TestCase):
    def test_migration_keeps_history_and_consistent_wal_backup_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            writer = old_database(path)  # WAL fica aberto e não é copiado cegamente.
            try:
                with self.assertLogs(paper.LOG, level="INFO") as logged:
                    conn = paper.open_database(path)
                self.assertIn("carteiras_preservadas=true", logged.output[0])
                self.assertIn("operações_preservadas=1", logged.output[0])
                self.assertIn("backup=", logged.output[0])
                try:
                    self.assertEqual(paper.free_balances(conn), {"poly": 54321, "sx": 67890})
                    trade = paper.trade_details(conn, 7)
                    self.assertEqual(trade["note"], "historical note to preserve")
                    self.assertIsNone(trade["poly_token_id"])
                    self.assertIsNone(trade["sx_outcome_one"])
                    self.assertEqual(conn.execute("SELECT value FROM historical_sentinel").fetchone()[0], "keep every historical row")
                    self.assertEqual(conn.execute("SELECT message FROM paper_v12_open_notifications").fetchone()[0], "original notification")
                    self.assertEqual(conn.execute("SELECT trade_id FROM paper_v12_notifications").fetchone()[0], 99)
                finally:
                    conn.close()
                backups = list((Path(directory) / "backups").glob("*.sqlite3"))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as backup:
                    self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(dict(backup.execute("SELECT * FROM paper_v12_wallets")), {"poly": 54321, "sx": 67890})
                    self.assertNotIn("poly_token_id", {r[1] for r in backup.execute("PRAGMA table_info(paper_v12_trades)")})
                    self.assertEqual(backup.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
                with patch.object(paper.LOG, "info") as logger:
                    paper.open_database(path).close()
                    logger.assert_not_called()
                self.assertEqual(list((Path(directory) / "backups").glob("*.sqlite3")), backups)
            finally:
                writer.close()

    def test_migration_rolls_back_if_wallet_invariant_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            legacy = old_database(path)
            legacy.execute(
                "CREATE TRIGGER migration_fault BEFORE INSERT ON paper_v12_wallets "
                "BEGIN UPDATE paper_v12_wallets SET free_cents=free_cents+1 WHERE platform='poly'; END"
            )
            legacy.commit()
            legacy.close()
            with self.assertRaisesRegex(RuntimeError, "Migração PAPER revertida"):
                paper.open_database(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(dict(conn.execute("SELECT * FROM paper_v12_wallets")), {"poly": 54321, "sx": 67890})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
                self.assertNotIn("poly_token_id", {r[1] for r in conn.execute("PRAGMA table_info(paper_v12_trades)")})

    def test_low_disk_blocks_migration_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            old_database(path).close()
            usage = namedtuple("Usage", "total used free")(500 * 1048576, 492 * 1048576, 8 * 1048576)
            with patch.object(paper.shutil, "disk_usage", return_value=usage):
                with self.assertRaisesRegex(RuntimeError, "espaço insuficiente"):
                    paper.open_database(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(dict(conn.execute("SELECT * FROM paper_v12_wallets")), {"poly": 54321, "sx": 67890})
                self.assertNotIn("poly_token_id", {r[1] for r in conn.execute("PRAGMA table_info(paper_v12_trades)")})
            self.assertFalse((Path(directory) / "backups").exists())

    def test_new_trade_keeps_identity_and_portugal_times(self):
        conn = paper.open_database(":memory:")
        try:
            epoch = int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp())
            ident = new_trade(conn, open_notice=True, game_start=epoch)
            row = paper.trade_details(conn, ident)
            self.assertEqual((row["game_start_epoch"], row["poly_token_id"], row["sx_outcome_one"]), (epoch, "token-one", 0))
            opening = conn.execute("SELECT message FROM paper_v12_open_notifications").fetchone()[0]
            self.assertIn("01/07/2026 13:00 (Portugal)", opening)
            paper.settle_verified_outcomes(conn, ident, True, False, evidence_for(conn, ident), now_epoch=epoch + 7200)
            closing = paper.format_settlement_message(conn, ident)
            self.assertIn("01/07/2026 15:00 (Portugal)", closing)
            self.assertIn("01/07/2026 13:00 (Portugal)", closing)
        finally:
            conn.close()

    def test_independent_outcomes_losses_and_idempotency(self):
        for poly_won, sx_won in ((True, False), (False, True), (False, False), (True, True)):
            with self.subTest(poly_won=poly_won, sx_won=sx_won):
                conn = paper.open_database(":memory:")
                try:
                    ident = new_trade(conn)
                    before = paper.free_balances(conn)
                    row = paper.trade_details(conn, ident)
                    payout = row["conditional_payout_cents"]
                    total = row["poly_debit_cents"] + row["sx_debit_cents"]
                    evidence = evidence_for(conn, ident, poly_won, sx_won)
                    result = paper.settle_verified_outcomes(conn, ident, poly_won, sx_won, evidence)
                    expected = (payout * (int(poly_won) + int(sx_won)) - total) / 100
                    self.assertAlmostEqual(result, expected)
                    after = {"poly": before["poly"] + payout * poly_won, "sx": before["sx"] + payout * sx_won}
                    self.assertEqual(paper.free_balances(conn), after)
                    self.assertIsNone(paper.settle_verified_outcomes(conn, ident, poly_won, sx_won, evidence))
                    self.assertEqual(paper.free_balances(conn), after)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_settlement_notifications").fetchone()[0], 1)
                    stored = paper.trade_details(conn, ident)
                    self.assertEqual(json.loads(stored["resolution_evidence_json"]), evidence)
                    with self.assertRaisesRegex(ValueError, "contradizem"):
                        paper.settle_verified_outcomes(conn, ident, not poly_won, sx_won, evidence)
                    self.assertEqual(paper.free_balances(conn), after)
                finally:
                    conn.close()

    def test_invalid_source_and_unresolved_results_cannot_credit(self):
        conn = paper.open_database(":memory:")
        try:
            ident = new_trade(conn)
            before = paper.free_balances(conn)
            for side, key, value in (
                ("poly", "source_url", "https://example.invalid/result"),
                ("poly", "resolved", False),
                ("poly", "token_id", "different-token"),
                ("sx", "market_hash", "different-market"),
                ("sx", "outcome_one", True),
                ("sx", "won", True),
            ):
                evidence = evidence_for(conn, ident)
                evidence[side][key] = value
                with self.assertRaises(ValueError):
                    paper.settle_verified_outcomes(conn, ident, True, False, evidence)
                self.assertEqual(paper.free_balances(conn), before)
                self.assertEqual(paper.trade_details(conn, ident)["status"], "OPEN_CONDITIONAL")
            with self.assertRaises(ValueError):
                paper.settle_verified_outcomes(conn, ident, 1, False, evidence_for(conn, ident))
            with self.assertRaises(ValueError):
                paper.settle_verified_outcomes(conn, ident, True, False, {"method": "manual_verified", "verified_both_rules": True})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_settlement_notifications").fetchone()[0], 0)
        finally:
            conn.close()

    def test_review_and_unusual_old_margin_require_manual_handling(self):
        conn = paper.open_database(":memory:")
        try:
            first = new_trade(conn, "review")
            paper.flag_exception(conn, first, "Official outcome is ambiguous")
            with self.assertRaises(ValueError):
                paper.settle_verified_outcomes(conn, first, True, False, evidence_for(conn, first))
            second = new_trade(conn, "old-margin")
            with conn:
                conn.execute("UPDATE paper_v12_trades SET conditional_profit_cents=5000 WHERE id=?", (second,))
            with self.assertRaisesRegex(ValueError, "margem histórica"):
                paper.settle_verified_outcomes(conn, second, True, False, evidence_for(conn, second))
        finally:
            conn.close()

    def test_outbox_failure_rolls_back_credit_and_result(self):
        conn = paper.open_database(":memory:")
        try:
            ident = new_trade(conn)
            before = paper.free_balances(conn)
            conn.execute("CREATE TRIGGER reject_close BEFORE INSERT ON paper_v12_settlement_notifications BEGIN SELECT RAISE(ABORT, 'simulated outbox failure'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                paper.settle_verified_outcomes(conn, ident, True, False, evidence_for(conn, ident))
            self.assertEqual(paper.free_balances(conn), before)
            self.assertEqual(paper.trade_details(conn, ident)["status"], "OPEN_CONDITIONAL")
            self.assertIsNone(paper.trade_details(conn, ident)["resolution_evidence_json"])
        finally:
            conn.close()

    def test_failed_delivery_retries_identical_message_without_credit(self):
        conn = paper.open_database(":memory:")
        try:
            ident = new_trade(conn)
            paper.settle_verified_outcomes(conn, ident, True, False, evidence_for(conn, ident))
            frozen = paper.format_settlement_message(conn, ident)
            messages = []
            def sender(message):
                messages.append(message)
                return len(messages) > 1
            with patch.object(paper, "send_telegram_notice", side_effect=sender):
                self.assertFalse(paper.send_settlement_once(conn, ident, now_epoch=1000))
                with conn:
                    conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents+777 WHERE platform='poly'")
                current = paper.free_balances(conn)
                self.assertEqual(paper.retry_pending_settlement_notices(conn, now_epoch=1030), 0)
                self.assertEqual(paper.retry_pending_settlement_notices(conn, now_epoch=1061), 1)
                self.assertEqual(messages, [frozen, frozen])
                self.assertEqual(paper.free_balances(conn), current)
                self.assertTrue(paper.send_settlement_once(conn, ident, now_epoch=1122))
                self.assertEqual(paper.retry_pending_settlement_notices(conn, now_epoch=1122), 0)
                self.assertEqual(len(messages), 2)
        finally:
            conn.close()

    def test_pending_opening_still_delivered_after_settlement(self):
        conn = paper.open_database(":memory:")
        try:
            ident = new_trade(conn, open_notice=True)
            paper.settle_verified_outcomes(conn, ident, True, False, evidence_for(conn, ident))
            balances = paper.free_balances(conn)
            messages = []
            def sender(text):
                messages.append(text)
                return len(messages) > 1  # a primeira tentativa de abertura falha
            with patch.object(paper, "send_telegram_notice", side_effect=sender):
                self.assertFalse(paper.send_settlement_once(conn, ident))
                self.assertFalse(paper.send_open_once(conn, ident, now_epoch=1000))
                self.assertFalse(paper.send_settlement_once(conn, ident))
                self.assertEqual(len(messages), 1)
                self.assertEqual(paper.retry_pending_open_notices(conn), 1)
                self.assertEqual(paper.retry_pending_settlement_notices(conn), 1)
                self.assertIn("APOSTA VIRTUAL", messages[0])
                self.assertEqual(messages[0], messages[1])
                self.assertIn("RESULTADO VIRTUAL CONFIRMADO", messages[2])
                self.assertEqual(paper.free_balances(conn), balances)
        finally:
            conn.close()

    def test_two_connections_cannot_credit_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            conn = paper.open_database(path)
            ident = new_trade(conn)
            evidence = evidence_for(conn, ident)
            before = paper.free_balances(conn)
            payout = paper.trade_details(conn, ident)["conditional_payout_cents"]
            conn.close()
            barrier = threading.Barrier(2, timeout=5)
            def worker():
                ledger = paper.open_database(path)
                try:
                    barrier.wait()
                    return paper.settle_verified_outcomes(ledger, ident, True, False, evidence)
                finally:
                    ledger.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(worker) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(sum(value is None for value in results), 1)
            conn = paper.open_database(path)
            try:
                self.assertEqual(paper.free_balances(conn), {"poly": before["poly"] + payout, "sx": before["sx"]})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_settlement_notifications").fetchone()[0], 1)
                self.assertFalse((Path(directory) / "backups").exists())
            finally:
                conn.close()

    def test_summary_stays_consistent_during_concurrent_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            reader = paper.open_database(path)
            reader.execute("PRAGMA journal_mode=WAL")
            ident = new_trade(reader)
            writer = paper.open_database(path)
            try:
                evidence = evidence_for(writer, ident)
                before = paper.paper_summary(reader)

                class ReaderWithConcurrentWriter:
                    triggered = False

                    def execute(self, sql, args=()):
                        cursor = reader.execute(sql, args)
                        if not self.triggered:
                            self.triggered = True
                            # Commit depois de a leitura começar: o resumo
                            # tem de refletir integralmente um só instante.
                            paper.settle_verified_outcomes(writer, ident, True, False, evidence)
                        return cursor

                observed = paper.paper_summary(ReaderWithConcurrentWriter())
                self.assertEqual(observed, before)
                after = paper.paper_summary(reader)
                self.assertEqual(after["settled"], 1)
                self.assertEqual(after["open_trades"], 0)
                self.assertNotEqual(after, before)

                reader.execute("BEGIN")
                reader.execute("UPDATE paper_v12_wallets SET free_cents=free_cents+1 WHERE platform='poly'")
                self.assertAlmostEqual(paper.paper_summary(reader)["poly_free_eur"], after["poly_free_eur"] + 0.01)
                self.assertTrue(reader.in_transaction)
                reader.rollback()
                self.assertEqual(paper.paper_summary(reader), after)
            finally:
                reader.close()
                writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
