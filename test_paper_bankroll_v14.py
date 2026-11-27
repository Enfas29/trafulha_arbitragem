"""Proportional PAPER allocation, transactional reserves and archived notices."""
from __future__ import annotations

import concurrent.futures
from dataclasses import replace
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bankroll_v14 as bankroll
import paper_v12 as paper
from test_paper_freshness_v13 import AdvancingConnection
from test_paper_lifecycle_v13 import evidence_for


def pair(name):
    start = time.time() + 3600
    return {
        "key": f"poly-{name}:sx-{name}:0",
        "poly": {"id": f"poly-{name}", "type": "moneyline", "sport": "basketball", "start": start},
        "sx": {"hash": f"sx-{name}", "sport": "basketball", "teams": ("Example A", "Example B"), "start": start},
        "poly_asset": f"asset-{name}", "sx_target": 1,
        "poly_label": "Example A", "sx_label": "Example B",
    }


BOOKS = ([(.43, 10000)], [(.5, 10000)])


def select(conn, books=BOOKS):
    free = paper.free_balances(conn)
    return paper.select_shadow_quote(
        *books, .91, free["poly"], free["sx"], allocation=paper.allocation_budget(conn),
    )


def enter(conn, name, books=BOOKS):
    return paper.try_paper_entry(conn, pair(name), *books, .91, "PRE_JOGO", .01)


