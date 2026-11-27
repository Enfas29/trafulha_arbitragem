"""Fee-aware PAPER quotes, immutable settlement and additive migration."""
from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_v12 as paper
from market_costs_v15 import FeeContext
from test_paper_bankroll_v14 import pair
from test_paper_freshness_v13 import AdvancingConnection
from test_paper_lifecycle_v13 import old_database, evidence_for


BOOKS = ([(.4, 1000)], [(.5, 1000)])


def context(name="test", rate=.02, now=None):
    now = time.time() if now is None else now
    return FeeContext("poly-" + name, .05, 1, rate, now, now + 900)


class PaperCostTests(unittest.TestCase):
    def setUp(self):
        self.conn = paper.open_database(":memory:")
        self.addCleanup(self.conn.close)

    def quote(self, name="test", rate=.02):
        return paper.quote_for_payout(*BOOKS, 100, 1, fee_context=context(name, rate))

    def book(self, name="test", rate=.02, conn=None):
        quote = self.quote(name, rate)
        ident = paper.book_shadow_trade(conn or self.conn, pair(name), quote,
                                        "PRE_JOGO", opening_odds=(2.5, 2),
                                        fee_context=quote.fee_context)
        self.assertIsNotNone(ident)
        return ident, quote

    def assert_empty(self):
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_known_example_uses_lesser_net_return_and_real_sx_profit_base(self):
        # 100 USDC payout: Poly stake40 + buffer0.50 + fee1.20; SX stake50
        # + buffer0.625. A 2% SX payout fee is1.00 (not2% of49.375).
        quote = self.quote()
        self.assertEqual(quote.poly_debit_cents, 4170)
        self.assertEqual(quote.sx_debit_cents, 5063)
        self.assertEqual(quote.poly_fee_cents, 120)
        self.assertEqual(quote.sx_payout_fee_cents, 100)
        self.assertEqual(quote.total_cents, 9233)
        self.assertEqual(quote.poly_net_payout_cents, 10000)
        self.assertEqual(quote.sx_net_payout_cents, 9900)
        self.assertEqual(quote.payout_eur_cents, 9900)
        self.assertEqual(quote.conditional_profit_cents, 667)

    def test_market_specific_poly_fee_changes_cost_instead_of_using_constant(self):
        quote = paper.quote_for_payout(*BOOKS, 100, 1,
                                      fee_context=replace(context(), poly_rate=0))
        self.assertEqual(quote.poly_fee_cents, 0)
        self.assertEqual(quote.poly_debit_cents, 4050)
        self.assertEqual(quote.conditional_profit_cents, 787)

    def test_sx_fee_can_remove_apparent_profit(self):
        without = paper.select_shadow_quote(*BOOKS, 1, 100000, 100000,
                                             fee_context=context(rate=0))
        with_fee = paper.select_shadow_quote(*BOOKS, 1, 100000, 100000,
                                              fee_context=context(rate=.2))
        self.assertIsNotNone(without)
        self.assertIsNone(with_fee)

    def test_expired_or_wrong_market_context_never_changes_wallets(self):
        expired = replace(context(), expires_epoch=time.time() - 1)
        self.assertIsNone(paper.quote_for_payout(*BOOKS, 100, 1, fee_context=expired))
        self.assertIsNone(paper.try_paper_entry(self.conn, pair("test"), *BOOKS, 1,
                                              "PRE_JOGO", .01, fee_context=context("different")))
        self.assert_empty()

    def test_quote_cannot_claim_lower_sx_fee_or_replace_verified_context(self):
        valid = self.quote()
        forged = replace(valid, sx_payout_fee_cents=0, sx_net_payout_cents=10000)
        self.assertIsNone(paper.book_shadow_trade(self.conn, pair("test"), forged, "PRE_JOGO"))
        self.assertIsNone(paper.book_shadow_trade(self.conn, pair("test"), valid, "PRE_JOGO",
                                                  fee_context=context(rate=0)))
        self.assert_empty()

    def test_fee_expiring_after_writer_lock_cannot_open(self):
        now = [time.time()]
        with patch.object(paper.time, "time", side_effect=lambda: now[0]):
            quote = self.quote()
            proxy = AdvancingConnection(self.conn,
                       after_begin=lambda: now.__setitem__(0, now[0] + 1000))
            self.assertIsNone(paper.book_shadow_trade(proxy, pair("test"), quote, "PRE_JOGO"))
        self.assert_empty()

    def test_fee_expiring_after_notice_write_rolls_back_entire_entry(self):
        now = [time.time()]
        with patch.object(paper.time, "time", side_effect=lambda: now[0]):
            quote = self.quote()
            proxy = AdvancingConnection(self.conn,
                       after_outbox=lambda: now.__setitem__(0, now[0] + 1000))
            self.assertIsNone(paper.book_shadow_trade(proxy, pair("test"), quote,
                              "PRE_JOGO", opening_odds=(2.5, 2)))
        self.assert_empty()

    def test_sx_settlement_uses_frozen_net_credit_even_after_fee_expiry(self):
        ident, quote = self.book()
        stored = paper.trade_details(self.conn, ident)
        evidence = json.loads(stored["fee_evidence_json"])
        self.assertEqual(evidence["sx_taker_payout_rate"], .02)
        self.assertNotIn("api_key", stored["fee_evidence_json"])
        expected = evidence_for(self.conn, ident, False, True)
        with patch.object(paper.time, "time", return_value=time.time() + 10000):
            result = paper.settle_verified_outcomes(self.conn, ident, False, True, expected)
        self.assertEqual(result, 6.67)
        row = paper.trade_details(self.conn, ident)
        self.assertEqual(row["sx_credit_cents"], 9900)
        self.assertEqual(row["poly_credit_cents"], 0)
        after = paper.free_balances(self.conn)
        self.assertIsNone(paper.settle_verified_outcomes(self.conn, ident, False, True, expected))
        self.assertEqual(paper.free_balances(self.conn), after)
        self.assertIn("1,00 € (já descontada)", paper.format_settlement_message(self.conn, ident))

    def test_poly_win_does_not_pay_sx_losing_leg_fee(self):
        ident, quote = self.book()
        result = paper.settle_verified_outcomes(self.conn, ident, True, False,
                                               evidence_for(self.conn, ident, True, False))
        self.assertEqual(result, 7.67)
        self.assertEqual(paper.trade_details(self.conn, ident)["poly_credit_cents"], 10000)
        self.assertIn("0,00 € (já descontada)", paper.format_settlement_message(self.conn, ident))

    def test_both_losing_legs_never_charge_a_payout_fee(self):
        ident, _ = self.book()
        self.assertEqual(paper.settle_verified_outcomes(
            self.conn, ident, False, False, evidence_for(self.conn, ident, False, False)), -92.33)
        self.assertEqual(paper.trade_details(self.conn, ident)["sx_credit_cents"], 0)

    def test_tampered_frozen_credit_blocks_settlement_without_partial_credit(self):
        ident, _ = self.book()
        with self.conn:
            self.conn.execute("UPDATE paper_v12_trades SET sx_net_payout_cents=10000 WHERE id=?", (ident,))
        before = paper.free_balances(self.conn)
        with self.assertRaises(ValueError):
            paper.settle_verified_outcomes(self.conn, ident, False, True,
                                           evidence_for(self.conn, ident, False, True))
        self.assertEqual(paper.free_balances(self.conn), before)
        self.assertEqual(paper.trade_details(self.conn, ident)["status"], "OPEN_CONDITIONAL")

    def test_open_notice_explains_both_returns_and_sx_contingent_fee(self):
        ident, _ = self.book()
        text = paper.format_open_message(self.conn, ident)
        self.assertIn("Crédito líquido se vencer Polymarket: 100,00 €", text)
        self.assertIn("Crédito líquido se vencer SX Bet: 99,00 €", text)
        self.assertIn("Comissão SX se vencer", text)
        self.assertIn("menor dos dois retornos", text)

    def test_legacy_migration_preserves_wallets_and_legacy_credit_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            old_database(path).close()
            conn = paper.open_database(path)
            try:
                self.assertEqual(paper.free_balances(conn), {"poly": 54321, "sx": 67890})
                self.assertIsNone(paper.trade_details(conn, 7)["fee_evidence_json"])
                self.assertEqual(paper.settle_normal(conn, 7, "sx", verified=True), 5)
                self.assertEqual(paper.trade_details(conn, 7)["sx_credit_cents"], 9000)
                self.assertEqual(paper.free_balances(conn), {"poly": 54321, "sx": 76890})
                self.assertEqual(conn.execute("SELECT MAX(version) FROM paper_v13_migrations").fetchone()[0], 16)
                backups = list((Path(directory) / "backups").glob("*.sqlite3"))
                self.assertEqual(len(backups), 1)
                with sqlite3.connect(backups[0]) as backup:
                    self.assertEqual(dict(backup.execute("SELECT * FROM paper_v12_wallets")),
                                     {"poly": 54321, "sx": 67890})
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
