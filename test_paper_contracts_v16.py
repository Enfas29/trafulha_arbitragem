"""New sports share atomic wallets, immutable contracts and verified results."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import market_contracts_v16 as contracts
import monitor_v11 as monitor
import paper_reset_v14 as reset
import paper_v12 as paper
from market_costs_v15 import FeeContext
from test_market_contracts_v16 import build_soccer_fixture, build_hockey_fixture
from test_live_candidates_v13 import proof
from test_paper_lifecycle_v13 import evidence_for, new_trade


def alternative_proposition(now):
    """A different binary market for the same soccer fixture (draw vs no draw)."""
    pair, rp, rs = build_soccer_fixture(now, predicate="draw")
    rp["id"] = "7202038"
    rs["marketHash"] = "0x" + "44" * 32
    p, s = pair["poly"], pair["sx"]
    p["id"], s["hash"] = rp["id"], rs["marketHash"]
    match = lambda a, b: a.casefold() == b.casefold()
    p["contract"] = contracts.classify_poly_market(
        rp, {"id": p["event_id"], "title": p["title"]}, p["sport"], p["teams"], match)
    s["contract"] = contracts.classify_sx_market(rs, s["sport"], s["teams"], match)
    pair["settlement_contract"] = contracts.make_pair_contract(p, s, 1, 0, {0: 0, 1: 1})
    pair["key"] = f'{p["id"]}:{s["hash"]}:1:0'
    assert contracts.validate_pair_contract(pair)
    return pair


class PaperContractTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.conn = paper.open_database(":memory:")
        self.addCleanup(self.conn.close)

    def quote(self, pair):
        now = time.time()
        fees = FeeContext(str(pair["poly"]["id"]), .03, 1, .01, now, now + 900)
        value = paper.quote_for_payout([(.4, 1000)], [(.5, 1000)], 100, 1, fee_context=fees)
        self.assertIsNotNone(value)
        return value

    def book(self, pair, conn=None):
        return paper.book_shadow_trade(conn or self.conn, pair, self.quote(pair), "PRE_JOGO",
                                       opening_odds=(2.5, 2.0))

    def evidence(self, ident, poly_won=True, sx_won=False, conn=None):
        conn = conn or self.conn
        value = evidence_for(conn, ident, poly_won, sx_won)
        proof = json.loads(paper.trade_details(conn, ident)["contract_evidence_json"])
        value.update(contract_digest=proof["digest"], fixture={
            "source_url": "https://api.sx.bet/fixture/status", "status": 3,
            "event_id": proof["sx"]["event_id"], "observed_epoch": time.time(),
        })
        return value

    def test_each_sport_books_atomic_sides_and_frozen_rule_evidence(self):
        for build in (build_soccer_fixture, build_hockey_fixture):
            pair, _, _ = build(self.now)
            before = paper.free_balances(self.conn)
            ident = self.book(pair)
            self.assertIsNotNone(ident)
            row = paper.trade_details(self.conn, ident)
            self.assertTrue(contracts.validate_record_contract(row))
            self.assertEqual(json.loads(row["contract_evidence_json"]), pair["settlement_contract"])
            self.assertEqual(row["poly_side"], pair["poly_label"])
            self.assertEqual(row["sx_side"], pair["sx_label"])
            after = paper.free_balances(self.conn)
            self.assertEqual(before["poly"] - after["poly"], row["poly_debit_cents"])
            self.assertEqual(before["sx"] - after["sx"], row["sx_debit_cents"])

    def test_missing_or_modified_proof_never_debits_or_creates_notice(self):
        base, _, _ = build_soccer_fixture(self.now)
        for mutate in (lambda p: p.pop("settlement_contract"),
                       lambda p: p["settlement_contract"].update(scope="hockey_full_game_ot_so"),
                       lambda p: p.update(poly_asset="999"),
                       lambda p: p["sx"].update(event_id="a-different-game")):
            with self.subTest(mutate=mutate):
                pair = copy.deepcopy(base)
                mutate(pair)
                self.assertIsNone(self.book(pair))
                self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)

    def test_multiple_propositions_share_one_fixture_budget(self):
        first, _, _ = build_soccer_fixture(self.now)
        second = alternative_proposition(self.now)
        self.assertNotEqual(paper.event_key(first), paper.event_key(second))
        self.assertIsNotNone(self.book(first))
        before = paper.free_balances(self.conn)
        self.assertIsNone(self.book(second))
        self.assertEqual(paper.free_balances(self.conn), before)
        audit = {}
        opened, reason = monitor.attempt_paper_entry(
            second, [(.4, 1000)], [(.5, 1000)], time.monotonic(), time.monotonic(),
            1, self.conn, "PRE_JOGO", fee_context=self.quote(second).fee_context, audit=audit)
        self.assertIsNone(opened)
        self.assertEqual(reason, "jogo_ja_registado")

    def test_simultaneous_different_propositions_cannot_both_debit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            paper.open_database(path).close()
            pairs = (build_soccer_fixture(self.now)[0], alternative_proposition(self.now))
            quotes = [self.quote(p) for p in pairs]
            barrier = threading.Barrier(2)
            def work(index):
                conn = paper.open_database(path)
                try:
                    barrier.wait(timeout=10)
                    return paper.book_shadow_trade(conn, pairs[index], quotes[index], "PRE_JOGO",
                                                   opening_odds=(2.5, 2))
                finally:
                    conn.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(work, (0, 1)))
            self.assertEqual(sum(result is not None for result in results), 1)
            conn = paper.open_database(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)
                balances = paper.free_balances(conn)
                self.assertEqual(balances["poly"], 100000 - quotes[0].poly_debit_cents)
                self.assertEqual(balances["sx"], 100000 - quotes[0].sx_debit_cents)
            finally:
                conn.close()

    def test_not_team_notice_includes_draw_and_conditional_rules(self):
        ident = self.book(build_soccer_fixture(self.now)[0])
        text = paper.format_open_message(self.conn, ident)
        self.assertIn("Manchester United FC não vence (inclui empate) [No]", text)
        self.assertIn("90 minutos e compensação", text)
        self.assertIn("Margem condicional", text)
        self.assertIn("exigem revisão", text)
        self.assertNotIn("Equipa: No", text)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM paper_v12_open_notifications WHERE trade_id=?", (ident,)).fetchone()[0], 1)

    def test_hockey_notice_and_settlement_use_explicit_selections(self):
        ident = self.book(build_hockey_fixture(self.now)[0])
        self.assertIn("incluindo prolongamento e desempate", paper.format_open_message(self.conn, ident))
        gain = paper.settle_verified_outcomes(self.conn, ident, False, True,
                                             self.evidence(ident, False, True))
        self.assertGreater(gain, 0)
        self.assertIn("Blue Jackets vence", paper.format_settlement_message(self.conn, ident))
        before = paper.free_balances(self.conn)
        self.assertIsNone(paper.settle_verified_outcomes(self.conn, ident, False, True,
                                                        self.evidence(ident, False, True)))
        self.assertEqual(paper.free_balances(self.conn), before)

    def test_missing_wrong_or_unfinished_fixture_cannot_credit(self):
        ident = self.book(build_soccer_fixture(self.now)[0])
        original = self.evidence(ident)
        mutations = (
            lambda e: e.pop("fixture"), lambda e: e.pop("contract_digest"),
            lambda e: e["fixture"].update(status=7),
            lambda e: e["fixture"].update(status="3"),
            lambda e: e["fixture"].update(status=True),
            lambda e: e["fixture"].update(event_id="other-game"),
            lambda e: e["fixture"].update(source_url="https://example.org/fixture/status"),
        )
        before = paper.free_balances(self.conn)
        for mutate in mutations:
            evidence = copy.deepcopy(original)
            mutate(evidence)
            with self.assertRaises(ValueError):
                paper.settle_verified_outcomes(self.conn, ident, True, False, evidence)
            self.assertEqual(paper.free_balances(self.conn), before)
            self.assertEqual(paper.trade_details(self.conn, ident)["status"], "OPEN_CONDITIONAL")

    def test_same_result_on_both_legs_requires_review_without_credit(self):
        ident = self.book(build_soccer_fixture(self.now)[0])
        before = paper.free_balances(self.conn)
        for won in (True, False):
            with self.assertRaises(ValueError):
                paper.settle_verified_outcomes(self.conn, ident, won, won,
                                               self.evidence(ident, won, won))
            self.assertEqual(paper.free_balances(self.conn), before)

    def test_live_ledger_rejects_unknown_period_even_with_otherwise_valid_proof(self):
        pair = build_soccer_fixture(self.now - 7200)[0]
        evidence = proof(pair, 100.0, delay=0)
        evidence.update(
            checked_epoch=self.now, candidate_started_mono=99.0,
            delay_started_mono=99.0, delay_ready_mono=99.0,
            initial_required_delay_ms=0, verified_after_delay=True,
            contract_digest=pair["settlement_contract"]["digest"],
        )
        with patch.object(paper.time, "monotonic", return_value=100.0), \
             patch.object(paper.time, "time", return_value=self.now), \
             patch.object(paper, "PAPER_LIVE_ENABLED", True):
            quote = self.quote(pair)
            for period in (None, "Unknown", "ET", "90+4"):
                self.assertIsNone(paper.book_shadow_trade(
                    self.conn, pair, quote, "LIVE_CONFIRMADO", valid_until_mono=102.0,
                    live_evidence={**evidence, "period": period}))
                self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
            self.assertIsNotNone(paper.book_shadow_trade(
                self.conn, pair, quote, "LIVE_CONFIRMADO", valid_until_mono=102.0,
                live_evidence={**evidence, "period": "2H"}, opening_odds=(2.5, 2)))

    def test_stored_source_tampering_cannot_credit(self):
        ident = self.book(build_soccer_fixture(self.now)[0])
        evidence = self.evidence(ident)
        proof = json.loads(paper.trade_details(self.conn, ident)["contract_evidence_json"])
        proof["poly"]["source_fields"]["description"] += " Extra time included."
        with self.conn:
            self.conn.execute("UPDATE paper_v12_trades SET contract_evidence_json=? WHERE id=?",
                              (json.dumps(proof), ident))
        before = paper.free_balances(self.conn)
        with self.assertRaises(ValueError):
            paper.settle_verified_outcomes(self.conn, ident, True, False, evidence)
        self.assertEqual(paper.free_balances(self.conn), before)

    def test_future_explicit_reset_can_reuse_fixture_but_repeat_request_cannot_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = paper.open_database(Path(directory) / "reset.sqlite3")
            try:
                pair = build_soccer_fixture(self.now)[0]
                ident = self.book(pair, conn)
                result = reset.reset_virtual_bankroll(conn, "synthetic-v16-reset")
                self.assertTrue(result["applied"])
                self.assertEqual(paper.trade_details(conn, ident)["status"], "RESET_ARCHIVED")
                self.assertIsNotNone(self.book(pair, conn))
                before = paper.free_balances(conn)
                self.assertFalse(reset.reset_virtual_bankroll(conn, "synthetic-v16-reset")["applied"])
                self.assertEqual(paper.free_balances(conn), before)
            finally:
                conn.close()

    def test_schema_16_migration_preserves_balances_history_outbox_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "previous.sqlite3"
            conn = paper.open_database(path)
            try:
                new_trade(conn, "old", open_notice=True)
                reset.reset_virtual_bankroll(conn, "synthetic-v15-reset")
                new_trade(conn, "current", open_notice=True)
                # Reconstruct the previous deployed schema with its real data.
                conn.execute("DROP INDEX paper_v16_fixture_once")
                conn.execute("ALTER TABLE paper_v12_trades DROP COLUMN fixture_key")
                conn.execute("ALTER TABLE paper_v12_trades DROP COLUMN contract_evidence_json")
                conn.execute("DELETE FROM paper_v13_migrations WHERE version=17")
                conn.execute("INSERT OR IGNORE INTO paper_v13_migrations VALUES(16,?,NULL)", (self.now,))
                conn.commit()
                balances = paper.free_balances(conn)
                tables = ("paper_v12_trades", "paper_v12_open_notifications",
                          "paper_v14_resets", "paper_v14_reset_trades")
                snapshots = {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY 1")]
                             for name in tables}
                columns = [r[1] for r in conn.execute("PRAGMA table_info(paper_v12_trades)")]
            finally:
                conn.close()
            migrated = paper.open_database(path)
            try:
                self.assertEqual(paper.free_balances(migrated), balances)
                for name in tables:
                    selected = ",".join(columns) if name == "paper_v12_trades" else "*"
                    self.assertEqual([tuple(row) for row in migrated.execute(
                        "SELECT " + selected + " FROM " + name + " ORDER BY 1")], snapshots[name])
                self.assertEqual(migrated.execute("SELECT MAX(version) FROM paper_v13_migrations").fetchone()[0], 17)
                self.assertEqual(migrated.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertFalse(reset.reset_virtual_bankroll(migrated, "synthetic-v15-reset")["applied"])
                self.assertEqual(paper.free_balances(migrated), balances)
                backup_path = migrated.execute(
                    "SELECT backup_path FROM paper_v13_migrations WHERE version=17").fetchone()[0]
                with sqlite3.connect(backup_path) as backup:
                    self.assertEqual(paper.free_balances(backup), balances)
                    self.assertNotIn("fixture_key", {r[1] for r in backup.execute("PRAGMA table_info(paper_v12_trades)")})
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
