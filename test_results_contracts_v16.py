"""V16 official-result reconciliation using an in-memory virtual ledger only."""
from __future__ import annotations

import copy
import json
import unittest

import paper_results_v13 as results
import paper_v12 as paper
from test_market_contracts_v16 import build_hockey_fixture, build_soccer_fixture


NOW = 1_800_000_000


class ContractResultFixtures:
    def __init__(self, build=build_soccer_fixture, *, poly_won=True, sx_won=False, **kwargs):
        self.pair, self.market, self.sx_market = build(now=NOW - 7200, **kwargs)
        self.market.update(closed=True, acceptingOrders=False)
        self.token = {
            "token_id": self.pair["poly_asset"], "condition_id": self.market["conditionId"],
            "module": "v1_ctf", "outcome": self.pair["poly_label"],
            "outcome_index": self.pair["settlement_contract"]["poly"]["selection_index"],
            "resolved": True, "final_price": int(poly_won),
        }
        selected_outcome = self.pair["sx_target"] + 1
        self.sx_market.update(status="INACTIVE", reportedDate=NOW - 30,
                              outcome=selected_outcome if sx_won else 3 - selected_outcome)
        self.fixture = {"status": 3}
        self.fixtures_response = {
            "status": "success", "data": {self.pair["sx"]["event_id"]: self.fixture},
        }
        self.calls = []

    def get(self, url, params=None, timeout=4):
        assert 0 < timeout <= results.REQUEST_TIMEOUT
        self.calls.append((url, params))
        if url == results.GAMMA + self.pair["poly"]["id"] and params is None:
            payload = self.market
        elif url == results.POLY_TOKENS and params == {"token_id": self.pair["poly_asset"]}:
            payload = {"data": [self.token]}
        elif url == results.SX_MARKETS and params == {"marketHashes": self.pair["sx"]["hash"]}:
            payload = {"status": "success", "data": [self.sx_market]}
        elif url == results.SX_FIXTURES and params == {"sportXEventIds": self.pair["sx"]["event_id"]}:
            payload = self.fixtures_response
        else:
            raise AssertionError("Unexpected public GET: " + url)
        return copy.deepcopy(payload)


