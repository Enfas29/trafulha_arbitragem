"""Offline synthetic fixtures using documented public-result response schemas.

No HTTP requests, betting API writes, account credentials or Telegram sends.
Run: python -m unittest test_paper_results_v13 -v
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import paper_results_v13 as results
import paper_v12 as paper


NOW = 1_800_000_000


class ResultFixtures:
    def __init__(self):
        self.markets = {}
        self.tokens = {}
        self.sx = {}
        self.calls = []

    def add(self, ident, poly_won=True, sx_won=False):
        market_id, token_id = str(ident), str(10000 + ident * 2)
        condition = "0x" + format(ident, "064x")
        sx_hash = "0x" + format(ident + 100, "064x")
        self.markets[market_id] = {
            "id": market_id, "conditionId": condition,
            "outcomes": '["Team A", "Team B"]',
            "clobTokenIds": json.dumps([token_id, str(int(token_id) + 1)]),
            "closed": True, "outcomePrices": '["1", "0"]',
        }
        self.tokens[token_id] = {
            "token_id": token_id, "condition_id": condition,
            "module": "v1_ctf", "outcome_index": 0, "outcome": "Team A",
            "resolved": True, "final_price": int(poly_won), "closed": True,
        }
        self.sx[sx_hash] = {
            "marketHash": sx_hash, "status": "INACTIVE",
            "outcomeOneName": "Team A", "outcomeTwoName": "Team B",
            "outcomeVoidName": "NO_CONTEST", "reportedDate": NOW - 60,
            "outcome": 2 if sx_won else 1,
        }
        return market_id, token_id, sx_hash

    def get(self, url, params=None, timeout=4):
        assert 0 < timeout <= results.REQUEST_TIMEOUT
        self.calls.append((url, params))
        if url.startswith(results.GAMMA):
            payload = self.markets[url[len(results.GAMMA):]]
        elif url == results.POLY_TOKENS:
            payload = {"data": [self.tokens[params["token_id"]]]}
        elif url == results.SX_MARKETS:
            payload = {"status": "success", "data": [self.sx[params["marketHashes"]]]}
        else:
            raise AssertionError("Unexpected endpoint: " + url)
        return copy.deepcopy(payload)


class OfficialResultsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = paper.open_database(str(Path(self.directory.name) / "paper.sqlite3"))
        self.api = ResultFixtures()

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def add_trade(self, ident=101, *, poly_won=True, sx_won=False,
                  legacy=False, status="OPEN_CONDITIONAL", profit=500):
        market, token, sx_hash = self.api.add(ident, poly_won, sx_won)
        with self.db:
            self.db.execute("""INSERT INTO paper_v12_trades(
              id,event_key,created_epoch,sport,event,poly_market,sx_market,
              poly_side,sx_side,stage,payout_usdc,fx_eur_per_usd,
              poly_debit_cents,sx_debit_cents,poly_fee_cents,
              conditional_payout_cents,conditional_profit_cents,status,note,
              game_start_epoch,poly_token_id,sx_outcome_one)
              VALUES(?,?,?,'basketball','Team A vs Team B',?,?,'Team A','Team B',
                'PRE_JOGO',95,1,4500,4500,0,9500,?,?,?, ?,?,?)
            """, (ident, "fixture-" + str(ident), NOW - 7200, market, sx_hash,
                  profit, status, "Synthetic fixture; virtual funds only.",
                  NOW - 3600, None if legacy else token, None if legacy else 0))
            self.db.execute("UPDATE paper_v12_wallets SET free_cents=free_cents-4500")
        return market, token, sx_hash

    def state(self, ident=101):
        return self.db.execute("SELECT status FROM paper_v12_trades WHERE id=?",
                               (ident,)).fetchone()[0]

    def balances(self):
        return paper.free_balances(self.db)

    def run_pass(self, now=NOW, limit=10, get=None):
        return results.run_settlement_pass(self.db, get_json=get or self.api.get,
                                           now_epoch=now, limit=limit)

    def test_public_probe_checks_sources_without_creating_operations(self):
        self.api.add(101)
        def fetch(url, params=None, timeout=4):
            if url == results.GAMMA.rstrip("/"):
                return list(self.api.markets.values())
            if url == "https://api.sx.bet/markets/active":
                return {"data": {"markets": list(self.api.sx.values())}}
            return self.api.get(url, params, timeout)
        before = self.balances()
        with patch.object(results.time, "time", return_value=NOW):
            report = results.probe_public_sources(fetch)
        self.assertEqual(report["polymarket"], "OK")
        self.assertEqual(report["sx_bet"], "OK")
        self.assertTrue(report["poly_sample_resolved"])
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)

    def test_public_probe_timeout_is_diagnostic_and_never_a_result(self):
        def unavailable(*args, **kwargs):
            raise TimeoutError()
        report = results.probe_public_sources(unavailable)
        self.assertEqual(report, {"polymarket": "PENDENTE:TimeoutError", "sx_bet": "PENDENTE:TimeoutError"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0], 0)

    def test_closed_and_price_one_do_not_prove_poly_resolution(self):
        _, token, _ = self.add_trade()
        self.api.tokens[token]["resolved"] = False
        before = self.balances()
        count = self.run_pass()
        self.assertEqual(count["pending"], 1)
        self.assertEqual(count["settled"], 0)
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.state(), "OPEN_CONDITIONAL")

    def test_official_results_credit_only_virtual_winning_leg_once(self):
        self.add_trade()
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(self.state(), "SETTLED")
        self.assertEqual(self.balances(), {"poly": before["poly"] + 9500, "sx": before["sx"]})
        count_calls = len(self.api.calls)
        after = self.balances()
        self.assertEqual(self.run_pass(now=NOW + 120)["checked"], 0)
        self.assertEqual(len(self.api.calls), count_calls)
        self.assertEqual(self.balances(), after)

    def test_independent_outcomes_can_both_win_or_both_lose(self):
        for ident, poly_won, sx_won in [(101, True, True), (102, False, False)]:
            self.add_trade(ident, poly_won=poly_won, sx_won=sx_won)
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 2)
        self.assertEqual(self.balances(), {key: value + 9500 for key, value in before.items()})
        profits = dict(self.db.execute("SELECT id,realized_profit_cents FROM paper_v12_trades"))
        self.assertEqual(profits, {101: 10000, 102: -9000})

    def test_sx_void_keeps_funds_locked_and_saves_report_evidence(self):
        _, _, sx_hash = self.add_trade()
        self.api.sx[sx_hash]["outcome"] = 0
        before = self.balances()
        self.assertEqual(self.run_pass()["review"], 1)
        self.assertEqual(self.state(), "REVIEW_REQUIRED")
        self.assertEqual(self.balances(), before)
        evidence = json.loads(self.db.execute(
            "SELECT evidence_json FROM paper_v13_result_checks WHERE trade_id=101"
        ).fetchone()[0])
        self.assertEqual(evidence["sx"]["outcome"], 0)

    def test_poly_fractional_payout_requires_review(self):
        _, token, _ = self.add_trade()
        self.api.tokens[token]["final_price"] = 0.5
        before = self.balances()
        self.assertEqual(self.run_pass()["review"], 1)
        self.assertEqual(self.balances(), before)

    def test_source_market_mismatch_never_credits(self):
        market, _, _ = self.add_trade()
        self.api.markets[market]["id"] = "999"
        before = self.balances()
        self.assertEqual(self.run_pass()["review"], 1)
        self.assertEqual(self.balances(), before)

    def test_token_condition_mismatch_never_credits(self):
        _, token, _ = self.add_trade()
        self.api.tokens[token]["condition_id"] = "0x" + "f" * 64
        before = self.balances()
        self.assertEqual(self.run_pass()["review"], 1)
        self.assertEqual(self.balances(), before)

    def test_inactive_sx_without_report_is_pending(self):
        _, _, sx_hash = self.add_trade()
        self.api.sx[sx_hash].pop("reportedDate")
        before = self.balances()
        self.assertEqual(self.run_pass()["pending"], 1)
        self.assertEqual(self.state(), "OPEN_CONDITIONAL")
        self.assertEqual(self.balances(), before)

    def test_active_sx_with_official_report_uses_report_not_status(self):
        _, _, sx_hash = self.add_trade()
        self.api.sx[sx_hash]["status"] = "ACTIVE"
        self.assertEqual(self.run_pass()["settled"], 1)

    def test_legacy_identity_is_recovered_only_from_exact_unique_labels(self):
        _, token, _ = self.add_trade(legacy=True)
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(tuple(self.db.execute(
            "SELECT poly_token_id,sx_outcome_one FROM paper_v12_trades WHERE id=101"
        ).fetchone()), (token, 0))

    def test_legacy_fuzzy_label_is_rejected(self):
        market, _, _ = self.add_trade(legacy=True)
        self.api.markets[market]["outcomes"] = '["Team A Club", "Team B"]'
        self.assertEqual(self.run_pass()["review"], 1)

    def test_review_rows_and_suspicious_old_margin_are_excluded(self):
        self.add_trade(101, status="REVIEW_REQUIRED")
        self.add_trade(102, profit=900)
        before = self.balances()
        self.assertEqual(self.run_pass()["checked"], 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.balances(), before)

    def test_bounded_round_robin_does_not_starve_later_rows(self):
        for ident in (101, 102, 103):
            _, token, _ = self.add_trade(ident)
            self.api.tokens[token]["resolved"] = False
        for _ in range(3):
            self.assertEqual(self.run_pass(limit=1)["checked"], 1)
        checked_ids = [r[0] for r in self.db.execute(
            "SELECT trade_id FROM paper_v13_result_checks ORDER BY trade_id")]
        self.assertEqual(checked_ids, [101, 102, 103])
        self.assertEqual(self.run_pass(limit=1)["checked"], 0)

    def test_network_failure_is_retryable_and_never_sets_final_result(self):
        self.add_trade()
        before = self.balances()
        def fail(*args, **kwargs):
            raise TimeoutError("offline fixture")
        count = self.run_pass(get=fail)
        self.assertEqual((count["errors"], count["pending"]), (1, 1))
        self.assertEqual(self.state(), "OPEN_CONDITIONAL")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.run_pass(now=NOW + 61)["settled"], 1)

    def test_pass_deadline_stops_requests(self):
        self.add_trade()
        with patch.object(results.time, "monotonic", side_effect=[0, 0, 46]):
            count = self.run_pass()
        self.assertEqual((count["checked"], count["pending"]), (1, 1))
        self.assertEqual(self.api.calls, [])

    def test_duplicate_official_token_records_are_ambiguous(self):
        self.add_trade()
        before = self.balances()
        def duplicate(url, params=None, timeout=4):
            response = self.api.get(url, params, timeout)
            if url == results.POLY_TOKENS:
                response["data"] *= 2
            return response
        self.assertEqual(self.run_pass(get=duplicate)["review"], 1)
        self.assertEqual(self.balances(), before)

    def test_bool_is_not_a_documented_sx_outcome_enum(self):
        _, _, sx_hash = self.add_trade()
        self.api.sx[sx_hash]["outcome"] = True
        self.assertEqual(self.run_pass()["review"], 1)

    def test_report_that_arrives_during_the_pass_is_not_future_rejected(self):
        _, _, sx_hash = self.add_trade()
        self.api.sx[sx_hash]["reportedDate"] = NOW + 1
        self.assertEqual(self.run_pass()["settled"], 1)

    def test_v2_uses_position_ids_even_when_ctf_ids_are_also_present(self):
        market, token, _ = self.add_trade()
        self.api.markets[market].update(
            version="v2", positionIds=[token, str(int(token) + 1)],
            clobTokenIds='["999001", "999002"]',
        )
        self.api.tokens[token]["module"] = "binary"
        self.assertEqual(self.run_pass()["settled"], 1)
        requests = [params for url, params in self.api.calls if url == results.POLY_TOKENS]
        self.assertEqual(requests, [{"token_id": token}])

    def test_v2_carried_over_condition_requires_explicit_official_alias(self):
        market, token, _ = self.add_trade()
        structural = "0x" + "e" * 64
        self.api.markets[market].update(
            version="v2", positionIds=[token, str(int(token) + 1)],
            clobTokenIds=None, conditionId=structural,
        )
        self.api.tokens[token].update(module="neg_risk", structural_condition_id=structural)
        self.assertEqual(self.run_pass()["settled"], 1)

    def test_v2_missing_position_ids_waits_without_falling_back_to_ctf(self):
        market, _, _ = self.add_trade()
        self.api.markets[market]["version"] = "v2"
        before = self.balances()
        self.assertEqual(self.run_pass()["pending"], 1)
        self.assertEqual(self.state(), "OPEN_CONDITIONAL")
        self.assertEqual(self.balances(), before)
        self.assertFalse(any(url == results.POLY_TOKENS for url, _ in self.api.calls))

    def test_missing_dynamic_fields_are_retryable_without_review_or_credit(self):
        for ident, source, key in [
            (101, "token", "resolved"), (102, "token", "final_price"),
            (103, "token", "outcome"), (104, "metadata", "conditionId"),
            (105, "metadata", "outcomes"), (106, "metadata", "clobTokenIds"),
            (107, "metadata", "id"), (108, "sx", "outcomeOneName"),
        ]:
            market, token, sx_hash = self.add_trade(ident)
            data = {"metadata": self.api.markets[market],
                    "token": self.api.tokens[token], "sx": self.api.sx[sx_hash]}[source]
            saved = data.pop(key)
            before = self.balances()
            with self.subTest(source=source, missing=key):
                counts = self.run_pass(limit=1)
                self.assertEqual((counts["pending"], counts["review"]), (1, 0))
                self.assertEqual(self.state(ident), "OPEN_CONDITIONAL")
                self.assertEqual(self.balances(), before)
            data[key] = saved
        self.assertEqual(self.run_pass(now=NOW + 61)["settled"], 8)

    def test_http_404_is_retryable_then_can_settle(self):
        self.add_trade()
        def not_found(url, **kwargs):
            raise HTTPError(url, 404, "not found", {}, None)
        before = self.balances()
        self.assertEqual(self.run_pass(get=not_found)["pending"], 1)
        self.assertEqual(self.state(), "OPEN_CONDITIONAL")
        self.assertEqual(self.balances(), before)
        self.assertEqual(self.run_pass(now=NOW + 61)["settled"], 1)


if __name__ == "__main__":
    unittest.main()
