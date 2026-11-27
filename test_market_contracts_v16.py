"""Offline contract proofs and shared realistic fixtures for V16 integration.

Descriptions reproduce the official Gamma templates inspected on 2026-11-27.
IDs/participants are deterministic test data; no network or real trading.
"""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import market_contracts_v16 as contracts


DEFAULT_NOW = 1795860000
POLY_CONDITION = "0x" + "11" * 32
SX_HASH = "0x" + "22" * 32


def _fixture_dates(start):
    local = datetime.fromtimestamp(start, ZoneInfo("America/New_York"))
    deadline = (local + timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
    scheduled = local.strftime("%A, %B %d, %Y at %I:%M %p ET")
    return scheduled, deadline.strftime("%A, %B %d, %Y, %I:%M %p ET")


def _soccer_description(teams, predicate, start):
    scheduled, deadline = _fixture_dates(start)
    team = teams[0 if predicate == "home" else 1] if predicate != "draw" else None
    first = "If the game ends in a draw" if team is None else f"If {team} wins"
    result = "Yes" if predicate == "draw" else "No"
    return f'''In the upcoming Premier League game between {teams[0]} and {teams[1]}, originally scheduled for {scheduled} (as of creation of the first market related to the event):
{first}, this market will resolve to "Yes".
Otherwise, this market will resolve to "No".
If this market's outcome was definitively determined under its rules by the listed resolution sources before abandonment, that outcome will stand. Otherwise, the following no-result provisions apply. If the game is canceled, does not start by {deadline}, or is abandoned with no official result by that time, this market will resolve to "{result}".
The following abandonment rules apply only when this market's outcome was not already definitively determined under its rules before abandonment. If the game is abandoned, this market will resolve according to the official ruling of the governing body or event organizers on the abandoned game. If a result is awarded by {deadline} that determines this market's outcome, this market will resolve based on the awarded result, regardless of the score as played. If no such result is awarded and the result as played at the time of abandonment is declared to stand by {deadline}, this market will resolve based on the game as played. If the game is replayed or resumed from the point of abandonment and has an official result by {deadline}, this market will resolve based on the completed result. In any other case, this market will resolve to "{result}".
This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time.

Recognizable abbreviations, alternate or erroneous spellings, sponsor names, regional identifiers, and minor formatting differences in a team's or player's name will not affect resolution, provided the intended team or player can be clearly and uniquely identified as a participant in the listed fixture or competition. If a listed name has no reasonable connection to any participant, or could reasonably refer to more than one participant such that the intended team or player cannot be determined, this market will resolve as it would if the listed fixture had been canceled entirely; a listed participant who cannot be identified does not count as inactive or absent. The primary resolution source for this market will be official match information from Premier League (https://www.premierleague.com/). If the governing body or event organizers have not published final statistics within 2 hours of the event's conclusion, data from Flashscore may be used; if Flashscore data is unavailable, data from Sofascore may be used; if neither is available, a consensus of credible reporting may be used. Revisions to officially declared final scores made after market resolution will not be accounted for in determining the outcome. If no acceptable data is available within 24 hours of the event's conclusion, this market will resolve to "{result}".'''


def _hockey_description(teams, start, official=False):
    scheduled, deadline = _fixture_dates(start)
    if official:
        return f'''In the upcoming Liiga match between {teams[0]} and {teams[1]}, originally scheduled for {scheduled} (as of creation of the first market related to the event):

This market will resolve to "{teams[0]}" if {teams[0]} is officially credited with the match win.

This market will resolve to "{teams[1]}" if {teams[1]} is officially credited with the match win.

The official final result will be used, including overtime and any shootout. If the official result is a draw, this market will resolve 50-50.

If this market's outcome was definitively determined under its rules by the listed resolution sources before abandonment, that outcome will stand. Otherwise, the following no-result provisions apply. If the match is canceled, does not start by {deadline}, or is abandoned with no official result by that time, this market will resolve 50-50.

Resolution will be based on official Liiga results. If official results have not been published within 24 hours after the match concludes, a consensus of credible reporting may be used instead.'''
    return f'''In the upcoming NHL game between {teams[0]} and {teams[1]}, originally scheduled for {scheduled} (as of creation of the first market related to the event):
If the {teams[0]} win, the market will resolve to "{teams[0]}".
If the {teams[1]} win, the market will resolve to "{teams[1]}".
If this market's outcome was definitively determined under its rules by the listed resolution sources before abandonment, that outcome will stand. Otherwise, the following no-result provisions apply. If the game is canceled, does not start by {deadline}, or is abandoned with no official result by that time, this market will resolve 50-50.
The result will be determined based on the final score including any overtime periods and shootouts. In the event of a shootout, one goal will be added to the winning team's score for the purpose of resolution.'''


def _fixture(sport, now, *, predicate="home", poly_index=1, sx_target=0,
             reverse_sx=False, official=False):
    teams = (["Manchester United FC", "Brentford FC"] if sport == "soccer"
             else ["Assat Pori", "Karpat Oulu"] if official else ["Red Wings", "Blue Jackets"])
    start = int(now) + 3600
    title = " vs. ".join(teams)
    date = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d")
    if sport == "soccer":
        name = teams[0 if predicate == "home" else 1] if predicate != "draw" else "Tie"
        question = f"Will {title} end in a draw?" if predicate == "draw" else f"Will {name} win on {date}?"
        group = f"Draw ({title})" if predicate == "draw" else name
        description = _soccer_description(teams, predicate, start)
        labels = ["Yes", "No"]
        sx_labels = [name, "Not " + ("tie" if name == "Tie" else name)]
    else:
        question, group, labels = title, None, teams[:]
        description = _hockey_description(teams, start, official)
        sx_labels = teams[:]
    raw_poly = {
        "id": "7202037" if sport == "soccer" else "6474596",
        "conditionId": POLY_CONDITION, "question": question,
        "description": description,
        "resolutionSource": ("https://www.premierleague.com/" if sport == "soccer"
                             else "https://liiga.fi/en" if official else "https://www.nhl.com/scores"),
        "sportsMarketType": "moneyline", "groupItemTitle": group,
        "version": "v1", "gameStartTime": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "outcomes": json.dumps(labels), "clobTokenIds": '["101", "102"]',
        "positionIds": ["201", "202"], "negRisk": sport == "soccer",
        "negRiskMarketID": "0x" + "33" * 32 if sport == "soccer" else None,
        "active": True, "closed": False, "archived": False,
        "enableOrderBook": True, "acceptingOrders": True,
        "feesEnabled": True, "feeSchedule": {"rate": "0.03", "exponent": 1, "takerOnly": True},
        "spread": 0.01,  # Gamma bid/ask price spread, not a sports handicap.
        "fallbackResolutionType": "equal_split", "resolutionMechanism": "oracle",
    }
    sx_teams = teams[::-1] if reverse_sx else teams[:]
    raw_sx = {
        "marketHash": SX_HASH, "type": 1 if sport == "soccer" else 226,
        "sportXeventId": "L20913345", "gameTime": start,
        "teamOneName": sx_teams[0], "teamTwoName": sx_teams[1],
        "outcomeOneName": sx_labels[0], "outcomeTwoName": sx_labels[1],
        "outcomeVoidName": "NO_CONTEST", "sportId": 5 if sport == "soccer" else 6,
        "sportLabel": "Soccer" if sport == "soccer" else "Ice Hockey",
        "leagueId": 1837 if sport == "soccer" else 11,
        "leagueLabel": "England Premier League" if sport == "soccer" else "NHL",
        "participantOneId": 1013, "participantTwoId": 1037,
        "isQuarterLineMarket": False, "status": "ACTIVE", "liveEnabled": True,
    }
    event = {"id": "1432228" if sport == "soccer" else "1289073", "title": title}
    matcher = lambda a, b: a.casefold() == b.casefold()
    pc = contracts.classify_poly_market(raw_poly, event, sport, teams, matcher)
    sc = contracts.classify_sx_market(raw_sx, sport, sx_teams, matcher)
    p = {
        "id": raw_poly["id"], "event_id": event["id"], "title": title,
        "sport": sport, "teams": teams, "outcomes": labels, "assets": ["101", "102"],
        "start": start, "type": "moneyline", "version": "v1", "contract": pc,
        "event_live": False, "event_ended": False, "series_id": "10192" if sport == "soccer" else "10346",
        "feesEnabled": raw_poly["feesEnabled"], "feeSchedule": raw_poly["feeSchedule"],
        "fees_fetched_epoch": float(now),
    }
    s = {
        "hash": SX_HASH, "sport": sport, "teams": sx_teams, "outcomes": sx_labels,
        "event_id": raw_sx["sportXeventId"], "start": start, "type": raw_sx["type"],
        "sport_id": raw_sx["sportId"], "league_id": raw_sx["leagueId"],
        "league": raw_sx["leagueLabel"], "live_enabled": True, "contract": sc,
    }
    mapping = {0: 1, 1: 0} if reverse_sx else {0: 0, 1: 1}
    proof = contracts.make_pair_contract(p, s, poly_index, sx_target, mapping)
    pair = {
        "poly": p, "sx": s, "poly_asset": p["assets"][poly_index],
        "poly_label": labels[poly_index], "sx_target": sx_target,
        "sx_label": sx_labels[sx_target], "settlement_contract": proof,
        "key": f'{p["id"]}:{s["hash"]}:{poly_index}:{sx_target}',
    }
    return pair, raw_poly, raw_sx


def build_soccer_fixture(now=DEFAULT_NOW, *, predicate="home", poly_index=1,
                         sx_target=0, reverse_sx=False):
    """Return (pair, raw_poly, raw_sx); default No(home) covers draw + away."""
    return _fixture("soccer", now, predicate=predicate, poly_index=poly_index,
                    sx_target=sx_target, reverse_sx=reverse_sx)


def build_hockey_fixture(now=DEFAULT_NOW, *, poly_index=0, sx_target=1,
                         reverse_sx=False, official=False):
    """Return (pair, raw_poly, raw_sx), with explicit overtime + shootout rules."""
    return _fixture("hockey", now, poly_index=poly_index, sx_target=sx_target,
                    reverse_sx=reverse_sx, official=official)


def record_for_pair(pair, **extra):
    record = {
        "sport": pair["sx"]["sport"], "poly_market": pair["poly"]["id"],
        "poly_token_id": pair["poly_asset"], "poly_side": pair["poly_label"],
        "sx_market": pair["sx"]["hash"], "sx_side": pair["sx_label"],
        "sx_outcome_one": int(pair["sx_target"] == 0),
        "game_start_epoch": pair["sx"]["start"],
        "fixture_key": pair["sx"]["sport"] + ":sx:" + pair["sx"]["event_id"],
        "contract_evidence_json": json.dumps(pair["settlement_contract"]),
    }
    record.update(extra)
    return record


def _rehash(contract):
    value = {k: v for k, v in contract.items() if k != "digest"}
    contract["digest"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                 separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ContractTests(unittest.TestCase):
    def test_soccer_no_home_includes_draw_and_away(self):
        pair, raw_p, raw_s = build_soccer_fixture()
        c = pair["settlement_contract"]
        self.assertEqual(c["selected_states"], {"poly": ["draw", "away"], "sx": ["home"]})
        self.assertEqual(c["poly_display"], "Manchester United FC não vence (inclui empate)")
        self.assertEqual(c["compatibility"], "normal_result_only")
        self.assertEqual(c["exception_policy"], "review")
        self.assertNotEqual(c["poly"]["exceptional_rules"], c["sx"]["exceptional_rules"])
        self.assertTrue(contracts.validate_pair_contract(pair))
        self.assertTrue(contracts.verify_poly_source(c, raw_p))
        self.assertTrue(contracts.verify_sx_source(c, raw_s))

    def test_all_soccer_propositions_cover_exactly_three_states(self):
        for predicate in ("home", "draw", "away"):
            for p, s in ((0, 1), (1, 0)):
                with self.subTest(predicate=predicate, p=p):
                    pair, _, _ = build_soccer_fixture(predicate=predicate, poly_index=p, sx_target=s)
                    states = pair["settlement_contract"]["selected_states"]
                    self.assertFalse(set(states["poly"]) & set(states["sx"]))
                    self.assertEqual(set(states["poly"]) | set(states["sx"]), {"home", "draw", "away"})
                    self.assertTrue(contracts.validate_pair_contract(pair))

    def test_no_home_is_not_an_away_moneyline(self):
        pair, _, _ = build_soccer_fixture()
        away_pair, _, _ = build_soccer_fixture(predicate="away")
        with self.assertRaisesRegex(contracts.ContractError, "not_complementary"):
            contracts.make_pair_contract(pair["poly"], away_pair["sx"], 1, 0, {0: 0, 1: 1})

    def test_same_sides_are_rejected(self):
        pair, _, _ = build_soccer_fixture()
        with self.assertRaisesRegex(contracts.ContractError, "not_complementary"):
            contracts.make_pair_contract(pair["poly"], pair["sx"], 0, 0, {0: 0, 1: 1})

    def test_reversed_provider_order_is_mapped(self):
        pair, _, _ = build_soccer_fixture(reverse_sx=True)
        self.assertEqual(pair["settlement_contract"]["selected_states"],
                         {"poly": ["home", "draw"], "sx": ["away"]})
        self.assertTrue(contracts.validate_pair_contract(pair))

    def test_mapping_cannot_swap_identical_participant_names(self):
        # A false reversal could otherwise make two bets on the SAME hockey
        # winner appear disjoint in the certificate's coordinate system.
        pair, _, _ = build_hockey_fixture()
        with self.assertRaisesRegex(contracts.ContractError, "mapping_contradicts_identity"):
            contracts.make_pair_contract(pair["poly"], pair["sx"], 0, 0, {0: 1, 1: 0})

    def test_regulation_live_period_requires_positive_known_evidence(self):
        for period in ("1H", "2H", "HT", "FIRST_HALF", "second-half", "1st half", "2nd half", "half time"):
            self.assertTrue(contracts.soccer_live_period_is_regulation(period), period)
        for period in (None, "", 1, True, "unknown", "90+4", "95", "ET", "OT",
                       "extra time", "first half extra time", "penalties", "shootout", "2nd"):
            self.assertFalse(contracts.soccer_live_period_is_regulation(period), period)

    def test_hockey_templates_require_overtime_and_shootouts(self):
        for official in (False, True):
            pair, _, _ = build_hockey_fixture(official=official)
            self.assertTrue(contracts.validate_pair_contract(pair))
            c = pair["settlement_contract"]
            self.assertEqual(c["scope"], "hockey_full_game_ot_so")
            self.assertFalse(c["poly"]["exceptional_rules"]["split_is_stake_refund"])
            self.assertEqual(c["selected_states"], {"poly": ["home"], "sx": ["away"]})

    def test_observed_shl_variant_only_omits_article_before_team(self):
        pair, raw, _ = build_hockey_fixture()
        teams = ["Brynäs", "Örebro HK"]
        raw.update(question="SHL: Brynäs vs. Örebro HK", outcomes=json.dumps(teams),
                   resolutionSource="https://www.eliteprospects.com/league/shl")
        raw["description"] = '''In the upcoming SHL game between Brynäs and Örebro HK, originally scheduled for Saturday, November 28, 2026 at 9:15 AM ET (as of creation of the first market related to the event):
If Brynäs win, the market will resolve to "Brynäs".
If Örebro HK win, the market will resolve to "Örebro HK".
If this market's outcome was definitively determined under its rules by the listed resolution sources before abandonment, that outcome will stand. Otherwise, the following no-result provisions apply. If the game is canceled, does not start by Monday, November 30, 2026, 11:59 PM ET, or is abandoned with no official result by that time, this market will resolve 50-50.
The result will be determined based on the final score including any overtime periods and shootouts. In the event of a shootout, one goal will be added to the winning team's score for the purpose of resolution.'''
        event = {"id": "1458818", "title": raw["question"]}
        proof = contracts.classify_poly_market(raw, event, "hockey", teams, lambda a, b: a == b)
        self.assertEqual(proof["winning_states"], [["home"], ["away"]])
        self.assertEqual(proof["source_fields"]["description"], raw["description"])
        raw["description"] = raw["description"].replace("periods and shootouts", "periods but excluding shootouts")
        with self.assertRaises(contracts.ContractError):
            contracts.classify_poly_market(raw, event, "hockey", teams, lambda a, b: a == b)

    def test_unknown_or_conflicting_rules_fail_closed(self):
        for build in (build_soccer_fixture, build_hockey_fixture):
            pair, raw, _ = build()
            pc = pair["poly"]["contract"]
            variants = ["", raw["description"] + " Only the first half counts.",
                        raw["description"].replace("including", "excluding").replace("first 90 minutes", "first 45 minutes")]
            for description in variants:
                with self.subTest(sport=pair["sx"]["sport"], description=description[-80:]):
                    bad = {**raw, "description": description}
                    with self.assertRaises(contracts.ContractError):
                        contracts.classify_poly_market(bad, {"id": pc["event_id"], "title": pc["event_title"]},
                                                        pc["sport"], pc["teams"], lambda a, b: a == b)

    def test_type_alone_cannot_approve_wrong_or_unknown_hockey_labels(self):
        pair, _, raw = build_hockey_fixture()
        for mutation in ({"type": 52}, {"outcomeTwoName": "Not Red Wings"},
                         {"isQuarterLineMarket": True}, {"sportLabel": "Basketball"},
                         {"outcomeVoidName": None}, {"period": "1st period"}):
            with self.subTest(mutation=mutation):
                with self.assertRaises(contracts.ContractError):
                    contracts.classify_sx_market({**raw, **mutation}, "hockey", pair["sx"]["teams"], lambda a, b: a == b)

    def test_v2_uses_position_ids_even_if_both_arrays_present(self):
        pair, raw, _ = build_soccer_fixture()
        pc = pair["poly"]["contract"]
        raw["version"] = "v2"
        classified = contracts.classify_poly_market(raw, {"id": pc["event_id"], "title": pc["event_title"]},
                                                    "soccer", pc["teams"], lambda a, b: a == b)
        self.assertEqual(classified["assets"], ["201", "202"])
        self.assertEqual(classified["identifier_field"], "positionIds")

    def test_source_changes_block_but_status_and_prices_do_not(self):
        pair, raw_p, raw_s = build_soccer_fixture()
        c = pair["settlement_contract"]
        self.assertTrue(contracts.verify_poly_source(c, {**raw_p, "closed": True, "outcomePrices": '["0","1"]'}))
        self.assertTrue(contracts.verify_poly_source(c, {**raw_p, "spread": 0.03}))
        self.assertTrue(contracts.verify_sx_source(c, {**raw_s, "status": "SETTLED", "outcome": 1}))
        for mutation in ({"description": raw_p["description"] + " "}, {"resolutionSource": "https://example.invalid/"},
                         {"clobTokenIds": '["103","104"]'}, {"version": "v2"}, {"gameStartTime": "2030-01-01T00:00:00+00:00"},
                         {"fallbackResolutionType": "refund"}, {"resolutionMechanism": "manual"}):
            self.assertFalse(contracts.verify_poly_source(c, {**raw_p, **mutation}))
        for mutation in ({"type": 52}, {"outcomeVoidName": "UNKNOWN"}, {"sportXeventId": "L999"},
                         {"gameTime": raw_s["gameTime"] + 60}, {"outcomeTwoName": "Brentford FC"}):
            self.assertFalse(contracts.verify_sx_source(c, {**raw_s, **mutation}))

    def test_self_rehashed_forged_state_set_fails_semantic_validation(self):
        pair, _, _ = build_soccer_fixture()
        pair["settlement_contract"]["poly"]["winning_states"][1] = ["away"]
        _rehash(pair["settlement_contract"])
        self.assertFalse(contracts.validate_pair_contract(pair))

    def test_pair_identity_and_selection_are_bound_to_certificate(self):
        original, _, _ = build_soccer_fixture()
        for key, value in (("poly_asset", "999"), ("poly_label", "Yes"), ("sx_target", 1),
                           ("sx_label", "Not Manchester United FC")):
            pair = copy.deepcopy(original)
            pair[key] = value
            self.assertFalse(contracts.validate_pair_contract(pair))
        pair = copy.deepcopy(original)
        pair["poly"]["teams"].reverse()
        self.assertFalse(contracts.validate_pair_contract(pair))
        pair = copy.deepcopy(original)
        pair.pop("settlement_contract")
        self.assertFalse(contracts.validate_pair_contract(pair))

    def test_ledger_validation_has_no_freshness_cutoff_and_ignores_other_columns(self):
        pair, _, _ = build_soccer_fixture(now=1000000000)
        record = record_for_pair(pair, status="OPEN_CONDITIONAL", created_epoch=1, free_cents=80000)
        self.assertTrue(contracts.validate_record_contract(record))
        for field, value in (("poly_market", "1"), ("poly_token_id", "999"), ("poly_side", "Yes"),
                             ("sx_side", "Not Manchester United FC"), ("sx_outcome_one", 0),
                             ("sport", "hockey"), ("fixture_key", "soccer:sx:Lother")):
            self.assertFalse(contracts.validate_record_contract({**record, field: value}))

    def test_digest_and_displays_are_frozen(self):
        pair, _, _ = build_hockey_fixture()
        pair["settlement_contract"]["poly_display"] = "Guaranteed profit"
        _rehash(pair["settlement_contract"])
        self.assertFalse(contracts.validate_pair_contract(pair))

    def test_ledger_start_binds_to_sx_when_provider_times_differ(self):
        pair, _, raw_s = build_soccer_fixture()
        raw_s["gameTime"] += 300
        pair["sx"]["start"] += 300
        pair["sx"]["contract"] = contracts.classify_sx_market(
            raw_s, "soccer", pair["sx"]["teams"], lambda a, b: a == b)
        pair["settlement_contract"] = contracts.make_pair_contract(
            pair["poly"], pair["sx"], 1, 0, {0: 0, 1: 1})
        self.assertTrue(contracts.validate_pair_contract(pair))
        self.assertTrue(contracts.validate_record_contract(record_for_pair(pair)))
        self.assertFalse(contracts.validate_record_contract(record_for_pair(
            pair, game_start_epoch=pair["poly"]["start"])))

    def test_eastern_deadline_blocks_postponement_beyond_limit(self):
        # ET means UTC-5 in winter and UTC-4 in summer, not a fixed offset.
        for month, utc_hour in ((1, 4), (7, 3)):
            now = int(datetime(2027, month, 2, 12, tzinfo=timezone.utc).timestamp())
            expected = int(datetime(2027, month, 5, utc_hour, 59, tzinfo=timezone.utc).timestamp())
            for build in (build_soccer_fixture, build_hockey_fixture):
                with self.subTest(month=month, sport=build.__name__):
                    pair, raw, _ = build(now)
                    pc = pair["poly"]["contract"]
                    self.assertEqual(pc["exceptional_rules"]["deadline_epoch"], expected)
                    event = {"id": pc["event_id"], "title": pc["event_title"]}
                    # The original description date need not be the updated
                    # kickoff, provided the rescheduled game is within its limit.
                    raw["gameStartTime"] = datetime.fromtimestamp(expected - 3600, timezone.utc).isoformat()
                    within = contracts.classify_poly_market(raw, event, pc["sport"], pc["teams"], lambda a, b: a == b)
                    self.assertEqual(within["start"], expected - 3600)
                    raw["gameStartTime"] = datetime.fromtimestamp(expected + 1, timezone.utc).isoformat()
                    with self.assertRaisesRegex(contracts.ContractError, "start_after_resolution_deadline"):
                        contracts.classify_poly_market(raw, event, pc["sport"], pc["teams"], lambda a, b: a == b)

        # Even when Poly's kickoff is earlier, SX's independently reported
        # kickoff cannot lie on the other side of the Poly cancellation limit.
        pair, raw_p, raw_s = build_soccer_fixture(now)
        deadline = pair["poly"]["contract"]["exceptional_rules"]["deadline_epoch"]
        p, s = pair["poly"], pair["sx"]
        raw_p["gameStartTime"] = datetime.fromtimestamp(deadline - 300, timezone.utc).isoformat()
        raw_s["gameTime"] = deadline + 1
        p["start"], s["start"] = deadline - 300, deadline + 1
        p["contract"] = contracts.classify_poly_market(raw_p, {"id": p["event_id"], "title": p["title"]},
                                                      p["sport"], p["teams"], lambda a, b: a == b)
        s["contract"] = contracts.classify_sx_market(raw_s, s["sport"], s["teams"], lambda a, b: a == b)
        with self.assertRaisesRegex(contracts.ContractError, "start_after_resolution_deadline"):
            contracts.make_pair_contract(p, s, 1, 0, {0: 0, 1: 1})


if __name__ == "__main__":
    unittest.main()
