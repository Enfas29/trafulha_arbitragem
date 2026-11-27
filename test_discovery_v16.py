"""Offline V16 discovery tests using official public market snapshots.

Fixtures retrieved 2026-11-27 from:
https://gamma-api.polymarket.com/events/1432228
https://gamma-api.polymarket.com/events/1289073
Only contract identity/rules, status and fee fields are retained.
The test suite never requests APIs, sends Telegram, opens a ledger or orders.
"""
from __future__ import annotations

import copy
import json
import unittest
from collections import Counter
from unittest.mock import patch

import market_contracts_v16 as contracts
import trafulha_arbitragem as bot

SOCCER_EVENT = {'id': '1432228',
 'title': 'Manchester United FC vs. Brentford FC',
 'markets': [{'id': '7202037',
              'question': 'Will Manchester United FC win on 2026-11-28?',
              'conditionId': '0xcfcadf1aa69c22b417039918b04cb677f7f90158b68428b080be61bef8938f62',
              'resolutionSource': 'https://www.premierleague.com/',
              'description': 'In the upcoming Premier League game between Manchester United FC and '
                             'Brentford FC, originally scheduled for Saturday, November 28, 2026 '
                             'at 10:00 AM ET (as of creation of the first market related to the '
                             'event):\n'
                             'If Manchester United FC wins, this market will resolve to "Yes".\n'
                             'Otherwise, this market will resolve to "No".\n'
                             "If this market's outcome was definitively determined under its rules "
                             'by the listed resolution sources before abandonment, that outcome '
                             'will stand. Otherwise, the following no-result provisions apply. If '
                             'the game is canceled, does not start by Monday, November 30, 2026, '
                             '11:59 PM ET, or is abandoned with no official result by that time, '
                             'this market will resolve to "No".\n'
                             "The following abandonment rules apply only when this market's "
                             'outcome was not already definitively determined under its rules '
                             'before abandonment. If the game is abandoned, this market will '
                             'resolve according to the official ruling of the governing body or '
                             'event organizers on the abandoned game. If a result is awarded by '
                             "Monday, November 30, 2026, 11:59 PM ET that determines this market's "
                             'outcome, this market will resolve based on the awarded result, '
                             'regardless of the score as played. If no such result is awarded and '
                             'the result as played at the time of abandonment is declared to stand '
                             'by Monday, November 30, 2026, 11:59 PM ET, this market will resolve '
                             'based on the game as played. If the game is replayed or resumed from '
                             'the point of abandonment and has an official result by Monday, '
                             'November 30, 2026, 11:59 PM ET, this market will resolve based on '
                             'the completed result. In any other case, this market will resolve to '
                             '"No".\n'
                             'This market refers only to the outcome within the first 90 minutes '
                             'of regular play plus stoppage time.\n'
                             '\n'
                             'Recognizable abbreviations, alternate or erroneous spellings, '
                             'sponsor names, regional identifiers, and minor formatting '
                             "differences in a team's or player's name will not affect resolution, "
                             'provided the intended team or player can be clearly and uniquely '
                             'identified as a participant in the listed fixture or competition. If '
                             'a listed name has no reasonable connection to any participant, or '
                             'could reasonably refer to more than one participant such that the '
                             'intended team or player cannot be determined, this market will '
                             'resolve as it would if the listed fixture had been canceled '
                             'entirely; a listed participant who cannot be identified does not '
                             'count as inactive or absent. The primary resolution source for this '
                             'market will be official match information from Premier League '
                             '(https://www.premierleague.com/). If the governing body or event '
                             'organizers have not published final statistics within 2 hours of the '
                             "event's conclusion, data from Flashscore may be used; if Flashscore "
                             'data is unavailable, data from Sofascore may be used; if neither is '
                             'available, a consensus of credible reporting may be used. Revisions '
                             'to officially declared final scores made after market resolution '
                             'will not be accounted for in determining the outcome. If no '
                             "acceptable data is available within 24 hours of the event's "
                             'conclusion, this market will resolve to "No".',
              'outcomes': '["Yes", "No"]',
              'active': True,
              'closed': False,
              'groupItemTitle': 'Manchester United FC',
              'questionID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13000',
              'gameStartTime': '2026-11-28 15:00:00+00',
              'clobTokenIds': '["71065753025362956131675332020232403673370772535124975508309449610396293023389", '
                              '"72773898871484663130587102961409378786754907151000728077636498332146731945469"]',
              'positionIds': ['1163758146525570172421600930251808485205050750828786825083231722024399273984',
                              '1163758146525570172421600930251808485205050750828786825083231722024399273985'],
              'customLiveness': 900,
              'negRisk': True,
              'negRiskMarketID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13000',
              'negRiskRequestID': '0x61da93034377d9c15fee8ea27ef7825e13e73d3f5e2fa11f5815a61baee24800',
              'spread': 0.01,
              'sportsMarketType': 'moneyline',
              'feesEnabled': True,
              'feeSchedule': {'exponent': 1, 'rate': 0.05, 'takerOnly': True, 'rebateRate': 0.15},
              'version': 'v1'},
             {'id': '7202038',
              'question': 'Will Manchester United FC vs. Brentford FC end in a draw?',
              'conditionId': '0xe2b2307981a52ead6c1d0b3fc9eb1092e1ac9ed4e8f80bfdfd56701e0360faf1',
              'resolutionSource': 'https://www.premierleague.com/',
              'description': 'In the upcoming Premier League game between Manchester United FC and '
                             'Brentford FC, originally scheduled for Saturday, November 28, 2026 '
                             'at 10:00 AM ET (as of creation of the first market related to the '
                             'event):\n'
                             'If the game ends in a draw, this market will resolve to "Yes".\n'
                             'Otherwise, this market will resolve to "No".\n'
                             "If this market's outcome was definitively determined under its rules "
                             'by the listed resolution sources before abandonment, that outcome '
                             'will stand. Otherwise, the following no-result provisions apply. If '
                             'the game is canceled, does not start by Monday, November 30, 2026, '
                             '11:59 PM ET, or is abandoned with no official result by that time, '
                             'this market will resolve to "Yes".\n'
                             "The following abandonment rules apply only when this market's "
                             'outcome was not already definitively determined under its rules '
                             'before abandonment. If the game is abandoned, this market will '
                             'resolve according to the official ruling of the governing body or '
                             'event organizers on the abandoned game. If a result is awarded by '
                             "Monday, November 30, 2026, 11:59 PM ET that determines this market's "
                             'outcome, this market will resolve based on the awarded result, '
                             'regardless of the score as played. If no such result is awarded and '
                             'the result as played at the time of abandonment is declared to stand '
                             'by Monday, November 30, 2026, 11:59 PM ET, this market will resolve '
                             'based on the game as played. If the game is replayed or resumed from '
                             'the point of abandonment and has an official result by Monday, '
                             'November 30, 2026, 11:59 PM ET, this market will resolve based on '
                             'the completed result. In any other case, this market will resolve to '
                             '"Yes".\n'
                             'This market refers only to the outcome within the first 90 minutes '
                             'of regular play plus stoppage time.\n'
                             '\n'
                             'Recognizable abbreviations, alternate or erroneous spellings, '
                             'sponsor names, regional identifiers, and minor formatting '
                             "differences in a team's or player's name will not affect resolution, "
                             'provided the intended team or player can be clearly and uniquely '
                             'identified as a participant in the listed fixture or competition. If '
                             'a listed name has no reasonable connection to any participant, or '
                             'could reasonably refer to more than one participant such that the '
                             'intended team or player cannot be determined, this market will '
                             'resolve as it would if the listed fixture had been canceled '
                             'entirely; a listed participant who cannot be identified does not '
                             'count as inactive or absent. The primary resolution source for this '
                             'market will be official match information from Premier League '
                             '(https://www.premierleague.com/). If the governing body or event '
                             'organizers have not published final statistics within 2 hours of the '
                             "event's conclusion, data from Flashscore may be used; if Flashscore "
                             'data is unavailable, data from Sofascore may be used; if neither is '
                             'available, a consensus of credible reporting may be used. Revisions '
                             'to officially declared final scores made after market resolution '
                             'will not be accounted for in determining the outcome. If no '
                             "acceptable data is available within 24 hours of the event's "
                             'conclusion, this market will resolve to "Yes".',
              'outcomes': '["Yes", "No"]',
              'active': True,
              'closed': False,
              'groupItemTitle': 'Draw (Manchester United FC vs. Brentford FC)',
              'questionID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13001',
              'gameStartTime': '2026-11-28 15:00:00+00',
              'clobTokenIds': '["3309401357002617814628748516343122215812201087002409953231901238197002607022", '
                              '"37216369694785410195325026486008251920700216235444241582073980594186884560692"]',
              'positionIds': ['1163758146525570172421600930251808485205050750828786825083231722024399274240',
                              '1163758146525570172421600930251808485205050750828786825083231722024399274241'],
              'customLiveness': 900,
              'negRisk': True,
              'negRiskMarketID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13000',
              'negRiskRequestID': '0x4816b9830d7c22fb170098b3755e85ebab56e562ef6c5d74b0ce4de7a9770575',
              'spread': 0.01,
              'sportsMarketType': 'moneyline',
              'feesEnabled': True,
              'feeSchedule': {'exponent': 1, 'rate': 0.05, 'takerOnly': True, 'rebateRate': 0.15},
              'version': 'v1'},
             {'id': '7202039',
              'question': 'Will Brentford FC win on 2026-11-28?',
              'conditionId': '0x6077282b90f89cd73219317cc12c4b7db8251af6b7746fdbbeee16fa06029704',
              'resolutionSource': 'https://www.premierleague.com/',
              'description': 'In the upcoming Premier League game between Manchester United FC and '
                             'Brentford FC, originally scheduled for Saturday, November 28, 2026 '
                             'at 10:00 AM ET (as of creation of the first market related to the '
                             'event):\n'
                             'If Brentford FC wins, this market will resolve to "Yes".\n'
                             'Otherwise, this market will resolve to "No".\n'
                             "If this market's outcome was definitively determined under its rules "
                             'by the listed resolution sources before abandonment, that outcome '
                             'will stand. Otherwise, the following no-result provisions apply. If '
                             'the game is canceled, does not start by Monday, November 30, 2026, '
                             '11:59 PM ET, or is abandoned with no official result by that time, '
                             'this market will resolve to "No".\n'
                             "The following abandonment rules apply only when this market's "
                             'outcome was not already definitively determined under its rules '
                             'before abandonment. If the game is abandoned, this market will '
                             'resolve according to the official ruling of the governing body or '
                             'event organizers on the abandoned game. If a result is awarded by '
                             "Monday, November 30, 2026, 11:59 PM ET that determines this market's "
                             'outcome, this market will resolve based on the awarded result, '
                             'regardless of the score as played. If no such result is awarded and '
                             'the result as played at the time of abandonment is declared to stand '
                             'by Monday, November 30, 2026, 11:59 PM ET, this market will resolve '
                             'based on the game as played. If the game is replayed or resumed from '
                             'the point of abandonment and has an official result by Monday, '
                             'November 30, 2026, 11:59 PM ET, this market will resolve based on '
                             'the completed result. In any other case, this market will resolve to '
                             '"No".\n'
                             'This market refers only to the outcome within the first 90 minutes '
                             'of regular play plus stoppage time.\n'
                             '\n'
                             'Recognizable abbreviations, alternate or erroneous spellings, '
                             'sponsor names, regional identifiers, and minor formatting '
                             "differences in a team's or player's name will not affect resolution, "
                             'provided the intended team or player can be clearly and uniquely '
                             'identified as a participant in the listed fixture or competition. If '
                             'a listed name has no reasonable connection to any participant, or '
                             'could reasonably refer to more than one participant such that the '
                             'intended team or player cannot be determined, this market will '
                             'resolve as it would if the listed fixture had been canceled '
                             'entirely; a listed participant who cannot be identified does not '
                             'count as inactive or absent. The primary resolution source for this '
                             'market will be official match information from Premier League '
                             '(https://www.premierleague.com/). If the governing body or event '
                             'organizers have not published final statistics within 2 hours of the '
                             "event's conclusion, data from Flashscore may be used; if Flashscore "
                             'data is unavailable, data from Sofascore may be used; if neither is '
                             'available, a consensus of credible reporting may be used. Revisions '
                             'to officially declared final scores made after market resolution '
                             'will not be accounted for in determining the outcome. If no '
                             "acceptable data is available within 24 hours of the event's "
                             'conclusion, this market will resolve to "No".',
              'outcomes': '["Yes", "No"]',
              'active': True,
              'closed': False,
              'groupItemTitle': 'Brentford FC',
              'questionID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13002',
              'gameStartTime': '2026-11-28 15:00:00+00',
              'clobTokenIds': '["48798972005904700516266254783857939085141866873050056008610141476996366896947", '
                              '"101008927254339132705129888715563764866104200996898010423559662380562772994836"]',
              'positionIds': ['1163758146525570172421600930251808485205050750828786825083231722024399274496',
                              '1163758146525570172421600930251808485205050750828786825083231722024399274497'],
              'customLiveness': 900,
              'negRisk': True,
              'negRiskMarketID': '0x73180f8e2769712761bd22d1756cc30292a9ecc0036ed546ceec43a110a13000',
              'negRiskRequestID': '0xc78e0428ee6b1f5177425f6ba292075ab09f953539d20cd5774e27dc82b1e0e2',
              'spread': 0.01,
              'sportsMarketType': 'moneyline',
              'feesEnabled': True,
              'feeSchedule': {'exponent': 1, 'rate': 0.05, 'takerOnly': True, 'rebateRate': 0.15},
              'version': 'v1'}]}