class ContractResultsTests(unittest.TestCase):
    def setUp(self):
        self.db = None
        self.new_case()

    def tearDown(self):
        self.db.close()

    def new_case(self, build=build_soccer_fixture, **kwargs):
        if self.db is not None:
            self.db.close()
        self.db = paper.open_database(":memory:")
        self.api = ContractResultFixtures(build, **kwargs)
        self.quote = paper.quote_for_payout([(0.47, 500)], [(0.48, 500)], 100, 1)
        self.assertIsNotNone(self.quote)
        self.assertGreater(self.quote.roi, 0)
        self.assertLess(self.quote.roi, paper.MAX_AUTO_PAPER_ROI)
        self.trade_id = paper.book_shadow_trade(
            self.db, self.api.pair, self.quote, "PRE_JOGO", now_epoch=NOW - 7200,
        )
        self.assertIsNotNone(self.trade_id)

    def run_pass(self, now=NOW, get=None):
        return results.run_settlement_pass(self.db, get_json=get or self.api.get, now_epoch=now)

    def balances(self):
        return paper.free_balances(self.db)

    def record(self):
        return dict(paper.trade_details(self.db, self.trade_id))

    def check(self):
        row = self.db.execute("SELECT verdict,reason,evidence_json FROM paper_v13_result_checks "
                              "WHERE trade_id=?", (self.trade_id,)).fetchone()
        return row[0], row[1], json.loads(row[2])

    def assert_review_unchanged(self, expected_reason):
        before = self.balances()
        count = self.run_pass()
        self.assertEqual((count["review"], count["settled"], count["errors"]), (1, 0, 0))
        self.assertEqual(self.record()["status"], "REVIEW_REQUIRED")
        self.assertEqual(self.balances(), before)
        verdict, reason, evidence = self.check()
        self.assertEqual((verdict, reason), ("review", expected_reason))
        return evidence

    def test_soccer_no_wins_on_draw_and_keeps_raw_token_label(self):
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(self.balances(), {"poly": before["poly"] + self.quote.payout_eur_cents,
                                          "sx": before["sx"]})
        self.assertEqual(self.record()["poly_side"], "No")
        verdict, _, evidence = self.check()
        self.assertEqual(verdict, "settled")
        self.assertEqual(evidence["poly"]["outcome_label"], "No")
        self.assertEqual(evidence["poly"]["token_id"], "102")
        self.assertEqual(evidence["contract_digest"], self.api.pair["settlement_contract"]["digest"])
        self.assertEqual(evidence["fixture"]["source_url"], results.SX_FIXTURES)
        self.assertEqual(evidence["fixture"]["status"], 3)
        self.assertEqual(len(self.api.calls), 4)

    def test_soccer_yes_draw_predicate_is_a_binary_draw_contract(self):
        self.new_case(predicate="draw", poly_index=0, sx_target=1)
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(self.balances()["poly"], before["poly"] + self.quote.payout_eur_cents)
        self.assertEqual(self.check()[2]["poly"]["outcome_label"], "Yes")
        self.assertEqual(self.check()[2]["sx"]["outcome_label"], "Not tie")

    def test_soccer_negative_sx_outcome_two_includes_draw_and_wins_correctly(self):
        self.new_case(poly_index=0, sx_target=1, poly_won=False, sx_won=True)
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(self.balances(), {"poly": before["poly"],
                                          "sx": before["sx"] + self.quote.payout_eur_cents})
        evidence = self.check()[2]
        self.assertIs(evidence["sx"]["outcome_one"], False)
        self.assertEqual(evidence["sx"]["outcome"], 2)
        self.assertIs(evidence["sx"]["won"], True)

    def test_hockey_second_sx_side_wins_and_payout_is_idempotent(self):
        self.new_case(build_hockey_fixture, poly_won=False, sx_won=True)
        before = self.balances()
        self.assertEqual(self.run_pass()["settled"], 1)
        self.assertEqual(self.balances(), {"poly": before["poly"],
                                          "sx": before["sx"] + self.quote.payout_eur_cents})
        after, calls = self.balances(), len(self.api.calls)
        self.assertEqual(self.run_pass(now=NOW + 120)["checked"], 0)
        self.assertEqual(self.balances(), after)
        self.assertEqual(len(self.api.calls), calls)
        self.assertIs(self.check()[2]["sx"]["outcome_one"], False)

    def test_both_winning_certified_legs_require_review_with_both_proofs(self):
        self.new_case(poly_won=True, sx_won=True)
        evidence = self.assert_review_unchanged("contract_results_not_complementary")
        self.assertIs(evidence["poly"]["won"], True)
        self.assertIs(evidence["sx"]["won"], True)

    def test_both_losing_certified_legs_require_review_with_both_proofs(self):
        self.new_case(poly_won=False, sx_won=False)
        evidence = self.assert_review_unchanged("contract_results_not_complementary")
        self.assertIs(evidence["poly"]["won"], False)
        self.assertIs(evidence["sx"]["won"], False)

    def test_observed_exception_is_preserved_before_payout_and_never_auto_cleared(self):
        for status, label in ((4, "cancelled"), (5, "postponed"),
                              (6, "interrupted"), (7, "abandoned")):
            with self.subTest(status=status):
                self.new_case()
                self.api.fixture["status"] = status
                self.api.token["resolved"] = False
                self.api.sx_market["outcome"] = None
                evidence = self.assert_review_unchanged("sx_fixture_exception_" + label)
                self.assertEqual(evidence["fixture"]["status"], status)
                self.assertEqual(len(self.api.calls), 1)
                self.api.fixture["status"] = 3
                self.api.token["resolved"] = True
                self.api.sx_market["outcome"] = 2
                balances, calls = self.balances(), len(self.api.calls)
                self.assertEqual(self.run_pass(now=NOW + 120)["checked"], 0)
                self.assertEqual(self.record()["status"], "REVIEW_REQUIRED")
                self.assertEqual(self.balances(), balances)
                self.assertEqual(len(self.api.calls), calls)
                self.assertEqual(self.check()[2]["fixture"]["status"], status)

    def test_unknown_unfinished_or_lost_coverage_fixture_never_credits(self):
        for status in (None, True, "3", 3.0, 0, 1, 2, 8, 9, 99):
            with self.subTest(status=status):
                self.new_case()
                self.api.fixture["status"] = status
                before = self.balances()
                counts = self.run_pass()
                self.assertEqual((counts["pending"], counts["review"], counts["settled"], counts["errors"]),
                                 (1, 0, 0, 0))
                self.assertEqual(self.record()["status"], "OPEN_CONDITIONAL")
                self.assertEqual(self.balances(), before)
                self.assertEqual(self.check()[2]["fixture"]["status"], status)

    def test_missing_fixture_is_pending_then_retries_without_inventing_result(self):
        self.api.fixtures_response["data"] = {}
        before = self.balances()
        self.assertEqual(self.run_pass()["pending"], 1)
        self.assertEqual(self.balances(), before)
        self.api.fixtures_response["data"] = {self.api.pair["sx"]["event_id"]: self.api.fixture}
        self.assertEqual(self.run_pass(now=NOW + 61)["settled"], 1)

    def test_fixture_event_mismatch_is_reviewed(self):
        self.api.fixture["sportXeventId"] = "L999"
        evidence = self.assert_review_unchanged("sx_fixture_event_mismatch")
        self.assertEqual(evidence["fixture"]["reported_event_id"], "L999")

    def test_changed_poly_rules_are_preserved_as_review_evidence(self):
        self.api.market["description"] += " Including extra time."
        evidence = self.assert_review_unchanged("poly_settlement_contract_changed")
        self.assertEqual(evidence["poly"]["observed_source_fields"]["description"],
                         self.api.market["description"])
        self.assertEqual(evidence["poly"]["expected_source_fingerprint"],
                         self.api.pair["settlement_contract"]["poly"]["source_fingerprint"])

    def test_changed_sx_scope_is_preserved_as_review_evidence(self):
        self.api.sx_market["type"] = 226
        evidence = self.assert_review_unchanged("sx_settlement_contract_changed")
        self.assertEqual(evidence["sx"]["observed_source_fields"]["type"], 226)
        self.assertIs(evidence["poly"]["resolved"], True)

    def test_missing_malformed_or_tampered_certificate_never_uses_legacy_path(self):
        for value in (None, "", "{}", "not JSON"):
            with self.subTest(value=value):
                self.new_case()
                with self.db:
                    self.db.execute("UPDATE paper_v12_trades SET contract_evidence_json=? WHERE id=?",
                                    (value, self.trade_id))
                self.assert_review_unchanged("settlement_contract_invalid")
                self.assertEqual(self.api.calls, [])
        self.new_case()
        changed = copy.deepcopy(self.api.pair["settlement_contract"])
        changed["digest"] = "0" * 64
        with self.db:
            self.db.execute("UPDATE paper_v12_trades SET contract_evidence_json=? WHERE id=?",
                            (json.dumps(changed), self.trade_id))
        self.assert_review_unchanged("settlement_contract_invalid")
        self.assertEqual(self.api.calls, [])

    def test_fractional_poly_payout_requires_review_including_reported_amount(self):
        self.new_case(build_hockey_fixture)
        self.api.token["final_price"] = 0.5
        evidence = self.assert_review_unchanged("poly_fractional_void_or_split_payout")
        self.assertEqual(evidence["poly"]["payout_rate"], 0.5)

    def test_sx_void_requires_review_including_official_void_report(self):
        self.api.sx_market["outcome"] = 0
        evidence = self.assert_review_unchanged("sx_void_or_refund_requires_review")
        self.assertEqual(evidence["sx"]["outcome"], 0)

    def test_finished_fixture_does_not_replace_missing_onchain_payout(self):
        self.api.token["resolved"] = False
        before = self.balances()
        self.assertEqual(self.run_pass()["pending"], 1)
        self.assertEqual(self.balances(), before)
        self.assertIs(self.check()[2]["poly"]["resolved"], False)


if __name__ == "__main__":
    unittest.main()
