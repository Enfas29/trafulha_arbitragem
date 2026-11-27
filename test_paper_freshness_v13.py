"""Freshness survives SQLite waits; all money and notices remain synthetic."""
import unittest
from unittest.mock import patch

import paper_v12 as paper


class FakeClock:
    def __init__(self):
        self.mono = 100.0
        self.epoch = 1800000000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.epoch


class AdvancingConnection:
    """A real SQLite connection with deterministic delay after one statement."""
    def __init__(self, connection, after_begin=None, after_outbox=None):
        self.connection = connection
        self.after_begin = after_begin
        self.after_outbox = after_outbox

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        if sql.strip().upper() == "BEGIN IMMEDIATE" and self.after_begin:
            self.after_begin()
        if "INSERT INTO paper_v12_open_notifications" in sql and self.after_outbox:
            self.after_outbox()
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)

    @property
    def row_factory(self):
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.conn = paper.open_database(":memory:")
        self.addCleanup(self.conn.close)
        self.clock = FakeClock()
        self.addCleanup(patch.stopall)
        patch.object(paper.time, "monotonic", side_effect=self.clock.monotonic).start()
        patch.object(paper.time, "time", side_effect=self.clock.time).start()
        self.pair = {
            "poly": {"id": "poly-fresh", "type": "moneyline", "sport": "basketball",
                     "start": self.clock.epoch + 3600},
            "sx": {"hash": "sx-fresh", "sport": "basketball",
                   "teams": ("Example A", "Example B"), "start": self.clock.epoch + 3600},
            "poly_asset": "token-fresh", "sx_target": 1,
            "poly_label": "Example A", "sx_label": "Example B",
        }
        self.levels = ([(0.4, 300)], [(0.5, 300)])
        self.quote = paper.select_shadow_quote(*self.levels, 0.91, 100000, 100000)
        self.assertIsNotNone(self.quote)
        self.balances = paper.free_balances(self.conn)

    def enter(self, conn=None, deadline=102.0):
        return paper.try_paper_entry(
            conn or self.conn, self.pair, *self.levels, 0.91, "PRE_JOGO", 0.01,
            valid_until_mono=deadline,
        )

    def assert_unchanged(self):
        self.assertEqual(paper.free_balances(self.conn), self.balances)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)

    def test_invalid_or_expired_deadline_never_writes(self):
        for deadline in (float("nan"), float("inf"), -float("inf"),
                         "102", "invalid", True, object(), 99.9):
            with self.subTest(deadline=str(deadline)):
                self.assertIsNone(self.enter(deadline=deadline))
                self.assertIsNone(paper.book_shadow_trade(
                    self.conn, self.pair, self.quote, "PRE_JOGO",
                    valid_until_mono=deadline,
                ))
                self.assert_unchanged()

    def test_deadline_expiring_during_begin_rolls_back_without_debit(self):
        def delayed_lock():
            self.clock.mono = 103.0
        proxy = AdvancingConnection(self.conn, after_begin=delayed_lock)
        self.assertIsNone(self.enter(proxy))
        self.assert_unchanged()

    def test_game_start_crossed_during_begin_rolls_back(self):
        def delayed_lock():
            self.clock.mono = 100.2  # Still within the price deadline.
            self.clock.epoch = self.pair["sx"]["start"]
        proxy = AdvancingConnection(self.conn, after_begin=delayed_lock)
        self.assertIsNone(self.enter(proxy))
        self.assert_unchanged()

    def test_deadline_expiring_after_outbox_insert_rolls_back_everything(self):
        def delayed_outbox():
            # Debit, operation and notice really exist inside the transaction.
            self.assertNotEqual(paper.free_balances(self.conn), self.balances)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)
            self.clock.mono = 103.0
        proxy = AdvancingConnection(self.conn, after_outbox=delayed_outbox)
        self.assertIsNone(self.enter(proxy))
        self.assert_unchanged()

    def test_game_start_crossed_after_outbox_insert_rolls_back_everything(self):
        def delayed_outbox():
            self.clock.epoch = self.pair["sx"]["start"]
        proxy = AdvancingConnection(self.conn, after_outbox=delayed_outbox)
        self.assertIsNone(self.enter(proxy))
        self.assert_unchanged()

    def test_valid_guard_uses_current_creation_time_after_lock_and_deduplicates(self):
        def brief_lock_wait():
            self.clock.mono = 100.5
            self.clock.epoch += 1
        proxy = AdvancingConnection(self.conn, after_begin=brief_lock_wait)
        ident = paper.book_shadow_trade(
            proxy, self.pair, self.quote, "PRE_JOGO", now_epoch=123,
            opening_odds=(2.5, 2.0), valid_until_mono=102.0,
        )
        self.assertIsNotNone(ident)
        row = paper.trade_details(self.conn, ident)
        self.assertEqual(row["created_epoch"], 1800000001)
        after = paper.free_balances(self.conn)
        self.assertEqual(after, {
            "poly": self.balances["poly"] - self.quote.poly_debit_cents,
            "sx": self.balances["sx"] - self.quote.sx_debit_cents,
        })
        self.assertIsNone(self.enter())
        self.assertEqual(paper.free_balances(self.conn), after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 1)

    def test_deadline_boundary_is_inclusive(self):
        self.assertIsNotNone(self.enter(deadline=self.clock.mono))

    def test_guard_rejects_already_started_or_unknown_pre_game_time(self):
        for start in (self.clock.epoch, self.clock.epoch - 1,
                      float("nan"), float("inf"), None):
            with self.subTest(start=start):
                self.pair["sx"]["start"] = start
                self.assertIsNone(self.enter())
                self.assert_unchanged()

    def test_legacy_calls_without_guard_keep_historical_fixture_times(self):
        self.pair["poly"]["start"] = self.clock.epoch - 3600
        self.pair["sx"]["start"] = self.clock.epoch - 3600
        ident = paper.book_shadow_trade(
            self.conn, self.pair, self.quote, "PRE_JOGO", now_epoch=123,
        )
        self.assertIsNotNone(ident)
        self.assertEqual(paper.trade_details(self.conn, ident)["created_epoch"], 123)


if __name__ == "__main__":
    unittest.main()