HOCKEY_EVENT = {'id': '1289073',
 'title': 'Red Wings vs. Blue Jackets',
 'markets': [{'id': '6474596',
              'question': 'Red Wings vs. Blue Jackets',
              'conditionId': '0x8c2eb75988b3041e7c098529574d90cab044f42fae4778d6be7af916fe3b461a',
              'resolutionSource': 'https://www.nhl.com/scores',
              'description': 'In the upcoming NHL game between Red Wings and Blue Jackets, '
                             'originally scheduled for Friday, November 27, 2026 at 3:00 PM ET (as '
                             'of creation of the first market related to the event):\n'
                             'If the Red Wings win, the market will resolve to "Red Wings".\n'
                             'If the Blue Jackets win, the market will resolve to "Blue Jackets".\n'
                             "If this market's outcome was definitively determined under its rules "
                             'by the listed resolution sources before abandonment, that outcome '
                             'will stand. Otherwise, the following no-result provisions apply. If '
                             'the game is canceled, does not start by Sunday, November 29, 2026, '
                             '11:59 PM ET, or is abandoned with no official result by that time, '
                             'this market will resolve 50-50.\n'
                             'The result will be determined based on the final score including any '
                             'overtime periods and shootouts. In the event of a shootout, one goal '
                             "will be added to the winning team's score for the purpose of "
                             'resolution.',
              'outcomes': '["Red Wings", "Blue Jackets"]',
              'active': True,
              'closed': False,
              'questionID': '0x5120616111d6285fc1022dfce2bab62d73af5978f1dc4c94ca4ca42b1def52ed',
              'gameStartTime': '2026-11-27 20:00:00+00',
              'clobTokenIds': '["103037561165600571779224165490016899578198443597620729101046946889211302425199", '
                              '"22844513487316275990832165284361761592705546406546156324870960967761501453698"]',
              'positionIds': ['763753833984202581258159980955298514619666555877200530036428665757559160832',
                              '763753833984202581258159980955298514619666555877200530036428665757559160833'],
              'customLiveness': 900,
              'negRisk': False,
              'spread': 0.01,
              'sportsMarketType': 'moneyline',
              'feesEnabled': True,
              'feeSchedule': {'exponent': 1, 'rate': 0.05, 'takerOnly': True, 'rebateRate': 0.15},
              'version': 'v1'}]}


