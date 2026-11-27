"""V15 scanner integration: fees, evidence, parallel reads and wallet invariants.

All markets, clocks and HTTP responses are synthetic. Telegram and real HTTP
are forbidden even when a virtual trade is booked successfully.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import decision_audit_v15 as audit
import monitor_v11 as monitor
import paper_reset_v14 as reset
import paper_v12 as paper
from latency_metrics_v13 import LatencyMetrics
from market_costs_v15 import FeeContext, FeeProvider, SX_FEES_URL
from parallel_books_v15 import BookRead
from test_monitor_reactive_v13 import FakeClock


class MutableProvider:
    """A changing background cache exposed only through the public interface."""
    def __init__(self, context):
        self.current = context
        self.calls = []

    def context_for_pair(self, pair):
        self.calls.append(pair["poly"]["id"])
        return self.current, "fees_verified"


class MonitorOpportunitiesTests(unittest.TestCase):
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
        self.patches.enter_context(patch.object(
            monitor, "utc_now", side_effect=lambda: datetime.fromtimestamp(
                self.clock.time(), timezone.utc).isoformat(timespec="seconds")))
        self.patches.enter_context(patch.object(monitor.bot, "FX", 1.0))
        self.patches.enter_context(patch.object(monitor, "LATENCIES", LatencyMetrics(clock=self.clock.monotonic)))
        self.patches.enter_context(patch.object(monitor, "_BOOK_BACKOFF_UNTIL", 0.0))
        self.patches.enter_context(patch.object(paper, "ALLOW_LIVE_UNVERIFIED", False))
        confirmation = monitor.confirm_paper_snapshot
        def confirm(*args, **kwargs):
            kwargs["sleep_fn"] = self.clock.sleep
            return confirmation(*args, **kwargs)
        self.patches.enter_context(patch.object(monitor, "confirm_paper_snapshot", side_effect=confirm))
        self.http = self.patches.enter_context(patch.object(
            requests.sessions.Session, "request", side_effect=AssertionError("HTTP forbidden")))
        self.telegram = self.patches.enter_context(patch.object(
            monitor, "telegram", side_effect=AssertionError("Telegram forbidden")))
        self.paper_telegram = self.patches.enter_context(patch.object(
            paper, "send_telegram_notice", side_effect=AssertionError("Telegram forbidden")))
        self.send_inline = self.patches.enter_context(patch.object(
            paper, "send_open_once", side_effect=AssertionError("Use persistent outbox")))
        self.real_fetch_books = monitor.fetch_books
        self.reader = self.patches.enter_context(patch.object(monitor, "fetch_books", side_effect=self.books))

    def tearDown(self):
        self.http.assert_not_called()
        self.telegram.assert_not_called()
        self.paper_telegram.assert_not_called()
        self.send_inline.assert_not_called()

    def pair(self, number=1):
        start = self.clock.time() + 3600
        return {
            "key": f"opportunity-{number}",
            "poly": {"id": str(7600000 + number), "type": "moneyline",
                     "sport": "basketball", "start": start,
                     "feesEnabled": True,
                     "feeSchedule": {"rate": .05, "exponent": 1, "takerOnly": True},
                     "fees_fetched_epoch": self.clock.time()},
            "sx": {"hash": f"sx-{number}", "sport": "basketball",
                   "teams": ("Example A", "Example B"), "start": start,
                   "live_enabled": False},
            "poly_asset": f"asset-{number}", "sx_target": 1,
            "poly_label": "Example A", "sx_label": "Example B",
        }

    def context(self, pair, rate=.02):
        now = self.clock.time()
        return FeeContext(pair["poly"]["id"], .05, 1, rate, now, now + 900)

    def provider(self, pair, verified=True):
        def fees(url, **_kwargs):
            if url == SX_FEES_URL:
                return {"status": "success", "data": {"takerPayoutFee": ".02"}}
            self.fail("Unexpected HTTP metadata request: " + url)
        provider = FeeProvider(get_json=fees)
        provider.update_pairs([pair])
        if verified:
            provider.refresh_once()
        return provider

    def books(self, _pair, poly_price=.4, sx_price=.5):
        self.clock.advance(.01)
        now = self.clock.monotonic()
        return ([(poly_price, 1000)], now), ([(sx_price, 1000)], now)

    def scan(self, pair, provider, notify=None):
        return monitor.scan_pass(
            self.monitor_db, [pair], 0, 1.0, paper_conn=self.ledger,
            fee_provider=provider, work_item=(pair, None, "normal"),
            notify_open=notify or (lambda: None), verbose=False,
        )

    def decisions(self):
        cursor = self.monitor_db.execute(f"SELECT * FROM {audit.TABLE} ORDER BY id")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]

    def assert_untouched(self):
        self.assertEqual(paper.free_balances(self.ledger), {"poly": 100000, "sx": 100000})
        for table in ("paper_v12_trades", "paper_v12_open_notifications",
                      "paper_v12_settlement_notifications"):
            self.assertEqual(self.ledger.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertFalse(self.ledger.in_transaction)

    def test_missing_official_fee_keeps_observation_but_never_debits_or_rechecks(self):
        pair = self.pair()
        result = self.scan(pair, self.provider(pair, verified=False))
        self.assert_untouched()
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(result["paper_reasons"], {"sx_fees_pending": 1})
        row = self.decisions()[0]
        self.assertEqual(row["reason"], "sx_fees_pending")
        self.assertIsNone(row["trade_id"])
        self.assertEqual(json.loads(row["details_json"])["provider_reason"], "sx_fees_pending")
        observation = self.monitor_db.execute(
            "SELECT hypothetical_total_eur,hypothetical_profit_eur,note FROM observations").fetchone()
        self.assertEqual(observation[:2], (None, None))
        self.assertIn("Custos por verificar", observation[2])

    def test_expired_official_fee_is_not_silently_replaced_by_zero(self):
        pair = self.pair()
        provider = self.provider(pair)
        self.clock.advance(901)
        result = self.scan(pair, provider)
        self.assert_untouched()
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(result["paper_reasons"], {"sx_fees_expired": 1})
        self.assertEqual(self.decisions()[0]["reason"], "sx_fees_expired")

    def test_verified_sx_fee_commits_consistent_returns_outbox_and_audit_before_notify(self):
        pair = self.pair()
        observed = []
        def notified():
            with sqlite3.connect(self.ledger_path) as reader:
                observed.append((
                    reader.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0],
                    reader.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0],
                    self.ledger.in_transaction,
                ))
        result = self.scan(pair, self.provider(pair), notify=notified)
        self.assertEqual(result["paper_reasons"], {"registada": 1})
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(observed, [(1, 1, False)])
        trade = paper.trade_details(self.ledger, 1)
        evidence = json.loads(trade["fee_evidence_json"])
        self.assertEqual(evidence["sx_taker_payout_rate"], .02)
        self.assertEqual(evidence["poly_market_id"], pair["poly"]["id"])
        self.assertLess(trade["sx_net_payout_cents"], trade["poly_net_payout_cents"])
        total = trade["poly_debit_cents"] + trade["sx_debit_cents"]
        self.assertEqual(trade["conditional_profit_cents"],
                         min(trade["sx_net_payout_cents"], trade["poly_net_payout_cents"]) - total)
        self.assertEqual(paper.free_balances(self.ledger), {
            "poly": 100000 - trade["poly_debit_cents"],
            "sx": 100000 - trade["sx_debit_cents"],
        })
        self.assertTrue(all(amount >= 30000 for amount in paper.free_balances(self.ledger).values()))
        self.assertLessEqual(total, 40000)
        notice = self.ledger.execute("SELECT message,sent_epoch FROM paper_v12_open_notifications").fetchone()
        self.assertIn("Comissão SX se vencer", notice[0])
        self.assertIsNone(notice[1])
        decision = self.decisions()[0]
        detail = json.loads(decision["details_json"])
        self.assertEqual((decision["trade_id"], decision["reason"]), (1, "registada"))
        self.assertEqual(detail["stake_eur"], total / 100)
        self.assertAlmostEqual(detail["final_roi_pct"], 100 * trade["conditional_profit_cents"] / total)

    def test_price_change_preserves_concrete_reason_and_prices_without_debits(self):
        pair = self.pair()
        calls = []
        def changing_prices(current_pair):
            calls.append(current_pair)
            return self.books(current_pair, poly_price=.4 if len(calls) == 1 else .43)
        self.reader.side_effect = changing_prices
        result = self.scan(pair, self.provider(pair))
        self.assert_untouched()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["paper_reasons"], {"validacao_sem_registo": 1})
        row = self.decisions()[0]
        self.assertEqual(row["reason"], "recheck_price_changed")
        detail = json.loads(row["details_json"])
        self.assertGreater(detail["first_roi_pct"], 0)
        self.assertEqual((detail["best_poly_price"], detail["final_poly_price"]), (.4, .43))
        self.assertIsNone(row["trade_id"])

    def test_stale_parallel_read_never_fetches_a_recheck_or_debits(self):
        pair = self.pair()
        now = self.clock.monotonic()
        reading = BookRead(
            (pair, (now - 4, {"poly"}), "ws"), 4, now - 3.1, now - 3,
            books=(([(.4, 1000)], now - 3), ([(.5, 1000)], now - 3)),
        )
        result = monitor.process_book_read(
            self.monitor_db, reading, 4, 1.0, paper_conn=self.ledger,
            fee_provider=self.provider(pair), notify_open=lambda: None,
        )
        self.assert_untouched()
        self.reader.assert_not_called()
        self.assertEqual(result["paper_reasons"], {"validacao_sem_registo": 1})
        row = self.decisions()[0]
        self.assertEqual(row["reason"], "initial_quote_stale")
        self.assertGreaterEqual(json.loads(row["details_json"])["quote_age_ms"], 3000)

    def test_old_catalog_generation_cannot_read_fees_fetch_prices_or_write_either_database(self):
        pair = self.pair()
        now = self.clock.monotonic()
        provider = MutableProvider(self.context(pair))
        reading = BookRead(
            (pair, (now - .1, {"sx"}), "ws"), 2, now - .02, now - .01,
            books=(([(.4, 1000)], now - .01), ([(.5, 1000)], now - .01)),
        )
        changes = self.monitor_db.total_changes, self.ledger.total_changes
        result = monitor.process_book_read(
            self.monitor_db, reading, 3, 1.0, paper_conn=self.ledger,
            fee_provider=provider, notify_open=lambda: None,
        )
        self.assertIsNone(result)
        self.reader.assert_not_called()
        self.assertEqual(provider.calls, [])
        self.assertEqual((self.monitor_db.total_changes, self.ledger.total_changes), changes)
        self.assert_untouched()

    def test_backoff_activated_during_confirmation_wait_prevents_recheck_and_debit(self):
        pair = self.pair()
        now = self.clock.monotonic()
        reading = BookRead(
            (pair, None, "normal"), 1, now - .02, now,
            books=(([(.4, 1000)], now), ([(.5, 1000)], now)),
        )
        def enter_backoff(seconds):
            self.clock.advance(seconds)
            monitor._BOOK_BACKOFF_UNTIL = self.clock.monotonic() + 10
        with patch.object(self.clock, "sleep", side_effect=enter_backoff) as delay:
            monitor.process_book_read(
                self.monitor_db, reading, 1, 1.0, paper_conn=self.ledger,
                fee_provider=self.provider(pair), notify_open=lambda: None,
            )
        delay.assert_called_once()
        self.reader.assert_not_called()
        self.assert_untouched()
        row = self.decisions()[0]
        self.assertEqual(row["reason"], "recheck_rate_limit_backoff")
        self.assertGreater(json.loads(row["details_json"])["first_roi_pct"], 0)
        self.assertIsNone(row["trade_id"])

    def test_active_backoff_does_not_submit_either_platform_book_request(self):
        pair = self.pair()
        changes = self.monitor_db.total_changes, self.ledger.total_changes
        with patch.object(monitor, "_BOOK_BACKOFF_UNTIL", self.clock.monotonic() + 10), \
                patch.object(monitor.bot, "poly_book") as poly, \
                patch.object(monitor.bot, "sx_book") as sx:
            with self.assertRaises(monitor.BookBackoffActive):
                self.real_fetch_books(pair)
        poly.assert_not_called()
        sx.assert_not_called()
        self.assertEqual((self.monitor_db.total_changes, self.ledger.total_changes), changes)
        self.assert_untouched()

    def test_fee_refresh_between_books_cannot_change_frozen_costs_mid_entry(self):
        pair = self.pair()
        original = self.context(pair)
        provider = MutableProvider(original)
        reads = []
        def change_cache(current_pair):
            reads.append(current_pair)
            if len(reads) == 2:
                provider.current = replace(original, sx_taker_payout_rate=.5)
            return self.books(current_pair)
        self.reader.side_effect = change_cache
        result = self.scan(pair, provider)
        self.assertEqual(result["paper_reasons"], {"registada": 1})
        self.assertEqual(provider.calls, [pair["poly"]["id"]])
        self.assertEqual(provider.current.sx_taker_payout_rate, .5)
        stored = json.loads(paper.trade_details(self.ledger, 1)["fee_evidence_json"])
        self.assertEqual(stored["sx_taker_payout_rate"], .02)
        self.assertEqual(stored["observed_epoch"], original.observed_epoch)
        detail = json.loads(self.decisions()[0]["details_json"])
        self.assertAlmostEqual(detail["first_roi_pct"], detail["final_roi_pct"])

    def test_expiry_during_recheck_blocks_entry_even_when_prices_are_unchanged(self):
        pair = self.pair()
        fee = replace(self.context(pair), expires_epoch=self.clock.time() + .2)
        self.scan(pair, MutableProvider(fee))
        self.assert_untouched()
        self.assertEqual(self.reader.call_count, 1)
        row = self.decisions()[0]
        self.assertEqual(row["reason"], "costs_expired")
        self.assertIsNone(row["trade_id"])

    def test_audit_write_failure_cannot_undo_or_repeat_committed_virtual_trade(self):
        pair = self.pair()
        provider = self.provider(pair)
        notified = Mock()
        with patch.object(audit, "record_decision", side_effect=sqlite3.OperationalError("synthetic audit lock")):
            with self.assertLogs(monitor.LOG, level="WARNING") as logged:
                first = self.scan(pair, provider, notify=notified)
            after = paper.free_balances(self.ledger)
            second = self.scan(pair, provider, notify=notified)
        self.assertEqual(first["paper_reasons"], {"registada": 1})
        self.assertEqual(second["paper_reasons"], {"ja_registada": 1})
        self.assertEqual(paper.free_balances(self.ledger), after)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)
        self.assertEqual(self.decisions(), [])
        self.assertFalse(self.ledger.in_transaction)
        self.assertEqual(notified.call_count, 1)
        self.assertTrue(any("PAPER_AUDIT" in item for item in logged.output))

    def test_summary_after_reset_keeps_historical_signal_separate_from_current_money(self):
        pair = self.pair()
        provider = self.provider(pair)
        self.scan(pair, provider)
        self.assertLess(paper.free_balances(self.ledger)["poly"], 100000)
        self.clock.advance(120)
        cleared = reset.reset_virtual_bankroll(
            self.ledger, "reset-opportunities-v15", now_epoch=self.clock.time())
        self.assertTrue(cleared["applied"])
        self.clock.advance(5)
        self.reader.side_effect = lambda p: self.books(p, poly_price=.55, sx_price=.6)
        self.scan(pair, provider)
        messages = []
        monitor.send_summary(
            self.monitor_db, 2, now_epoch=self.clock.time(), paper_conn=self.ledger,
            sender=messages.append,
        )
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertIn("ÚLTIMAS 6H", message)
        self.assertIn("0 abertas · 0 liquidadas · 0 em revisão", message)
        self.assertIn("Polymarket: 1.000,00 € livres", message)
        self.assertIn("SX Bet: 1.000,00 € livres", message)
        self.assertIn("antes do reset", message)
        self.assertIn("amostras guardadas", message)
        self.assertIn("1 registos virtuais auditados", message)
        self.assertIn("sem apostas reais", message)
        self.assertEqual(paper.free_balances(self.ledger), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.ledger.execute("SELECT status FROM paper_v12_trades").fetchone()[0], "RESET_ARCHIVED")
        self.assertEqual(self.monitor_db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        self.assertLess(len(message), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
