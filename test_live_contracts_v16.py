"""V16 LIVE contract checks, with public API fixtures and no network writes."""
from __future__ import annotations

import copy
import threading
import unittest

import live_market_v13 as live
from live_candidates_v13 import LiveCandidates
from test_live_candidates_v13 import Clock, ManualExecutor, proof
from test_live_market_v13 import FakeClock
from test_market_contracts_v16 import build_hockey_fixture, build_soccer_fixture


NOW = 1_800_000_000


class ContractLiveFixtures:
    def __init__(self, build=build_soccer_fixture, **kwargs):
        self.pair, self.market, self.sx_market = build(now=NOW - 7200, **kwargs)
        self.market["secondsDelay"] = 3
        self.event = {
            "id": self.pair["poly"]["event_id"], "live": True, "ended": False,
            "gameStatus": "InProgress", "period": "2H", "score": "0-1",
            "markets": [self.market],
        }
        self.sx_response = {"status": "success", "data": [self.sx_market]}
        self.fixture = {"status": 2}
        self.fixtures_response = {
            "status": "success", "data": {self.pair["sx"]["event_id"]: self.fixture},
        }
        self.metadata = {"status": "success", "data": {"bettingDelay": {
            "liveMsDefault": 8000, "pregameMsDefault": 0,
            "sportIdToMs": {}, "leagueIdToMs": {},
        }}}
        self.calls = []
        self.lock = threading.Lock()

    def get(self, url, params=None, timeout=None):
        assert 0 < timeout <= live.REQUEST_TIMEOUT
        with self.lock:
            self.calls.append((url, params))
        if url == live.GAMMA_EVENTS + self.pair["poly"]["event_id"] and params is None:
            payload = self.event
        elif url == live.SX_MARKETS and params == {"marketHashes": self.pair["sx"]["hash"]}:
            payload = self.sx_response
        elif url == live.SX_FIXTURES and params == {"sportXEventIds": self.pair["sx"]["event_id"]}:
            payload = self.fixtures_response
        elif url == live.SX_METADATA and params is None:
            payload = self.metadata
        else:
            raise AssertionError("Unexpected public GET: " + url)
        return copy.deepcopy(payload)


class LiveContractTests(unittest.TestCase):
    def snapshot(self, api):
        clock = FakeClock(epoch=NOW)
        return live.fetch_live_snapshot(api.pair, get_json=api.get,
                                       clock=clock.monotonic, wall=clock.time)

    def rejected(self, api, reason):
        with self.assertRaises(live.LiveCheckError) as caught:
            self.snapshot(api)
        self.assertEqual(caught.exception.reason, reason)

    def test_soccer_no_and_draw_contracts_preserve_raw_sides_and_digest(self):
        for predicate in ("home", "draw", "away"):
            with self.subTest(predicate=predicate):
                api = ContractLiveFixtures(predicate=predicate)
                evidence = self.snapshot(api)
                self.assertEqual(evidence["contract_digest"], api.pair["settlement_contract"]["digest"])
                self.assertEqual(evidence["poly_side"], "No")
                self.assertEqual(evidence["sx_side"], api.pair["sx_label"])
                self.assertEqual(evidence["sx_target"], 0)
                self.assertEqual(evidence["required_delay_ms"], 8000)
                self.assertEqual(len(api.calls), 4)

    def test_hockey_overtime_and_shootout_remain_inside_full_game_scope(self):
        for period, status in (("OT", "InProgress"), ("PenaltyShootout", "PenaltyShootout")):
            with self.subTest(period=period):
                api = ContractLiveFixtures(build_hockey_fixture)
                api.event.update(period=period, gameStatus=status)
                evidence = self.snapshot(api)
                self.assertEqual(evidence["contract_digest"], api.pair["settlement_contract"]["digest"])
                self.assertEqual(evidence["sx_target"], 1)

    def test_missing_or_changed_certificate_is_rejected_before_any_http(self):
        for mutation in ("missing", "digest", "sport", "selection"):
            with self.subTest(mutation=mutation):
                api = ContractLiveFixtures()
                if mutation == "missing":
                    api.pair.pop("settlement_contract")
                elif mutation == "digest":
                    api.pair["settlement_contract"]["digest"] = "0" * 64
                elif mutation == "sport":
                    api.pair["poly"]["sport"] = "basketball"
                else:
                    api.pair["poly_label"] = "Yes"
                self.rejected(api, "settlement_contract_invalid")
                self.assertEqual(api.calls, [])

    def test_changed_poly_rules_are_not_reused_from_catalogue(self):
        api = ContractLiveFixtures()
        api.market["description"] += " Extra time also counts."
        self.rejected(api, "poly_settlement_contract_changed")

    def test_changed_sx_market_scope_or_negative_label_invalidates_live(self):
        for mutation in ({"type": 226}, {"outcomeTwoName": "Brentford FC"},
                         {"outcomeVoidName": "VOID"}, {"gameTime": NOW}):
            with self.subTest(mutation=mutation):
                api = ContractLiveFixtures()
                api.sx_market.update(mutation)
                self.rejected(api, "sx_settlement_contract_changed")

    def test_soccer_regulation_does_not_extend_to_extra_time_or_penalties(self):
        for period in ("ET", "ET 1", "1st ET", "ET HT", "E2", "OT", "AET",
                       "Extra Time First Half", "Overtime", "PEN", "SO", "Penalty shootout"):
            with self.subTest(period=period):
                api = ContractLiveFixtures()
                api.event["period"] = period
                self.rejected(api, "poly_scope_period_not_live")
        api = ContractLiveFixtures()
        api.event.update(period=None, gameStatus="PenaltyShootout")
        self.rejected(api, "poly_scope_period_not_live")

    def test_soccer_explicit_regulation_periods_can_be_live(self):
        for period in ("1H", "HT", "2H", "First Half", "Second Half", "Half Time"):
            with self.subTest(period=period):
                api = ContractLiveFixtures()
                api.event["period"] = period
                self.assertEqual(self.snapshot(api)["period"], period)

    def test_soccer_missing_unknown_or_minute_only_period_cannot_prove_regulation(self):
        for period in (None, "", "unknown", "InProgress", "90+4", 90, True):
            with self.subTest(period=period):
                api = ContractLiveFixtures()
                api.event["period"] = period
                self.rejected(api, "poly_scope_period_not_live")
        api = ContractLiveFixtures()
        api.event.pop("period")
        self.rejected(api, "poly_scope_period_not_live")