NOW = bot.unix_time(HOCKEY_EVENT["markets"][0]["gameStartTime"]) - 3600


def sx_source(event, predicate="home", sport="soccer", identity=1,
              reverse_teams=False, reverse_outcomes=False):
    """SX wire format with fixture identities; no classifier-produced fields."""
    teams = bot.parse_title(event["title"])
    if sport == "soccer":
        label = "Tie" if predicate == "draw" else teams[0 if predicate == "home" else 1]
        outcomes = [label, "Not " + label]
    else:
        outcomes = list(teams)
    if reverse_teams:
        teams = tuple(reversed(teams))
    if reverse_outcomes:
        outcomes.reverse()
    return {
        "marketHash": "0x" + format(identity, "064x"),
        "type": 1 if sport == "soccer" else 226, "status": "ACTIVE",
        "teamOneName": teams[0], "teamTwoName": teams[1],
        "outcomeOneName": outcomes[0], "outcomeTwoName": outcomes[1],
        "outcomeVoidName": "NO_CONTEST",
        "gameTime": int(bot.unix_time(event["markets"][0]["gameStartTime"])),
        "sportLabel": "Soccer" if sport == "soccer" else "Hockey",
        "sportId": 5 if sport == "soccer" else 6,
        "leagueId": 111, "leagueLabel": "Premier League" if sport == "soccer" else "NHL",
        "sportXeventId": "L" + event["id"], "liveEnabled": True,
    }


