"""Offline discovery regressions: completeness, bounded work and identity."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import trafulha_arbitragem as bot


def event(identity, **market_extra):
    m = {
        "id": "m" + str(identity), "sportsMarketType": "moneyline",
        "active": True, "closed": False, "version": "v1",
        "outcomes": '["Team Alpha", "Team Bravo"]',
        "clobTokenIds": '["asset-a", "asset-b"]',
        "gameStartTime": "2027-01-01T18:00:00Z",
        "feesEnabled": True,
        "feeSchedule": {"rate": "0.05", "exponent": 1, "takerOnly": True},
    }
    m.update(market_extra)
    return {"id": str(identity), "title": "Team Alpha vs Team Bravo", "markets": [m]}


def sx_market(identity="s", **extra):
    m = {
        "marketHash": identity, "type": 52, "status": "ACTIVE",
        "teamOneName": "Team Alpha", "teamTwoName": "Team Bravo",
        "outcomeOneName": "Team Alpha", "outcomeTwoName": "Team Bravo",
        "gameTime": 1798826400, "sportLabel": "Tennis", "sportId": 1,
        "leagueId": 2, "sportXeventId": "fixture", "liveEnabled": False,
    }
    m.update(extra)
    return m


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bot, "_POLY_SERIES_CURSOR", 0),
            patch.object(bot, "_MATCHED_POLY_SERIES", set()),
            patch.object(bot, "DISCOVERY_STATS", {}),
            patch.object(bot, "MAX_POLY_SERIES", 3),
            patch.object(bot, "MAX_EVENTS_PER_SERIES", 2),
            patch.object(bot, "POLY_MAX_PAGES_PER_SERIES", 4),
            patch.object(bot, "POLY_MAX_REQUESTS_PER_REFRESH", 10),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def poly_api(self, pages, sports=None):
        calls = []
        def get(url, params=None):
            if url.endswith("/sports"):
                return sports or [{"sport": "tennis", "series": "101"}]
            calls.append((params["series_id"], params["offset"]))
            value = pages[(params["series_id"], params["offset"])]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        return get, calls

    def test_later_pages_keep_matches_costs_and_unique_markets(self):
        get, calls = self.poly_api({("101", 0): [event(1), event(2)],
                                    ("101", 2): [event(2), event(3)],
                                    ("101", 4): []})
        with patch.object(bot, "get_json", side_effect=get), patch.object(bot.time, "time", return_value=1800000000):
            result = bot.discover_poly()
        self.assertEqual([p["id"] for p in result], ["m1", "m2", "m3"])
        self.assertEqual(calls, [("101", 0), ("101", 2), ("101", 4)])
        self.assertEqual(result[2]["fees_fetched_epoch"], 1800000000)
        self.assertEqual(result[2]["feeSchedule"]["rate"], "0.05")
        self.assertIs(result[2]["feesEnabled"], True)
        self.assertEqual(result[2]["series_id"], "101")
        stats = bot.DISCOVERY_STATS["poly"]
        self.assertTrue(stats["complete"])
        self.assertEqual(stats["duplicate_event"], 1)

    def test_page_failure_is_not_returned_as_a_successful_partial_catalogue(self):
        get, calls = self.poly_api({("101", 0): [event(1), event(2)],
                                    ("101", 2): RuntimeError("hidden upstream detail")})
        with patch.object(bot, "get_json", side_effect=get):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                bot.discover_poly()
        self.assertEqual(len(calls), 2)
        self.assertFalse(bot.DISCOVERY_STATS["poly"]["complete"])
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["fetch_errors"], 1)

    def test_first_pages_are_fair_and_request_budget_is_not_exceeded(self):
        get, calls = self.poly_api({("101", 0): [event(1), event(2)],
                                    ("102", 0): [event(3), event(4)]},
                                   [{"sport": "tennis", "series": "101,102"}])
        with patch.object(bot, "get_json", side_effect=get), \
             patch.object(bot, "POLY_MAX_REQUESTS_PER_REFRESH", 2):
            result = bot.discover_poly()
        self.assertEqual(calls, [("101", 0), ("102", 0)])
        self.assertEqual(len(result), 4)
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["series_truncated"], 2)
        self.assertFalse(bot.DISCOVERY_STATS["poly"]["complete"])

    def test_series_page_limit_reports_incomplete(self):
        get, calls = self.poly_api({("101", 0): [event(1), event(2)]})
        with patch.object(bot, "get_json", side_effect=get), \
             patch.object(bot, "POLY_MAX_PAGES_PER_SERIES", 1):
            bot.discover_poly()
        self.assertEqual(len(calls), 1)
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["series_truncated"], 1)

    def test_rotate_compatible_series_and_preserve_matched_anchor(self):
        series = {str(i): "tennis" for i in range(101, 108)}
        series["999"] = "soccer"
        bot._MATCHED_POLY_SERIES = {"101"}
        seen = set()
        for _ in range(5):
            chosen, eligible = bot._select_poly_series(series)
            self.assertEqual(eligible, 7)
            self.assertEqual(len(chosen), 3)
            self.assertIn(("101", "tennis"), chosen)
            seen.update(sid for sid, _ in chosen)
        self.assertEqual(seen, set(series) - {"999"})

    def test_missing_fee_fields_remain_unknown(self):
        source = event(1)
        source["markets"][0].pop("feesEnabled")
        source["markets"][0].pop("feeSchedule")
        get, _ = self.poly_api({("101", 0): [source]})
        with patch.object(bot, "get_json", side_effect=get):
            result = bot.discover_poly()
        self.assertIsNone(result[0]["feesEnabled"])
        self.assertIsNone(result[0]["feeSchedule"])

    def test_unverified_types_and_yes_no_stay_excluded(self):
        items = [event(1, sportsMarketType="total"),
                 event(2, outcomes='["Yes","No"]'), event(3)]
        get, _ = self.poly_api({("101", 0): items})
        with patch.object(bot, "get_json", side_effect=get), \
             patch.object(bot, "MAX_EVENTS_PER_SERIES", 150):
            result = bot.discover_poly()
        self.assertEqual([m["id"] for m in result], ["m3"])
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["yes_no_not_equivalent"], 1)
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["unsupported_market_type"], 1)

    def test_sports_failure_overwrites_old_complete_status(self):
        bot.DISCOVERY_STATS["poly"] = {"complete": True}
        with patch.object(bot, "get_json", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                bot.discover_poly()
        self.assertFalse(bot.DISCOVERY_STATS["poly"]["complete"])

    def test_sx_deduplicates_pages_and_reports_completeness(self):
        responses = [
            {"data": {"markets": [sx_market("a")], "nextKey": "next"}},
            {"data": {"markets": [sx_market("a"), sx_market("b")]}},
        ]
        with patch.object(bot, "get_json", side_effect=responses):
            result = bot.discover_sx()
        self.assertEqual([m["hash"] for m in result], ["a", "b"])
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["duplicate_market"], 1)
        self.assertTrue(bot.DISCOVERY_STATS["sx"]["complete"])

    def test_sx_truncation_is_explicit(self):
        with patch.object(bot, "get_json", return_value={
                "data": {"markets": [sx_market()], "nextKey": "more"}}), \
             patch.object(bot, "MAX_SX_PAGES", 1):
            result = bot.discover_sx()
        self.assertEqual(len(result), 1)
        self.assertTrue(bot.DISCOVERY_STATS["sx"]["truncated"])

    def test_sx_stuck_pagination_raises_instead_of_claiming_complete(self):
        with patch.object(bot, "get_json", return_value={
                "data": {"markets": [sx_market()], "nextKey": "same"}}):
            with self.assertRaisesRegex(ValueError, "no progress"):
                bot.discover_sx()
        self.assertFalse(bot.DISCOVERY_STATS["sx"]["complete"])
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["fetch_errors"], 1)

    def test_matching_logs_why_and_does_not_relax_identity(self):
        p = {"id": "p1", "sport": "tennis", "teams": ("Team Alpha", "Team Bravo"),
             "start": 1800000000, "outcomes": ["Team Alpha", "Team Bravo"],
             "assets": ["a", "b"], "series_id": "101"}
        s = {"hash": "s1", "sport": "tennis", "teams": p["teams"],
             "outcomes": p["outcomes"], "start": p["start"], "type": 52}
        excluded = {**s, "hash": "s2", "sport": "soccer"}
        mismatch = {**s, "hash": "s3", "teams": ("Team Charlie", "Team Delta"),
                    "outcomes": ("Team Charlie", "Team Delta")}
        result = bot.pair_markets([p], [s, excluded, mismatch])
        self.assertEqual(len(result), 2)
        self.assertEqual(bot._MATCHED_POLY_SERIES, {"101"})
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["excluded_settlement_sport"], 1)
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["no_poly_participant_match"], 1)
        self.assertEqual(bot.pair_markets([p, {**p, "id": "p2"}], [s]), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["ambiguous_poly_markets"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