class LiveQueueContractTests(unittest.TestCase):
    def setUp(self):
        self.pair, _, _ = build_soccer_fixture(now=NOW - 7200)
        self.clock, self.executor = Clock(), ManualExecutor()
        self.proof_mutation = None
        self.queue = LiveCandidates(self.source, clock=self.clock, executor=self.executor)
        self.addCleanup(self.queue.stop)

    def source(self, pair):
        evidence = proof(pair, self.clock(), delay=1000)
        evidence["contract_digest"] = pair["settlement_contract"]["digest"]
        if self.proof_mutation:
            self.proof_mutation(evidence)
        return evidence

    def initial(self):
        self.assertEqual(self.queue.consider(self.pair), (None, "live_em_verificacao"))
        self.executor.run(0)
        self.queue.tick()

    def ready(self):
        self.initial()
        self.clock.advance(1)
        self.queue.tick()
        self.executor.run(1)
        self.queue.tick()

    def test_digest_survives_both_fresh_checks_and_delay_wait(self):
        self.ready()
        evidence, reason = self.queue.consider(self.pair)
        self.assertEqual(reason, "live_confirmado")
        self.assertEqual(evidence["contract_digest"], self.pair["settlement_contract"]["digest"])
        self.assertEqual(evidence["poly_side"], "No")
        self.assertIs(evidence["verified_after_delay"], True)
        self.assertGreaterEqual(evidence["checks_started_mono"], evidence["delay_ready_mono"])
        self.assertEqual(len(self.executor.jobs), 2)

    def test_missing_digest_rejects_initial_proof(self):
        self.proof_mutation = lambda evidence: evidence.pop("contract_digest")
        self.initial()
        self.assertEqual(self.queue.consider(self.pair), (None, "live_contrato_invalido"))

    def test_wrong_digest_rejects_final_proof_without_reusing_initial_one(self):
        self.initial()
        self.proof_mutation = lambda evidence: evidence.update(contract_digest="0" * 64)
        self.clock.advance(1)
        self.queue.tick()
        self.executor.run(1)
        self.queue.tick()
        self.assertEqual(self.queue.consider(self.pair), (None, "live_contrato_invalido"))

    def test_top_level_contract_change_invalidates_ready_identity(self):
        self.ready()
        changed = copy.deepcopy(self.pair)
        changed["settlement_contract"]["digest"] = "0" * 64
        self.queue.rebuild([changed])
        self.assertEqual(self.queue.ready_deadlines(), {})
        self.assertEqual(self.queue.take_ready_events(), {})
        self.assertEqual(self.queue.consider(self.pair), (None, "live_par_desatualizado"))


if __name__ == "__main__":
    unittest.main()