class V16DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bot, "_POLY_SERIES_CURSOR", 0),
            patch.object(bot, "_POLY_SERIES_OFFSETS", {}),
            patch.object(bot, "_MATCHED_POLY_SERIES", set()),
            patch.object(bot, "DISCOVERY_STATS", {}),
            patch.object(bot, "MAX_POLY_SERIES", 10),
            patch.object(bot, "MAX_EVENTS_PER_SERIES", 150),
            patch.object(bot, "POLY_MAX_REQUESTS_PER_REFRESH", 140),
            patch.object(bot, "MAX_LIVE_AGE_H", 8),
            patch.object(bot, "MAX_FUTURE_H", 168),
            patch.object(bot.time, "time", return_value=NOW),
            patch.object(bot, "get_json", side_effect=AssertionError("unexpected network")),
            patch.object(bot, "telegram", side_effect=AssertionError("unexpected message")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def poly(self, event=SOCCER_EVENT, sport="soccer"):
        counts = Counter()
        result = bot._poly_markets_from_event(copy.deepcopy(event), sport, "10188", NOW, counts)
        return result, counts

    def sx(self, *sources):
        with patch.object(bot, "get_json", return_value={"data": {"markets": list(sources)}}):
            return bot.discover_sx()

    def test_official_category_tags_cover_unknown_league_codes_without_nfl_confusion(self):
        self.assertEqual(bot.poly_sport_family({"sport": "lal", "tags": "1,100350"}), "soccer")
        self.assertEqual(bot.poly_sport_family({"sport": "liiga", "tags": [{"id": "100088"}]}), "hockey")
        self.assertEqual(bot.poly_sport_family({"sport": "nhl", "tags": "899"}), "hockey")
        self.assertEqual(bot.poly_sport_family({"sport": "nfl", "tags": "1,450"}), "american_football")
        self.assertEqual(bot.poly_sport_family({"sport": "lal", "tags": "100350,100088"}), "unknown")

    def test_many_soccer_leagues_do_not_starve_hockey_or_matched_anchors(self):
        series = {str(1000 + i): "soccer" for i in range(185)}
        series.update({str(2000 + i): "hockey" for i in range(14)})
        series.update({str(3000 + i): "tennis" for i in range(23)})
        series.update({str(4000 + i): "basketball" for i in range(2)})
        anchors = {"3000", "3001", "3002"}
        bot._MATCHED_POLY_SERIES = anchors
        seen = set()
        with patch.object(bot, "MAX_POLY_SERIES", 12):
            for _ in range(110):
                selected, eligible = bot._select_poly_series(series)
                self.assertEqual(eligible, len(series))
                self.assertEqual(len(selected), 12)
                self.assertEqual(len(set(selected)), 12)
                self.assertTrue(anchors <= {sid for sid, _ in selected})
                self.assertIn("hockey", {sport for _, sport in selected})
                self.assertIn("soccer", {sport for _, sport in selected})
                seen.update(sid for sid, _ in selected)
        self.assertEqual(seen, set(series))

    def test_budget_smaller_than_number_of_sports_still_rotates_all_sports(self):
        series = {"1": "basketball", "2": "tennis", "3": "soccer", "4": "hockey", "5": "baseball"}
        seen = set()
        with patch.object(bot, "MAX_POLY_SERIES", 2):
            for _ in range(6):
                selected, _ = bot._select_poly_series(series)
                self.assertEqual(len(selected), 2)
                seen.update(sid for sid, _ in selected)
        self.assertEqual(seen, set(series))

    def test_gamma_uses_end_date_band_not_creation_date_and_retains_fees(self):
        calls = []
        def api(url, params=None):
            if url.endswith("/sports"):
                return [{"sport": "epl", "tags": "100350", "series": "10188"},
                        {"sport": "nhl", "series": "10346"},
                        {"sport": "tennis", "series": "11"}]
            calls.append(dict(params))
            event = {"10188": SOCCER_EVENT, "10346": HOCKEY_EVENT}.get(params["series_id"])
            return [copy.deepcopy(event)] if event else []
        with patch.object(bot, "get_json", side_effect=api):
            result = bot.discover_poly()
        self.assertEqual(len(result), 4)
        for params in calls:
            self.assertNotIn("start_date_min", params)
            if params["series_id"] in {"10188", "10346"}:
                self.assertEqual(bot.unix_time(params["end_date_min"]), NOW - 48 * 3600)
                self.assertEqual(bot.unix_time(params["end_date_max"]), NOW + (168 + 48) * 3600)
            else:
                self.assertNotIn("end_date_min", params)
        self.assertTrue(bot.DISCOVERY_STATS["poly"]["complete"])
        self.assertEqual(bot.DISCOVERY_STATS["poly"]["markets_by_sport"], {"soccer": 3, "hockey": 1})
        for market in result:
            self.assertTrue(market["feesEnabled"])
            self.assertEqual(market["feeSchedule"]["rate"], 0.05)
            self.assertEqual(market["fees_fetched_epoch"], NOW)
            self.assertTrue(market["description"])
            self.assertEqual(market["contract"]["exception_policy"], "review")

    def test_real_price_spread_is_not_misread_as_a_handicap_or_frozen_rule(self):
        original, counts = self.poly()
        self.assertEqual(len(original), 3, counts)
        event = copy.deepcopy(SOCCER_EVENT)
        for market in event["markets"]:
            self.assertGreater(market["spread"], 0)
            market["spread"] = 0.24
        changed, _ = self.poly(event)
        self.assertEqual([p["contract"] for p in original], [p["contract"] for p in changed])

    def test_three_soccer_propositions_produce_six_raw_label_preserving_directions(self):
        poly, counts = self.poly()
        self.assertEqual(len(poly), 3, counts)
        sx = self.sx(*(sx_source(SOCCER_EVENT, predicate, identity=i + 1)
                       for i, predicate in enumerate(("home", "draw", "away"))))
        pairs = bot.pair_markets(poly, sx)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(len({p["key"] for p in pairs}), 6)
        self.assertEqual(Counter(p["sx"]["hash"] for p in pairs), {m["hash"]: 2 for m in sx})
        for pair in pairs:
            proof = pair["settlement_contract"]
            self.assertTrue(contracts.validate_pair_contract(pair))
            self.assertIn(pair["poly_label"], {"Yes", "No"})
            self.assertEqual(pair["sx_label"], pair["sx"]["outcomes"][pair["sx_target"]])
            p_states, s_states = (set(proof["selected_states"][side]) for side in ("poly", "sx"))
            self.assertFalse(p_states & s_states)
            self.assertEqual(p_states | s_states, {"home", "draw", "away"})
            self.assertEqual(proof["compatibility"], "normal_result_only")
            self.assertEqual(proof["exception_policy"], "review")
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["matched_soccer"], 3)

    def test_home_no_cannot_be_replaced_by_away_win(self):
        poly, _ = self.poly()
        home = [p for p in poly if p["contract"]["predicate"] == "home"]
        away = self.sx(sx_source(SOCCER_EVENT, "away"))
        self.assertEqual(bot.pair_markets(home, away), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["contract_selections_not_complementary"], 1)
        correct = bot.pair_markets(home, self.sx(sx_source(SOCCER_EVENT, "home")))
        self.assertEqual({(p["poly_label"], p["sx_label"]) for p in correct}, {
            ("Yes", "Not Manchester United FC"), ("No", "Manchester United FC")})

    def test_reversed_participants_and_raw_outcome_order_keep_assets_bound(self):
        event = copy.deepcopy(SOCCER_EVENT)
        event["markets"] = [event["markets"][0]]
        raw = event["markets"][0]
        raw["outcomes"] = json.dumps(list(reversed(bot.values(raw["outcomes"]))))
        raw["clobTokenIds"] = json.dumps(list(reversed(bot.values(raw["clobTokenIds"]))))
        poly, _ = self.poly(event)
        sx = self.sx(sx_source(event, reverse_teams=True, reverse_outcomes=True))
        pairs = bot.pair_markets(poly, sx)
        self.assertEqual(len(pairs), 2)
        for pair in pairs:
            self.assertTrue(contracts.validate_pair_contract(pair))
            self.assertEqual(pair["settlement_contract"]["team_mapping"], {"0": 1, "1": 0})
            self.assertEqual(pair["poly_asset"], poly[0]["assets"][poly[0]["outcomes"].index(pair["poly_label"])])
        self.assertEqual({(p["poly_label"], p["sx_label"]) for p in pairs}, {
            ("Yes", "Not Manchester United FC"), ("No", "Manchester United FC")})

    def test_verified_hockey_ot_shootouts_matches_only_sx_type_226(self):
        poly, counts = self.poly(HOCKEY_EVENT, "hockey")
        self.assertEqual(len(poly), 1, counts)
        raw = sx_source(HOCKEY_EVENT, sport="hockey")
        pairs = bot.pair_markets(poly, self.sx(raw))
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(contracts.validate_pair_contract(p) for p in pairs))
        self.assertEqual(pairs[0]["settlement_contract"]["scope"], "hockey_full_game_ot_so")
        raw["type"] = 52
        self.assertEqual(self.sx(raw), [])
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["contract_sx_period_type_mismatch"], 1)

    def test_missing_or_regulation_only_hockey_rules_never_enter_catalogue(self):
        for description in (None, "Winner after regulation time only; overtime excluded."):
            with self.subTest(description=description):
                event = copy.deepcopy(HOCKEY_EVENT)
                event["markets"][0]["description"] = description
                markets, counts = self.poly(event, "hockey")
                self.assertEqual(markets, [])
                self.assertEqual(sum(counts.values()), 1)

    def test_nhl_short_names_are_explicit_and_do_not_change_generic_matching(self):
        club_pairs = [
            (("Red Wings", "Blue Jackets"), ("Detroit Red Wings", "Columbus Blue Jackets")),
            (("Golden Knights", "Maple Leafs"), ("Vegas Golden Knights", "Toronto Maple Leafs")),
            (("Jets", "Wild"), ("Winnipeg Jets", "Minnesota Wild")),
        ]
        for short, full in club_pairs:
            with self.subTest(clubs=short):
                event = json.loads(json.dumps(HOCKEY_EVENT).replace("Red Wings", short[0]).replace("Blue Jackets", short[1]))
                poly, counts = self.poly(event, "hockey")
                self.assertEqual(len(poly), 1, counts)
                raw = sx_source(event, sport="hockey")
                raw.update(teamOneName=full[0], teamTwoName=full[1],
                           outcomeOneName=full[0], outcomeTwoName=full[1])
                pairs = bot.pair_markets(poly, self.sx(raw))
                self.assertEqual(len(pairs), 2)
                self.assertTrue(all(contracts.validate_pair_contract(pair) for pair in pairs))
                for a, b in zip(short, full):
                    self.assertFalse(bot.same_team(a, b))
                # Identical nicknames in a different league cannot borrow NHL identities.
                raw["leagueLabel"] = "KHL"
                other_league = self.sx(raw)
                self.assertEqual(bot.pair_markets(poly, other_league), [])
                # Changing only normalized metadata cannot override frozen source evidence.
                other_league[0]["league"] = "NHL"
                self.assertEqual(bot.pair_markets(poly, other_league), [])

    def test_explicit_start_tolerance_matches_ledger_gate_without_changing_legacy(self):
        event = copy.deepcopy(SOCCER_EVENT)
        event["markets"] = [event["markets"][0]]
        poly, _ = self.poly(event)
        raw = sx_source(event)
        original_start = raw["gameTime"]
        raw["gameTime"] = original_start + 1800
        self.assertEqual(len(bot.pair_markets(poly, self.sx(raw))), 2)
        raw["gameTime"] = original_start + 1801
        self.assertEqual(bot.pair_markets(poly, self.sx(raw)), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["contract_start_difference_exceeds_30m"], 1)
        self.assertTrue(bot.match_time(original_start, original_start + 1801))

    def test_new_sports_reject_old_and_far_future_games_by_game_time(self):
        for start in (NOW - 8 * 3600 - 1, NOW + 168 * 3600 + 1):
            with self.subTest(start=start):
                event = copy.deepcopy(SOCCER_EVENT)
                for market in event["markets"]:
                    market["gameStartTime"] = start
                markets, counts = self.poly(event)
                self.assertEqual(markets, [])
                self.assertEqual(counts["outside_discovery_window"], 3)
                raw = sx_source(SOCCER_EVENT)
                raw["gameTime"] = start
                self.assertEqual(self.sx(raw), [])
                self.assertEqual(bot.DISCOVERY_STATS["sx"]["outside_discovery_window"], 1)

    def test_matching_requires_unambiguous_game_and_verified_contracts(self):
        event = copy.deepcopy(SOCCER_EVENT)
        event["markets"] = [event["markets"][0]]
        poly, _ = self.poly(event)
        raw = sx_source(event)
        sx = self.sx(raw)
        duplicate = copy.deepcopy(event["markets"][0])
        duplicate["id"] = "9999999"
        event["markets"].append(duplicate)
        duplicates, _ = self.poly(event)
        self.assertEqual(bot.pair_markets(duplicates, sx), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["ambiguous_poly_markets"], 1)
        raw["gameTime"] += 3 * 3600 + 1
        self.assertEqual(bot.pair_markets(poly, self.sx(raw)), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["no_poly_start_match"], 1)
        missing = copy.deepcopy(sx)
        missing[0].pop("contract")
        self.assertEqual(bot.pair_markets(poly, missing), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["sx_contract_unverified"], 1)
        poly[0].pop("contract")
        self.assertEqual(bot.pair_markets(poly, sx), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["poly_contract_unverified"], 1)

    def test_wrong_participants_are_not_relaxed_to_find_more_matches(self):
        poly, _ = self.poly()
        raw = sx_source(SOCCER_EVENT)
        raw.update(teamOneName="Team Charlie", teamTwoName="Team Delta",
                   outcomeOneName="Team Charlie", outcomeTwoName="Not Team Charlie")
        self.assertEqual(bot.pair_markets(poly, self.sx(raw)), [])
        self.assertEqual(bot.DISCOVERY_STATS["matching"]["no_poly_participant_match"], 1)

    def test_totals_spreads_and_unverified_sport_propositions_remain_excluded(self):
        event = copy.deepcopy(SOCCER_EVENT)
        event["markets"][0]["sportsMarketType"] = "total"
        event["markets"][1]["sportsMarketType"] = "spread"
        event["markets"] = event["markets"][:2]
        markets, counts = self.poly(event)
        self.assertEqual(markets, [])
        self.assertEqual(counts["unsupported_market_type"], 2)
        raw = sx_source(SOCCER_EVENT)
        raw["type"] = 3
        self.assertEqual(self.sx(raw), [])
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["inactive_or_unsupported"], 1)
        raw.update(type=1, sportLabel="Tennis")
        self.assertEqual(self.sx(raw), [])
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["unsupported_sx_proposition_sport"], 1)

    def test_sx_pagination_deduplicates_new_contracts_without_expanding_to_totals(self):
        home = sx_source(SOCCER_EVENT, "home", identity=1)
        draw = sx_source(SOCCER_EVENT, "draw", identity=2)
        responses = [
            {"data": {"markets": [home], "nextKey": "next"}},
            {"data": {"markets": [home, draw]}},
        ]
        with patch.object(bot, "get_json", side_effect=responses) as api:
            markets = bot.discover_sx()
        self.assertEqual(len(markets), 2)
        self.assertTrue(all(m.get("contract") for m in markets))
        self.assertEqual(bot.DISCOVERY_STATS["sx"]["duplicate_market"], 1)
        self.assertTrue(bot.DISCOVERY_STATS["sx"]["complete"])
        self.assertEqual(api.call_args_list[0].args[1]["type"], "1,52,226")
        self.assertEqual(api.call_args_list[1].args[1]["paginationKey"], "next")


if __name__ == "__main__":
    unittest.main(verbosity=2)

