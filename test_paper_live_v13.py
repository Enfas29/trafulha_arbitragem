"""Verified live PAPER entries: synthetic sources, clocks and SQLite only."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import paper_v12 as paper
from test_paper_freshness_v13 import AdvancingConnection
from test_paper_lifecycle_v13 import old_database


class LiveLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = paper.open_database(":memory:")
        self.addCleanup(self.conn.close)
        self.mono, self.epoch = 100.0, 1800000000.0
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(paper.time, "monotonic", side_effect=lambda: self.mono))
        self.patches.enter_context(patch.object(paper.time, "time", side_effect=lambda: self.epoch))
        self.patches.enter_context(patch.object(paper, "PAPER_LIVE_ENABLED", True))
        self.patches.enter_context(patch.object(paper, "ALLOW_LIVE_UNVERIFIED", True))
        self.pair = {
            "key": "poly-one:sx-one:0",
            "poly": {"id": "poly-one", "event_id": "poly-event-one",
                     "type": "moneyline", "sport": "basketball", "start": self.epoch - 60},
            "sx": {"hash": "0x" + "a" * 64, "event_id": "sx-event-one",
                   "sport": "basketball", "teams": ("Example A", "Example B"),
                   "start": self.epoch - 60, "live_enabled": True},
            "poly_asset": "asset-one", "sx_target": 1,
            "poly_label": "Example A", "sx_label": "Example B",
        }
        self.evidence = {
            "method": "official_live_v1", "pair_key": self.pair["key"],
            "poly_event_id": "poly-event-one", "poly_market_id": "poly-one",
            "poly_asset_id": "asset-one", "poly_side": "Example A",
            "sx_event_id": "sx-event-one", "sx_market_hash": self.pair["sx"]["hash"],
            "sx_target": 1, "sx_side": "Example B",
            "poly_live": True, "poly_ended": False, "poly_active": True,
            "poly_closed": False, "poly_accepting_orders": True,
            "sx_fixture_status": 2, "sx_status": "ACTIVE",
            "sx_live_enabled": True, "sx_reported": False,
            "candidate_started_mono": 95.0, "delay_started_mono": 96.0,
            "delay_ready_mono": 99.0, "checks_started_mono": 99.5,
            "checked_mono": 99.8, "checked_epoch": self.epoch - .2,
            "valid_until_mono": 102.5, "sx_delay_ms": 3000,
            "poly_delay_ms": 1000, "required_delay_ms": 3000,
            "initial_required_delay_ms": 3000, "verified_after_delay": True,
            "score": "30-28", "period": "2.º período",
        }
        self.levels = ([(.4, 300)], [(.5, 300)])
        self.quote = paper.select_shadow_quote(*self.levels, .91, 100000, 100000)
        self.assertIsNotNone(self.quote)

    def enter(self, evidence=None, deadline=102.0, conn=None, stage="LIVE_CONFIRMADO"):
        return paper.try_paper_entry(
            self.conn if conn is None else conn, self.pair, *self.levels,
            .91, stage, .01, valid_until_mono=deadline,
            live_evidence=self.evidence if evidence is None else evidence,
        )

    def assert_untouched(self):
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_valid_live_entry_persists_proof_and_friendly_notice_atomically(self):
        opened = self.enter()
        self.assertIsNotNone(opened)
        ident, quote = opened
        row = paper.trade_details(self.conn, ident)
        self.assertEqual(row["stage"], "LIVE_CONFIRMADO")
        self.assertEqual(json.loads(row["entry_evidence_json"]), self.evidence)
        self.assertEqual(paper.free_balances(self.conn), {
            "poly": 100000 - quote.poly_debit_cents, "sx": 100000 - quote.sx_debit_cents,
        })
        message, sent = self.conn.execute(
            "SELECT message,sent_epoch FROM paper_v12_open_notifications WHERE trade_id=?", (ident,)
        ).fetchone()
        self.assertIsNone(sent)
        for expected in ("EM DIRETO confirmado", "30-28", "2.º período",
                         "Polymarket 1,00 s", "SX Bet 3,00 s", "Hipótese após revalidação",
                         "não execução real", "Início previsto:", "Carteira Polymarket:",
                         "Carteira SX Bet:", "APOSTA VIRTUAL"):
            self.assertIn(expected, message)

    def test_live_requires_evidence_and_price_deadline_even_for_direct_entry(self):
        for proof, deadline in ((None, None), (None, 102), ({}, 102), (self.evidence, None)):
            with self.subTest(proof=proof is not None, deadline=deadline):
                self.assertIsNone(paper.book_shadow_trade(
                    self.conn, self.pair, self.quote, "LIVE_CONFIRMADO",
                    live_evidence=proof, valid_until_mono=deadline,
                ))
                self.assert_untouched()

    def test_unverified_or_unknown_live_stage_cannot_bypass_with_old_flag(self):
        self.assertTrue(paper.ALLOW_LIVE_UNVERIFIED)
        for stage in ("LIVE_NAO_CONFIRMADO", "LIVE", "LIVE_UNKNOWN", "APOS_INICIO_NAO_CONFIRMADO"):
            with self.subTest(stage=stage):
                self.assertIsNone(self.enter(stage=stage))
                self.assertIsNone(paper.book_shadow_trade(self.conn, self.pair, self.quote, stage))
                self.assert_untouched()

    def test_disabled_live_feature_blocks_an_otherwise_valid_proof(self):
        with patch.object(paper, "PAPER_LIVE_ENABLED", False):
            self.assertIsNone(self.enter())
        self.assert_untouched()

    def test_every_identity_and_selected_side_must_match_the_pair(self):
        fields = ("pair_key", "poly_event_id", "poly_market_id", "poly_asset_id",
                  "poly_side", "sx_event_id", "sx_market_hash", "sx_side")
        for field in fields:
            for value in (None, "other", True, 1):
                with self.subTest(field=field, value=value):
                    proof = {**self.evidence, field: value}
                    self.assertIsNone(self.enter(proof))
                    self.assert_untouched()
        for side in (0, True, "1", 1.0, None, 2):
            with self.subTest(side=side):
                self.assertIsNone(self.enter({**self.evidence, "sx_target": side}))
                self.assert_untouched()

    def test_missing_pair_event_identity_cannot_reuse_another_proof(self):
        del self.pair["poly"]["event_id"]
        self.assertIsNone(self.enter())
        self.assert_untouched()

    def test_boolean_flags_are_strict_and_suspended_or_reported_markets_are_rejected(self):
        for field in ("poly_live", "poly_active", "poly_accepting_orders",
                      "sx_live_enabled", "verified_after_delay"):
            for value in (False, 1, "true", "false", None):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(self.enter({**self.evidence, field: value}))
                    self.assert_untouched()
        for field in ("poly_ended", "poly_closed", "sx_reported"):
            for value in (True, 0, "false", None):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(self.enter({**self.evidence, field: value}))
                    self.assert_untouched()
        for status in (True, 2.0, "2", 0, 1, 3, 4, None):
            with self.subTest(fixture_status=status):
                self.assertIsNone(self.enter({**self.evidence, "sx_fixture_status": status}))
        for status in (None, "INACTIVE", "SUSPENDED", "CANCELLED", "active"):
            with self.subTest(market_status=status):
                self.assertIsNone(self.enter({**self.evidence, "sx_status": status}))
        self.assert_untouched()

    def test_delay_values_and_completed_delay_must_be_consistent(self):
        for field in ("sx_delay_ms", "poly_delay_ms", "required_delay_ms", "initial_required_delay_ms"):
            for value in (None, True, -1, 1.0, "3000"):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(self.enter({**self.evidence, field: value}))
                    self.assert_untouched()
        changes = [
            {"required_delay_ms": 1000},
            {"initial_required_delay_ms": 4000},
            {"delay_started_mono": 96.01},
            {"sx_delay_ms": 4000, "required_delay_ms": 4000},
            {"verified_after_delay": False},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.enter({**self.evidence, **change}))
                self.assert_untouched()

    def test_unknown_method_nonfinite_times_future_checks_and_expired_deadlines_are_rejected(self):
        self.assertIsNone(self.enter({**self.evidence, "method": "assumed_live"}))
        fields = ("candidate_started_mono", "delay_started_mono", "delay_ready_mono",
                  "checks_started_mono", "checked_mono", "checked_epoch", "valid_until_mono")
        for field in fields:
            for value in (None, True, "99.5", float("nan"), float("inf")):
                with self.subTest(field=field, value=str(value)):
                    self.assertIsNone(self.enter({**self.evidence, field: value}))
                    self.assert_untouched()
        changes = [
            {"candidate_started_mono": 97.0}, {"delay_ready_mono": 95.0},
            {"checks_started_mono": 98.9, "valid_until_mono": 101.9},
            {"checked_mono": 99.4}, {"checked_mono": 100.1},
            {"checked_epoch": self.epoch + .01}, {"checked_epoch": self.epoch - 5},
            {"valid_until_mono": 103.0},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.enter({**self.evidence, **change}))
                self.assert_untouched()
        for deadline in (None, True, "102", float("nan"), float("inf"), 99.9, 102.6):
            with self.subTest(deadline=str(deadline)):
                self.assertIsNone(self.enter(deadline=deadline))
                self.assert_untouched()

    def test_evidence_size_and_json_content_are_bounded(self):
        for value in ("€" * 4000, object(), float("nan")):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(self.enter({**self.evidence, "extra": value}))
                self.assert_untouched()

    def test_expiry_after_sqlite_lock_rolls_back_everything(self):
        def lock_delay():
            self.mono = 103.0
            self.epoch += 3
        proxy = AdvancingConnection(self.conn, after_begin=lock_delay)
        self.assertIsNone(self.enter(conn=proxy))
        self.assert_untouched()

    def test_expiry_after_outbox_insert_rolls_back_wallets_trade_and_proof(self):
        def after_insert():
            self.assertNotEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
            proof = self.conn.execute("SELECT entry_evidence_json FROM paper_v12_trades").fetchone()[0]
            self.assertEqual(json.loads(proof), self.evidence)
            self.mono = 103.0
            self.epoch += 3
        proxy = AdvancingConnection(self.conn, after_outbox=after_insert)
        self.assertIsNone(self.enter(conn=proxy))
        self.assert_untouched()

    def test_proof_is_frozen_before_waiting_for_lock(self):
        expected = copy.deepcopy(self.evidence)
        def caller_mutates_evidence():
            self.evidence["score"] = "do not rewrite the verified snapshot"
        proxy = AdvancingConnection(self.conn, after_begin=caller_mutates_evidence)
        opened = self.enter(conn=proxy)
        self.assertIsNotNone(opened)
        actual = json.loads(paper.trade_details(self.conn, opened[0])["entry_evidence_json"])
        self.assertEqual(actual, expected)

    def test_live_duplicate_does_not_repeat_debits_or_notices(self):
        opened = self.enter()
        self.assertIsNotNone(opened)
        balances = paper.free_balances(self.conn)
        self.assertIsNone(self.enter())
        self.assertEqual(paper.free_balances(self.conn), balances)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)

    def test_migration_v13_to_v14_preserves_history_and_creates_backup_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            conn = old_database(path)
            for name, kind in paper.TRADE_COLUMNS_V13.items():
                if name != "entry_evidence_json":
                    conn.execute(f"ALTER TABLE paper_v12_trades ADD COLUMN {name} {kind}")
            conn.execute("CREATE TABLE paper_v13_migrations(version INTEGER PRIMARY KEY,applied_epoch INTEGER NOT NULL,backup_path TEXT)")
            conn.execute("INSERT INTO paper_v13_migrations VALUES(13,1700000000,NULL)")
            conn.execute("UPDATE paper_v12_trades SET stage='LIVE_NAO_CONFIRMADO' WHERE id=7")
            conn.commit()
            conn.close()
            migrated = paper.open_database(path)
            try:
                self.assertEqual(paper.free_balances(migrated), {"poly": 54321, "sx": 67890})
                row = paper.trade_details(migrated, 7)
                self.assertEqual(row["stage"], "LIVE_NAO_CONFIRMADO")
                self.assertEqual(row["note"], "historical note to preserve")
                self.assertIsNone(row["entry_evidence_json"])
                self.assertEqual(migrated.execute("SELECT message FROM paper_v12_open_notifications WHERE trade_id=7").fetchone()[0], "original notification")
                self.assertEqual([tuple(row) for row in migrated.execute("SELECT version FROM paper_v13_migrations ORDER BY version")], [(13,), (14,)])
            finally:
                migrated.close()
            backups = list((Path(directory) / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertIn("-pre-v14-", backups[0].name)
            with sqlite3.connect(backups[0]) as previous:
                self.assertNotIn("entry_evidence_json", {r[1] for r in previous.execute("PRAGMA table_info(paper_v12_trades)")})
                self.assertEqual(previous.execute("SELECT stage FROM paper_v12_trades WHERE id=7").fetchone()[0], "LIVE_NAO_CONFIRMADO")
            paper.open_database(path).close()
            self.assertEqual(list((Path(directory) / "backups").glob("*.sqlite3")), backups)


if __name__ == "__main__":
    unittest.main()