class PaperBankrollTests(unittest.TestCase):
    def setUp(self):
        self.policy_patch = patch.object(bankroll, "POLICY", bankroll.BankrollPolicy())
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        self.conn = paper.open_database(":memory:")
        self.addCleanup(self.conn.close)

    def assert_reserves(self, conn=None):
        conn = self.conn if conn is None else conn
        budget = paper.allocation_budget(conn)
        free = paper.free_balances(conn)
        self.assertGreaterEqual(free["poly"], budget.poly_reserve_cents)
        self.assertGreaterEqual(free["sx"], budget.sx_reserve_cents)
        self.assertLessEqual(budget.open_exposure_cents, budget.exposure_cap_cents)
        self.assertEqual(budget.total_equity_cents, sum(free.values()) + paper.open_exposure(conn))
        return budget

    def test_first_entry_can_exceed_old_100_eur_cap_and_keeps_both_reserves(self):
        ident, quote = enter(self.conn, "first")
        self.assertEqual(quote.total_cents, 40000)
        self.assertGreater(quote.total_eur, 100)
        self.assertEqual(self.assert_reserves().total_equity_cents, 200000)
        row = paper.trade_details(self.conn, ident)
        stored = json.loads(row["bankroll_evidence_json"])
        self.assertEqual(stored["method"], "bankroll_proportional_v1")
        self.assertEqual(stored["free_before_cents"], {"poly": 100000, "sx": 100000})
        self.assertEqual(stored["allocation"]["total_cap_cents"], 40000)
        self.assertEqual(stored["allocation"]["poly_reserve_cents"], 30000)
        self.assertEqual(stored["allocation"]["sx_reserve_cents"], 30000)
        self.assertEqual(stored["allocation"]["policy"]["reserve_bps"], 3000)

    def test_large_virtual_bankroll_has_no_fixed_1000_payout_or_500_exposure_cap(self):
        with self.conn:
            self.conn.execute("UPDATE paper_v12_wallets SET free_cents=1000000")
        ident, quote = enter(self.conn, "large")
        self.assertEqual(quote.total_cents, 400000)
        self.assertGreater(quote.payout_usdc, 1000)
        self.assertGreater(paper.open_exposure(self.conn), 50000)
        self.assertEqual(paper.trade_details(self.conn, ident)["status"], "OPEN_CONDITIONAL")
        self.assert_reserves()

    def test_multiple_operations_leave_30_percent_in_each_wallet(self):
        amounts = []
        for index in range(12):
            opened = enter(self.conn, str(index))
            if opened is None:
                break
            amounts.append(opened[1].total_cents)
            self.assert_reserves()
        self.assertGreaterEqual(len(amounts), 3)
        self.assertEqual(amounts[:3], [40000] * 3)
        self.assertLess(amounts[-1], 40000)
        free = paper.free_balances(self.conn)
        self.assertGreaterEqual(min(free.values()), 30000)
        self.assertLessEqual(sum(amounts), 140000)
        self.assertIsNone(enter(self.conn, "later-cannot-consume-reserve"))

    def test_stale_large_quote_is_rejected_after_competing_entries(self):
        stale_quote = select(self.conn)
        for index in range(3):
            self.assertIsNotNone(enter(self.conn, str(index)))
        before = paper.free_balances(self.conn)
        count = self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0]
        self.assertIsNone(paper.book_shadow_trade(self.conn, pair("stale"), stale_quote, "PRE_JOGO"))
        self.assertEqual(paper.free_balances(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], count)
        self.assertFalse(self.conn.in_transaction)
        self.assert_reserves()

    def test_uneven_wallet_budget_never_spends_another_platforms_reserve(self):
        with self.conn:
            self.conn.execute("UPDATE paper_v12_wallets SET free_cents=10000 WHERE platform='sx'")
        opened = enter(self.conn, "uneven")
        self.assertIsNotNone(opened)
        quote = opened[1]
        self.assertLess(quote.total_cents, 22000)  # 20% of combined equity is not all usable.
        self.assertLessEqual(quote.sx_debit_cents, 7000)
        self.assertGreaterEqual(paper.free_balances(self.conn)["sx"], 3000)
        self.assert_reserves()

    def test_a_loss_reduces_following_budget_without_counting_hypothetical_profit(self):
        ident, _ = enter(self.conn, "loss")
        self.assertEqual(paper.allocation_budget(self.conn).total_equity_cents, 200000)
        paper.settle_verified_outcomes(self.conn, ident, False, False,
                                       evidence_for(self.conn, ident, False, False))
        budget = self.assert_reserves()
        self.assertEqual(budget.total_equity_cents, 160000)
        self.assertEqual(budget.total_cap_cents, 32000)
        self.assertEqual(select(self.conn).total_cents, 32000)

    def test_a_confirmed_gain_increases_following_budget(self):
        ident, quote = enter(self.conn, "gain")
        paper.settle_verified_outcomes(self.conn, ident, True, False,
                                       evidence_for(self.conn, ident, True, False))
        budget = self.assert_reserves()
        self.assertEqual(budget.total_equity_cents, 200000 + quote.conditional_profit_cents)
        self.assertEqual(budget.operation_cap_cents, budget.total_equity_cents // 5)
        self.assertGreater(select(self.conn).total_cents, 40000)

    def test_review_still_reserves_cost_but_archived_history_does_not(self):
        ident, quote = enter(self.conn, "history")
        paper.flag_exception(self.conn, ident, "Synthetic result needs review")
        self.assertEqual(paper.allocation_budget(self.conn).open_exposure_cents, quote.total_cents)
        with self.conn:
            self.conn.execute("UPDATE paper_v12_trades SET status='RESET_ARCHIVED'")
            self.conn.execute("UPDATE paper_v12_wallets SET free_cents=100000")
        budget = paper.allocation_budget(self.conn)
        self.assertEqual(budget.total_equity_cents, 200000)
        self.assertEqual(budget.open_exposure_cents, 0)
        self.assertEqual(budget.total_cap_cents, 40000)
        self.assertEqual(paper.paper_summary(self.conn)["open_trades"], 0)

    def test_better_shallow_levels_choose_smaller_profitable_size(self):
        books = ([(.43, 150), (.7, 1000)], [(.5, 150), (.7, 1000)])
        largest = paper.quote_for_payout(*books, 350, .91)
        self.assertLess(largest.roi, paper.MIN_NET_ROI)
        quote = select(self.conn, books)
        self.assertIsNotNone(quote)
        self.assertGreater(quote.total_cents, 10000)
        self.assertLess(quote.total_cents, 40000)
        self.assertGreaterEqual(quote.roi, paper.MIN_NET_ROI)
        self.assertLess(quote.roi, paper.MAX_AUTO_PAPER_ROI)
        self.assertIsNotNone(enter(self.conn, "depth", books))

    def test_anomalous_margin_and_insufficient_depth_do_not_force_an_entry(self):
        self.assertIsNone(select(self.conn, ([(.51, 10000)], [(.14625, 10000)])))
        self.assertIsNone(select(self.conn, ([(.43, .1)], [(.5, 10000)])))
        self.assertIsNone(select(self.conn, ([(.55, 10000)], [(.55, 10000)])))
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})

    def test_minimum_cent_rounding_does_not_hide_a_profitable_shallow_level(self):
        books = ([(.45, 150), (.7, 1000)], [(.5098, 150), (.7, 1000)])
        with patch.object(paper, "MIN_NET_ROI", .015):
            known_valid = paper.quote_for_payout(*books, 100, .91)
            self.assertGreater(known_valid.roi, .015)
            quote = select(self.conn, books)
            self.assertIsNotNone(quote)
            self.assertGreaterEqual(quote.total_cents, known_valid.total_cents)
            self.assertGreaterEqual(quote.roi, .015)
            self.assertLess(quote.total_cents, 40000)

    def test_bad_direct_cent_amounts_cannot_bypass_reserves(self):
        quote = select(self.conn)
        malformed = (
            replace(quote, poly_debit_cents=-1),
            replace(quote, sx_debit_cents=0),
            replace(quote, total_cents=1),
            replace(quote, conditional_profit_cents=quote.conditional_profit_cents + 1),
            replace(quote, roi=.01),
            replace(quote, payout_eur_cents=quote.payout_eur_cents + 1),
            replace(quote, poly_fee_cents=True),
        )
        for item in malformed:
            with self.subTest(item=item):
                self.assertIsNone(paper.book_shadow_trade(self.conn, pair("invalid"), item, "PRE_JOGO"))
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)

    def test_price_expiry_rolls_back_budget_audit_and_outbox_with_wallets(self):
        clock = [100.0]
        def after_outbox():
            row = self.conn.execute("SELECT bankroll_evidence_json FROM paper_v12_trades").fetchone()
            self.assertEqual(json.loads(row[0])["allocation"]["total_cap_cents"], 40000)
            clock[0] = 103.0
        proxy = AdvancingConnection(self.conn, after_outbox=after_outbox)
        quote = select(self.conn)
        with patch.object(paper.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertIsNone(paper.book_shadow_trade(
                proxy, pair("expired"), quote, "PRE_JOGO", valid_until_mono=102,
                opening_odds=(1 / .43, 2),
            ))
        self.assertEqual(paper.free_balances(self.conn), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)

    def test_open_notice_contains_frozen_policy_and_archived_notice_never_sends(self):
        ident, _ = enter(self.conn, "notice")
        original = self.conn.execute("SELECT message FROM paper_v12_open_notifications").fetchone()[0]
        for snippet in ("Gestão automática", "20%", "400,00 €", "300,00 €", "30%"):
            self.assertIn(snippet, original)
        with self.conn:
            self.conn.execute("UPDATE paper_v12_trades SET status='RESET_ARCHIVED' WHERE id=?", (ident,))
        with patch.object(paper, "send_telegram_notice") as sender:
            self.assertFalse(paper.send_open_once(self.conn, ident, retry_seconds=0))
            self.assertEqual(paper.retry_pending_open_notices(self.conn), 0)
            sender.assert_not_called()
        row = self.conn.execute("SELECT message,sent_epoch,attempts FROM paper_v12_open_notifications").fetchone()
        self.assertEqual(tuple(row), (original, None, 0))

    def test_atomic_writers_cannot_both_consume_the_last_available_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parallel.sqlite3"
            conn = paper.open_database(path)
            try:
                for index in range(3):
                    self.assertIsNotNone(enter(conn, f"first-{index}"))
                before = paper.free_balances(conn)
                initial_count = conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0]
                barrier = threading.Barrier(2)
                def competing(number):
                    local = paper.open_database(path)
                    try:
                        candidate = select(local)
                        self.assertIsNotNone(candidate)
                        barrier.wait(timeout=5)
                        return paper.book_shadow_trade(local, pair(f"race-{number}"), candidate, "PRE_JOGO")
                    finally:
                        local.close()
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(competing, range(2)))
                self.assertEqual(sum(value is not None for value in results), 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], initial_count + 1)
                self.assertNotEqual(paper.free_balances(conn), before)
                self.assert_reserves(conn)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
