"""Offline integration: public phase checks -> delay -> fresh books -> ledger."""
from __future__ import annotations

import copy
import json
import unittest
from concurrent.futures import Future
from contextlib import ExitStack
from unittest.mock import patch

import monitor_v11 as monitor
import paper_v12 as paper
from live_candidates_v13 import LiveCandidates
from live_market_v13 import fetch_live_snapshot, LiveCheckError
from test_live_market_v13 import Fixtures
from test_monitor_reactive_v13 import FakeClock


class ImmediateExecutor:
    """Run synthetic source work immediately; scanner still polls the future."""
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class LiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.api = Fixtures()
        self.pair = self.api.pair
        self.pair["poly"]["start"] = self.pair["sx"]["start"]
        self.monitor_db = monitor.open_database(":memory:")
        self.ledger = paper.open_database(":memory:")
        self.addCleanup(self.monitor_db.close)
        self.addCleanup(self.ledger.close)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(monitor.time, "monotonic", self.clock.monotonic))
        self.patches.enter_context(patch.object(monitor.time, "time", self.clock.time))
        self.patches.enter_context(patch.object(paper, "PAPER_LIVE_ENABLED", True))
        self.patches.enter_context(patch.object(paper, "ALLOW_LIVE_UNVERIFIED", True))
        self.patches.enter_context(patch.object(monitor.bot, "FX", .91))
        self.fetch = self.patches.enter_context(patch.object(monitor, "fetch_books", side_effect=self.books))
        confirmation = monitor.confirm_paper_snapshot
        def confirm(*args, **kwargs):
            return confirmation(*args, **kwargs, sleep_fn=self.clock.sleep)
        self.patches.enter_context(patch.object(monitor, "confirm_paper_snapshot", side_effect=confirm))
        self.telegram = self.patches.enter_context(patch.object(paper, "send_telegram_notice"))
        self.coordinator = LiveCandidates(
            fetch_snapshot=lambda pair: fetch_live_snapshot(
                pair, get_json=self.api.get, clock=self.clock.monotonic, wall=self.clock.time),
            clock=self.clock.monotonic, executor=ImmediateExecutor(), max_workers=1,
        )
        self.addCleanup(self.coordinator.stop)
        self.notices = []
        self.poly_price, self.sx_price = .4, .5
        self.book_seconds = .1

    def books(self, _pair):
        self.clock.advance(self.book_seconds)
        return (([(self.poly_price, 300)], self.clock.monotonic()),
                ([(self.sx_price, 300)], self.clock.monotonic()))

    def scan(self, pair=None, coordinator=True, event=None):
        pair = self.pair if pair is None else pair
        return monitor.scan_pass(
            self.monitor_db, [pair], 0, .91, paper_conn=self.ledger,
            work_item=(pair, event, "live" if event else "rotation"),
            notify_open=lambda: self.notices.append(1), verbose=False,
            live_candidates=self.coordinator if coordinator else None,
        )

    def untouched(self):
        self.assertEqual(paper.free_balances(self.ledger), {"poly": 100000, "sx": 100000})
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM paper_v12_open_notifications").fetchone()[0], 0)
        self.assertEqual(self.notices, [])
        self.telegram.assert_not_called()

    def ready(self):
        result = self.scan()
        self.assertEqual(result["paper_reasons"], {"live_em_verificacao": 1})
        self.coordinator.tick()
        self.assertEqual(self.coordinator.snapshot()["phases"], {"waiting": 1})
        self.untouched()
        self.clock.advance(8.0)
        self.coordinator.tick()  # Starts the independent final checks.
        self.coordinator.tick()  # Receives those checks.
        self.assertEqual(self.coordinator.snapshot()["phases"], {"ready": 1})
        self.assertEqual(len(self.api.calls), 8)

    def test_live_end_to_end_waits_rechecks_persists_and_notifies_once(self):
        initial = self.clock.monotonic()
        self.ready()
        events = self.coordinator.take_ready_events()
        scheduler = monitor.ReactiveScheduler([self.pair])
        scheduler.ingest_ready(events, self.coordinator.ready_deadlines(), now=self.clock.monotonic())
        item = scheduler.choose(now=self.clock.monotonic())
        self.assertEqual(item[2], "live")
        result = self.scan(event=item[1])
        self.assertEqual(result["paper_reasons"], {"registada": 1})
        row = self.ledger.execute("SELECT * FROM paper_v12_trades").fetchone()
        record = paper.trade_details(self.ledger, row[0])
        self.assertEqual(record["stage"], "LIVE_CONFIRMADO")
        evidence = json.loads(record["entry_evidence_json"])
        self.assertEqual(evidence["required_delay_ms"], 8000)
        self.assertGreaterEqual(evidence["checks_started_mono"], initial + 8)
        self.assertGreaterEqual(evidence["checks_started_mono"], evidence["delay_ready_mono"])
        self.assertEqual(self.fetch.call_count, 3)  # Initial, post-delay, 250ms recheck.
        self.assertEqual(self.clock.sleeps, [.25])
        self.assertEqual(self.notices, [1])
        self.assertGreater(result["ws_latencies"][0]["decision"], 8000)
        self.assertIn("LIVE", result["ws_latencies"][0]["sources"])
        balances = paper.free_balances(self.ledger)
        self.assertEqual(self.scan()["paper_reasons"], {"ja_registada": 1})
        self.assertEqual(paper.free_balances(self.ledger), balances)
        self.assertEqual(self.notices, [1])
        self.assertEqual(len(self.api.calls), 8)

    def test_other_pregame_market_keeps_running_while_live_waits(self):
        self.scan()
        self.coordinator.tick()
        other = copy.deepcopy(self.pair)
        other["key"] = "other-direction"
        other["poly"]["id"] = "22"
        other["sx"]["hash"] = "0x" + "cd" * 32
        other["poly"]["start"] = other["sx"]["start"] = self.clock.time() + 3600
        result = self.scan(other)
        self.assertEqual(result["paper_reasons"], {"registada": 1})
        self.assertEqual(self.coordinator.snapshot()["phases"], {"waiting": 1})
        self.assertEqual(self.clock.sleeps, [.25])
        self.assertLess(self.clock.monotonic(), 1001)
        self.assertEqual(len(self.api.calls), 4)

    def test_margin_disappearing_after_delay_discards_without_debit(self):
        self.ready()
        self.poly_price = .55
        result = self.scan()
        self.assertEqual(result["paper_reasons"], {"live_preco_margem_saldo_inelegivel": 1})
        self.assertEqual(self.coordinator.snapshot()["pending"], 0)
        self.assertEqual(self.clock.sleeps, [])
        self.untouched()

    def test_suspended_during_delay_never_opens_a_trade(self):
        self.scan()
        self.coordinator.tick()
        self.api.fixture["status"] = 6
        self.clock.advance(8)
        self.coordinator.tick()
        self.coordinator.tick()
        result = self.scan()
        self.assertEqual(result["paper_reasons"], {"sx_fixture_not_live": 1})
        self.assertEqual(self.clock.sleeps, [])
        self.untouched()

    def test_proof_expiring_during_final_books_rolls_back(self):
        self.ready()
        self.book_seconds = 1.6
        self.scan()
        self.assertEqual(self.clock.sleeps, [.25])
        self.untouched()

    def test_books_timeout_discards_candidate_without_fallback(self):
        self.ready()
        self.fetch.side_effect = TimeoutError("synthetic")
        self.scan()
        self.assertEqual(self.coordinator.snapshot()["pending"], 0)
        self.untouched()

    def test_legacy_flag_cannot_bypass_missing_live_coordinator(self):
        result = self.scan(coordinator=False)
        self.assertEqual(result["paper_reasons"], {"live_desativado_ou_sem_coordenador": 1})
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(len(self.api.calls), 0)
        self.untouched()

    def test_true_live_hint_before_scheduled_time_still_requires_proof(self):
        self.pair["poly"]["event_live"] = True
        self.pair["poly"]["start"] = self.pair["sx"]["start"] = self.clock.time() + 3600
        result = self.scan(coordinator=False)
        self.assertEqual(result["paper_reasons"], {"live_desativado_ou_sem_coordenador": 1})
        self.untouched()

    def test_unknown_live_enabled_cannot_authorize_even_with_active_books(self):
        self.pair["sx"]["live_enabled"] = "false"
        self.assertEqual(self.scan()["checked"], 0)
        self.fetch.assert_not_called()
        self.untouched()

    def test_probe_is_read_only_and_deduplicates_directions(self):
        other_direction = copy.deepcopy(self.pair)
        other_direction["key"] += "-opposite"
        seen = []
        def probe(pair):
            seen.append(pair["key"])
            raise LiveCheckError("poly_not_live")
        result = monitor.probe_live_markets([self.pair, other_direction], snapshot_fn=probe)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result[0]["estado"], "poly_not_live")
        self.assertEqual(self.coordinator.snapshot()["pending"], 0)
        self.untouched()


class CatalogLiveTests(unittest.TestCase):
    def test_discovery_preserves_source_ids_and_never_coerces_live_strings(self):
        api = Fixtures()
        api.event.update(title="Team A vs Team B", startTime="2027-01-01T18:00:00Z")
        api.market["gameStartTime"] = "2027-01-01T18:00:00Z"
        def get_poly(url, params=None):
            return [{"sport": "nba", "series": "103"}] if url.endswith("/sports") else [api.event]
        with patch.object(monitor.bot, "get_json", side_effect=get_poly):
            result = monitor.bot.discover_poly()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["event_id"], result[0]["version"], result[0]["event_live"]),
                         ("101", "v1", True))
        api.sx_market["liveEnabled"] = "false"
        with patch.object(monitor.bot, "get_json", return_value={
                "data": {"markets": [api.sx_market]}}):
            result = monitor.bot.discover_sx()
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["event_id"], result[0]["sport_id"], result[0]["league_id"]),
                         ("L111", 3, 29))
        self.assertIs(result[0]["live_enabled"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
