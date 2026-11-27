"""Offline checks of the four public schemas; no orders or network access."""
from __future__ import annotations

import copy
import json
import re
import threading
import unittest
from unittest.mock import patch

import live_market_v13 as live


NOW = 1_800_000_000


class FakeClock:
    def __init__(self, started=100.0, checked=100.4, epoch=NOW):
        self.started, self.checked, self.epoch = started, checked, epoch
        self.calls = 0

    def monotonic(self):
        self.calls += 1
        return self.started if self.calls == 1 else self.checked

    def time(self):
        return self.epoch


class Fixtures:
    def __init__(self):
        self.pair = {
            "key": "11:0x" + "ab" * 32 + ":0",
            "poly": {
                "id": "11", "event_id": "101", "version": "v1",
                "type": "moneyline", "sport": "basketball",
                "outcomes": ["Team A", "Team B"], "assets": ["1001", "1002"],
            },
            "sx": {
                "hash": "0x" + "ab" * 32, "event_id": "L111",
                "type": 226, "sport_id": 3, "league_id": 29,
                "teams": ("Team A", "Team B"), "outcomes": ("Team A", "Team B"),
                "sport": "basketball", "start": NOW - 3600, "live_enabled": True,
            },
            "poly_asset": "1001", "poly_label": "Team A",
            "sx_target": 1, "sx_label": "Team B",
        }
        self.market = {
            "id": "11", "sportsMarketType": "moneyline", "version": "v1",
            "active": True, "closed": False, "acceptingOrders": True,
            "outcomes": '["Team A", "Team B"]',
            "clobTokenIds": '["1001", "1002"]', "secondsDelay": 3,
        }
        self.event = {
            "id": "101", "live": True, "ended": False, "gameStatus": "InProgress",
            "score": "48-51", "period": "Q3", "markets": [self.market],
        }
        self.sx_market = {
            "marketHash": self.pair["sx"]["hash"], "sportXeventId": "L111",
            "type": 226, "sportId": 3, "leagueId": 29, "gameTime": NOW - 3600,
            "status": "ACTIVE", "liveEnabled": True,
            "outcomeOneName": "Team A", "outcomeTwoName": "Team B",
            "teamOneName": "Team A", "teamTwoName": "Team B",
            "outcome": None, "reportedDate": None,
        }
        self.sx_response = {"status": "success", "data": [self.sx_market]}
        self.fixture = {"status": 2}
        self.fixtures_response = {"status": "success", "data": {"L111": self.fixture}}
        self.delay = {
            "liveMsDefault": 8000, "pregameMsDefault": 0,
            "sportIdToMs": {}, "leagueIdToMs": {},
        }
        self.metadata = {"status": "success", "data": {"bettingDelay": self.delay}}
        self.calls = []
        self.lock = threading.Lock()
        self.barrier = None
        self.failure = None

    def get(self, url, params=None, timeout=None):
        if not 0 < timeout <= 2.0:
            raise AssertionError("Public source timeout must be bounded to two seconds")
        with self.lock:
            self.calls.append((url, params, timeout, threading.get_ident()))
        if self.barrier:
            self.barrier.wait(timeout=1)
        if self.failure:
            raise self.failure
        if url == live.GAMMA_EVENTS + "101" and params is None:
            return copy.deepcopy(self.event)
        if url == live.SX_MARKETS and params == {"marketHashes": self.pair["sx"]["hash"]}:
            return copy.deepcopy(self.sx_response)
        if url == live.SX_FIXTURES and params == {"sportXEventIds": "L111"}:
            return copy.deepcopy(self.fixtures_response)
        if url == live.SX_METADATA and params is None:
            return copy.deepcopy(self.metadata)
        raise AssertionError("Unexpected public GET")


class LiveMarketTests(unittest.TestCase):
    def setUp(self):
        self.api = Fixtures()
        self.clock = FakeClock()

    def snapshot(self, api=None, clock=None):
        api, clock = api or self.api, clock or self.clock
        return live.fetch_live_snapshot(api.pair, get_json=api.get,
                                        clock=clock.monotonic, wall=clock.time)

    def rejected(self, reason=None, *, api=None, clock=None):
        with self.assertRaises(live.LiveCheckError) as caught:
            self.snapshot(api, clock)
        self.assertRegex(caught.exception.reason, r"^[a-z0-9_]+$")
        self.assertEqual(str(caught.exception), caught.exception.reason)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)

    def test_nominal_contract_and_four_parallel_public_gets(self):
        self.api.barrier = threading.Barrier(4)
        snapshot = self.snapshot()
        self.assertEqual(len(self.api.calls), 4)
        self.assertEqual(len({call[3] for call in self.api.calls}), 4)
        self.assertTrue(all(call[2] == 2.0 for call in self.api.calls))
        self.assertEqual(snapshot, {
            "method": "official_live_v1", "pair_key": self.api.pair["key"],
            "poly_event_id": "101", "poly_market_id": "11", "poly_asset_id": "1001",
            "poly_side": "Team A", "sx_event_id": "L111",
            "sx_market_hash": self.api.pair["sx"]["hash"], "sx_target": 1,
            "sx_side": "Team B", "poly_live": True, "poly_ended": False,
            "poly_active": True, "poly_closed": False, "poly_accepting_orders": True,
            "sx_fixture_status": 2, "sx_status": "ACTIVE", "sx_live_enabled": True,
            "sx_reported": False, "checks_started_mono": 100.0, "checked_mono": 100.4,
            "checked_epoch": float(NOW), "valid_until_mono": 103.0,
            "sx_delay_ms": 8000, "poly_delay_ms": 3000, "required_delay_ms": 8000,
            "score": "48-51", "period": "Q3",
        })
        self.assertNotIn("verified_after_delay", snapshot)

    def test_each_fetch_retrieves_all_sources_without_cache(self):
        self.snapshot()
        self.api.fixture["status"] = 6
        self.rejected("sx_fixture_not_live", clock=FakeClock())
        self.assertEqual(len(self.api.calls), 8)

    def test_other_side_and_integer_source_ids_are_bound_exactly(self):
        self.api.event["id"] = 101
        self.api.market["id"] = 11
        self.api.pair.update(poly_label="Team B", poly_asset="1002", sx_label="Team A", sx_target=0)
        snapshot = self.snapshot()
        self.assertEqual((snapshot["poly_asset_id"], snapshot["poly_side"], snapshot["sx_target"]),
                         ("1002", "Team B", 0))

    def test_v2_uses_position_ids_even_when_ctf_ids_coexist(self):
        self.api.market.update(version="v2", positionIds=["2001", "2002"])
        self.api.pair["poly"].update(version="v2", assets=["2001", "2002"])
        self.api.pair["poly_asset"] = "2001"
        self.assertEqual(self.snapshot()["poly_asset_id"], "2001")
        for position_ids in (None, '["2001", "2002"]', ["1001", "1002"]):
            with self.subTest(position_ids=position_ids):
                self.api.market["positionIds"] = position_ids
                self.rejected(clock=FakeClock())

    def test_versions_missing_unknown_or_changed_never_fall_back(self):
        for version in (None, "", "v3", True, "v2"):
            with self.subTest(version=version):
                self.api.market["version"] = version
                self.rejected(clock=FakeClock())
        self.api.market.pop("version")
        self.rejected("poly_version_unknown", clock=FakeClock())

    def test_only_integer_fixture_status_two_is_live(self):
        for status in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, None, True, False, "2", 2.0):
            with self.subTest(status=status):
                self.api.fixture["status"] = status
                self.rejected("sx_fixture_not_live", clock=FakeClock())

    def test_live_flags_must_be_actual_booleans(self):
        for location, field, expected in (
            ("event", "live", True), ("event", "ended", False),
            ("market", "active", True), ("market", "closed", False),
            ("market", "acceptingOrders", True), ("sx_market", "liveEnabled", True),
        ):
            for bad in (None, "true", "false", 0, 1, not expected):
                with self.subTest(location=location, field=field, bad=bad):
                    api = Fixtures()
                    getattr(api, location)[field] = bad
                    self.rejected(api=api, clock=FakeClock())
            api = Fixtures()
            del getattr(api, location)[field]
            self.rejected(api=api, clock=FakeClock())

    def test_missing_catalogue_identifiers_reject_before_http(self):
        for location, field in (("poly", "event_id"), ("poly", "id"),
                                ("sx", "event_id"), ("sx", "hash"),
                                ("sx", "sport_id"), ("sx", "league_id")):
            with self.subTest(location=location, field=field):
                api = Fixtures()
                del api.pair[location][field]
                self.rejected(api=api, clock=FakeClock())
                self.assertEqual(api.calls, [])

    def test_wrong_or_duplicated_source_ids_are_rejected(self):
        for location, field, bad in (
            ("event", "id", "999"), ("market", "id", "999"),
            ("sx_market", "marketHash", "0x" + "cd" * 32),
            ("sx_market", "sportXeventId", "L999"),
            ("sx_market", "sportId", 99), ("sx_market", "leagueId", 99),
            ("fixture", "sportXeventId", "L999"),
        ):
            with self.subTest(location=location, field=field):
                api = Fixtures()
                getattr(api, location)[field] = bad
                self.rejected(api=api, clock=FakeClock())
        self.api.event["markets"].append(copy.deepcopy(self.api.market))
        self.rejected("poly_market_mismatch")
        api = Fixtures()
        api.sx_response["data"].append(copy.deepcopy(api.sx_market))
        self.rejected("sx_market_mismatch", api=api, clock=FakeClock())
        api = Fixtures()
        api.fixtures_response["data"] = {"L999": {"status": 2}}
        self.rejected("sx_fixture_missing", api=api, clock=FakeClock())

    def test_assets_and_outcome_labels_must_match_the_catalogue(self):
        changes = (
            ("market", "clobTokenIds", '["1002", "1001"]'),
            ("market", "clobTokenIds", '["1001", "1001"]'),
            ("market", "outcomes", '["Team B", "Team A"]'),
            ("market", "outcomes", '["Yes", "No"]'),
            ("market", "outcomes", '["Team A", "Team A"]'),
            ("market", "outcomes", '["Team A", "Team B", "Draw"]'),
            ("market", "outcomes", None),
            ("sx_market", "outcomeTwoName", "Team C"),
            ("sx_market", "teamOneName", "Team C"),
        )
        for location, field, bad in changes:
            with self.subTest(location=location, field=field, bad=bad):
                api = Fixtures()
                getattr(api, location)[field] = bad
                self.rejected(api=api, clock=FakeClock())

    def test_wrong_selected_side_or_token_never_passes(self):
        for field, bad in (("poly_asset", "1002"), ("poly_label", "Team B"),
                           ("sx_target", 0), ("sx_target", True),
                           ("sx_target", "1"), ("sx_target", 1.0),
                           ("sx_label", "Team A")):
            with self.subTest(field=field, bad=bad):
                api = Fixtures()
                api.pair[field] = bad
                self.rejected(api=api, clock=FakeClock())

    def test_changed_market_type_and_schedule_are_rejected(self):
        for location, field, bad in (
            ("market", "sportsMarketType", None),
            ("market", "sportsMarketType", "spread"),
            ("market", "sportsMarketType", "half_moneyline"),
            ("sx_market", "type", 52), ("sx_market", "type", "226"),
            ("sx_market", "type", 226.0), ("sx_market", "gameTime", NOW - 3500),
            ("sx_market", "status", "INACTIVE"), ("sx_market", "status", None),
            ("sx_market", "isQuarterLineMarket", True),
            ("sx_market", "legs", [{"marketHash": "other"}]),
        ):
            with self.subTest(location=location, field=field, bad=bad):
                api = Fixtures()
                getattr(api, location)[field] = bad
                self.rejected(api=api, clock=FakeClock())

    def test_poly_suspension_final_or_contradictory_state_rejects(self):
        for field, bad in (
            ("gameStatus", "Suspended"), ("gameStatus", "Final"),
            ("gameStatus", "Delayed"), ("gameStatus", "Scheduled"),
            ("gameStatus", "unknown"), ("gameStatus", 2),
            ("period", "FT"), ("period", "FT OT"), ("period", "Suspended"),
            ("active", False), ("closed", True), ("archived", "false"),
            ("finishedTimestamp", "2026-11-27T00:00:00Z"),
        ):
            with self.subTest(field=field, bad=bad):
                api = Fixtures()
                api.event[field] = bad
                self.rejected(api=api, clock=FakeClock())

    def test_known_live_sport_states_and_optional_missing_display(self):
        for state in ("InProgress", "inprogress", "running", "Break", "PenaltyShootout", None):
            with self.subTest(state=state):
                self.api.event["gameStatus"] = state
                self.assertTrue(self.snapshot(clock=FakeClock())["poly_live"])
        del self.api.event["gameStatus"]
        del self.api.event["score"]
        del self.api.event["period"]
        snapshot = self.snapshot(clock=FakeClock())
        self.assertNotIn("score", snapshot)
        self.assertNotIn("period", snapshot)

    def test_any_published_sx_result_rejects_including_void(self):
        for field in ("outcome", "reportedDate"):
            for value in (0, 1, 2, NOW - 30, "", "0", True, False):
                with self.subTest(field=field, value=value):
                    api = Fixtures()
                    api.sx_market[field] = value
                    self.rejected("sx_result_already_reported", api=api, clock=FakeClock())

    def test_delay_overrides_use_league_then_sport_then_default(self):
        self.api.delay["sportIdToMs"] = {"3": 6000}
        self.assertEqual(self.snapshot()["sx_delay_ms"], 6000)
        self.api.delay["leagueIdToMs"] = {"29": 4000}
        self.assertEqual(self.snapshot(clock=FakeClock())["sx_delay_ms"], 4000)
        self.api.delay["leagueIdToMs"]["29"] = 0
        self.assertEqual(self.snapshot(clock=FakeClock())["sx_delay_ms"], 0)
        self.assertEqual(self.snapshot(clock=FakeClock())["required_delay_ms"], 3000)
        self.api.market["secondsDelay"] = 12
        self.assertEqual(self.snapshot(clock=FakeClock())["required_delay_ms"], 12000)

    def test_sx_delay_policy_uses_game_time_not_fixture_status(self):
        self.api.sx_market["gameTime"] = NOW + 60
        self.api.pair["sx"]["start"] = NOW + 60
        self.api.delay["pregameMsDefault"] = 100
        self.api.delay["leagueIdToMs"] = {"29": 9000}
        snapshot = self.snapshot()
        self.assertEqual(snapshot["sx_fixture_status"], 2)
        self.assertEqual(snapshot["sx_delay_ms"], 100)
        self.api.sx_market["gameTime"] = NOW
        self.api.pair["sx"]["start"] = NOW
        self.assertEqual(self.snapshot(clock=FakeClock())["sx_delay_ms"], 9000)

    def test_poly_missing_or_invalid_delay_is_never_zero(self):
        for delay in (None, "0", True, False, -1, 0.5, float("nan"), float("inf")):
            with self.subTest(delay=delay):
                self.api.market["secondsDelay"] = delay
                self.rejected("poly_delay_unknown", clock=FakeClock())
        del self.api.market["secondsDelay"]
        self.rejected("poly_delay_unknown", clock=FakeClock())
        self.api.market["secondsDelay"] = 0
        self.assertEqual(self.snapshot(clock=FakeClock())["poly_delay_ms"], 0)

    def test_missing_sx_delay_fields_and_invalid_selected_override_reject(self):
        for field in ("liveMsDefault", "pregameMsDefault", "sportIdToMs", "leagueIdToMs"):
            with self.subTest(field=field):
                api = Fixtures()
                del api.delay[field]
                self.rejected("sx_delay_unknown", api=api, clock=FakeClock())
        for bad in (None, "4000", True, -1, 1.5, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                api = Fixtures()
                api.delay["sportIdToMs"] = {"3": 6000}
                api.delay["leagueIdToMs"] = {"29": bad}
                self.rejected("sx_delay_unknown", api=api, clock=FakeClock())

    def test_missing_or_failed_public_responses_reject(self):
        for name in ("sx_response", "fixtures_response", "metadata"):
            for bad in (None, [], {"status": "failure"}, {"status": True, "data": {}}):
                with self.subTest(source=name, bad=bad):
                    api = Fixtures()
                    setattr(api, name, bad)
                    self.rejected(api=api, clock=FakeClock())
        for exception in (TimeoutError("token=DO_NOT_LEAK"), ValueError("private URL")):
            self.api.failure = exception
            self.rejected("poly_source_unavailable", clock=FakeClock())

    def test_ttl_is_counted_from_before_http_and_cannot_be_extended(self):
        snapshot = self.snapshot(clock=FakeClock(100, 102.9))
        self.assertEqual(snapshot["checks_started_mono"], 100)
        self.assertEqual(snapshot["valid_until_mono"], 103)
        self.assertAlmostEqual(snapshot["valid_until_mono"] - snapshot["checked_mono"], 0.1)
        for finish in (103, 104):
            with self.subTest(finish=finish):
                self.rejected("live_evidence_expired", clock=FakeClock(100, finish))
        self.rejected("monotonic_clock_regressed", clock=FakeClock(100, 99))

    def test_invalid_clocks_do_not_produce_evidence(self):
        for bad in (None, "100", True, float("nan"), float("inf"), -1):
            with self.subTest(bad=bad):
                self.rejected("invalid_monotonic_clock", clock=FakeClock(started=bad))
                self.rejected("invalid_wall_clock", clock=FakeClock(epoch=bad))

    def test_display_fields_are_optional_short_strings(self):
        self.api.event["score"] = "X\x00\n" * 100
        self.api.event["period"] = 3
        result = self.snapshot()
        self.assertLessEqual(len(result["score"]), 80)
        self.assertFalse(re.search(r"[\x00-\x1f]", result["score"]))
        self.assertNotIn("period", result)

    def test_overlapping_checks_do_not_queue_more_than_four_http_workers(self):
        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()
        lock = threading.Lock()
        counters = {"entered": 0, "done": 0}
        errors = []

        def blocked_get(url, params=None, timeout=None):
            with lock:
                counters["entered"] += 1
                if counters["entered"] == 4:
                    started.set()
            try:
                release.wait(timeout=2)
                return self.api.get(url, params=params, timeout=timeout)
            finally:
                with lock:
                    counters["done"] += 1
                    if counters["done"] == 4:
                        finished.set()

        def first_call():
            try:
                live.fetch_live_snapshot(self.api.pair, get_json=blocked_get,
                                         clock=FakeClock().monotonic, wall=lambda: NOW)
            except live.LiveCheckError as exc:
                errors.append(exc.reason)

        with patch.object(live, "REQUEST_TIMEOUT", 0.15):
            worker = threading.Thread(target=first_call)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                self.rejected("live_sources_busy", clock=FakeClock())
                worker.join(timeout=0.6)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, ["live_sources_timeout"])
                self.assertEqual(counters["entered"], 4)
                self.rejected("live_sources_busy", clock=FakeClock())
            finally:
                release.set()
                worker.join(timeout=2)
                self.assertTrue(finished.wait(timeout=2))
        # Drain completion callbacks before a later test reuses the global pool.
        acquired = 0
        try:
            for _ in range(4):
                self.assertTrue(live._SLOTS.acquire(timeout=1))
                acquired += 1
        finally:
            for _ in range(acquired):
                live._SLOTS.release()


if __name__ == "__main__":
    unittest.main()
