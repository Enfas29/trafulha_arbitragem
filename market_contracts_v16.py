"""Explicit, fail-closed contracts for soccer/hockey PAPER simulations.

This module proves complementary *normal* result sets, not universal settlement
equivalence. SX's void/refund rules differ from Polymarket's no-result/50-50
rules. Exceptional results therefore require review and never an inferred
refund. No orders, network calls, balances or mutable runtime configuration.

Rules checked 2026-11-27 against official sources:
https://docs.sx.bet/api-reference/market-types
https://help.sx.bet/en/articles/3613418-general-betting-rules
https://docs.polymarket.com/market-data/market-details
https://docs.polymarket.com/concepts/resolution
Gamma examples: markets 7202037/7202038 (soccer), 6474596/7360020 (hockey).
Unknown description templates are excluded until separately reviewed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 16
EXPLICIT_SPORTS = frozenset({"soccer", "hockey"})
SCOPES = {"soccer": "soccer_regulation_90",
          "hockey": "hockey_full_game_ot_so"}
UNIVERSES = {"soccer": ["home", "draw", "away"],
             "hockey": ["home", "away"]}
SX_RULES_URL = "https://help.sx.bet/en/articles/3613418-general-betting-rules"
SX_TYPES_URL = "https://docs.sx.bet/api-reference/market-types"
MAX_CONTRACT_BYTES = 32768
_ET = ZoneInfo("America/New_York")

# Exclude changing prices, status, volume and result fields. Every field below
# can change the identity, proposition or resolution interpretation. Both token
# arrays are frozen: version, rather than their presence, selects the tradable ID.
# Gamma's ``spread`` is a changing bid/ask spread, not a sports handicap.
POLY_FIELDS = (
    "id", "conditionId", "questionID", "question", "description",
    "resolutionSource", "sportsMarketType", "groupItemTitle", "outcomes",
    "version", "clobTokenIds", "positionIds", "gameStartTime", "negRisk",
    "negRiskMarketID", "negRiskRequestID", "line", "handicap",
    "period", "marketType", "customLiveness", "fallbackResolutionType",
    "resolutionMechanism",
)
SX_FIELDS = (
    "marketHash", "type", "sportXeventId", "gameTime", "teamOneName",
    "teamTwoName", "outcomeOneName", "outcomeTwoName", "outcomeVoidName",
    "sportId", "sportLabel", "leagueId", "leagueLabel", "participantOneId",
    "participantTwoId", "isQuarterLineMarket", "line", "handicap", "spread",
    "period", "segment", "legs", "group", "marketName", "marketTypeName",
)


class ContractError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


def _need(ok, reason):
    if not ok:
        raise ContractError(reason)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _norm(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", "", word)
                    for word in value.split()).strip()


def _text(value, reason):
    _need(isinstance(value, str) and bool(value.strip()), reason)
    return value


def _array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ContractError("contract_invalid_array") from None
    _need(isinstance(value, (list, tuple)), "contract_invalid_array")
    return list(value)


def _snapshot(raw, fields):
    _need(isinstance(raw, dict), "contract_source_not_object")
    result = {key: copy.deepcopy(raw.get(key)) for key in fields}
    try:
        _json(result)
    except (TypeError, ValueError):
        raise ContractError("contract_source_not_json") from None
    return result


def _epoch(value):
    _need(not isinstance(value, bool), "contract_invalid_start")
    try:
        if isinstance(value, (int, float)):
            epoch = float(value)
        else:
            value = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(value)
            _need(dt.tzinfo is not None, "contract_start_timezone_missing")
            epoch = dt.timestamp()
        _need(0 < epoch < 32503680000 and epoch == int(epoch), "contract_invalid_start")
        return int(epoch)
    except (TypeError, ValueError, OverflowError):
        raise ContractError("contract_invalid_start") from None


def _teams(teams):
    _need(isinstance(teams, (tuple, list)) and len(teams) == 2,
          "contract_participants_missing")
    result = [_text(x, "contract_participants_missing") for x in teams]
    _need(_norm(result[0]) and _norm(result[0]) != _norm(result[1]),
          "contract_participants_ambiguous")
    return result


def _team_index(value, teams, match_team):
    # Inside one provider, exact normalized names must corroborate the injected
    # matcher. Fuzzy cross-provider aliases belong to the separate fixture match.
    matches = [i for i, name in enumerate(teams)
               if _norm(value) == _norm(name) and bool(match_team(value, name))]
    _need(len(matches) == 1, "contract_participant_not_unique")
    return matches[0]


def _id(value, reason):
    _need(not isinstance(value, bool) and value is not None, reason)
    value = str(value)
    _need(bool(re.fullmatch(r"[0-9]+", value)), reason)
    return value


def _hash(value, reason):
    _need(isinstance(value, str) and bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", value)), reason)
    return value.lower()


def _sport(sport):
    _need(isinstance(sport, str) and sport in EXPLICIT_SPORTS, "contract_unsupported_sport")
    return sport


def _plain_lines(raw):
    _need(raw.get("isQuarterLineMarket") is not True,
          "contract_quarter_lines_unsupported")
    for key in ("line", "spread", "handicap", "period", "segment", "legs"):
        _need(raw.get(key) in (None, "", []), "contract_partial_or_line_market")


def _normal_description(raw):
    return " ".join(_text(raw.get("description"), "contract_missing_rules").split())


def _deadline_epoch(value):
    """Interpret the template's ET deadline using Eastern DST rules.

    A nonexistent or ambiguous local time cannot establish a safe deadline.
    The observed templates use 11:59 PM, away from either DST transition.
    """
    reason = "contract_poly_resolution_deadline_invalid"
    try:
        local = datetime.strptime(value, "%A, %B %d, %Y, %I:%M %p ET")
        _need(local.strftime("%A") == value.split(",", 1)[0], reason)
        first, second = (local.replace(tzinfo=_ET, fold=fold) for fold in (0, 1))
        _need(first.utcoffset() == second.utcoffset(), reason)
        epoch = int(first.timestamp())
        _need(datetime.fromtimestamp(epoch, _ET).replace(tzinfo=None) == local, reason)
        return epoch
    except (TypeError, ValueError, OverflowError):
        raise ContractError(reason) from None


_PRE_ABANDON = (
    "If this market's outcome was definitively determined under its rules by the "
    "listed resolution sources before abandonment, that outcome will stand. "
    "Otherwise, the following no-result provisions apply. "
)
_NAME_RULES = (
    "Recognizable abbreviations, alternate or erroneous spellings, sponsor names, "
    "regional identifiers, and minor formatting differences in a team's or player's "
    "name will not affect resolution, provided the intended team or player can be "
    "clearly and uniquely identified as a participant in the listed fixture or "
    "competition. If a listed name has no reasonable connection to any participant, "
    "or could reasonably refer to more than one participant such that the intended "
    "team or player cannot be determined, this market will resolve as it would if "
    "the listed fixture had been canceled entirely; a listed participant who cannot "
    "be identified does not count as inactive or absent. "
)
_FALLBACK = (
    "If the governing body or event organizers have not published final statistics "
    "within 2 hours of the event's conclusion, data from Flashscore may be used; if "
    "Flashscore data is unavailable, data from Sofascore may be used; if neither is "
    "available, a consensus of credible reporting may be used. Revisions to "
    "officially declared final scores made after market resolution will not be "
    "accounted for in determining the outcome. "
)
_SOCCER_SCOPE = "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
_NHL_SCOPE = (
    "The result will be determined based on the final score including any overtime "
    "periods and shootouts. In the event of a shootout, one goal will be added to "
    "the winning team's score for the purpose of resolution."
)
_HOCKEY_OFFICIAL_SCOPE = (
    "The official final result will be used, including overtime and any shootout. "
    "If the official result is a draw, this market will resolve 50-50."
)


def _intro_pattern(teams, noun="game"):
    # Date/league text may vary, but cannot introduce another sentence or rule.
    return (r"In the upcoming (?P<league>[^.!?]+?) " + noun + " between "
            + re.escape(teams[0]) + " and " + re.escape(teams[1])
            + r", originally scheduled for [A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M ET \(as of creation of the first market related to the event\): ")


def _no_result_pattern(noun, final):
    return (re.escape(_PRE_ABANDON + f"If the {noun} is canceled, does not start by ")
            + r"(?P<deadline>[A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M ET)"
            + re.escape(", or is abandoned with no official result by that time, this market will resolve " + final + "."))


def _soccer_rules(raw, teams, predicate):
    final = '"Yes"' if predicate == "draw" else '"No"'
    team = teams[0 if predicate == "home" else 1] if predicate != "draw" else None
    first = ("If the game ends in a draw" if team is None else f"If {team} wins")
    pattern = (_intro_pattern(teams)
               + re.escape(first + ', this market will resolve to "Yes". Otherwise, this market will resolve to "No". ')
               + _no_result_pattern("game", "to " + final) + " "
               + re.escape("The following abandonment rules apply only when this market's outcome was not already definitively determined under its rules before abandonment. If the game is abandoned, this market will resolve according to the official ruling of the governing body or event organizers on the abandoned game. If a result is awarded by ")
               + r"(?P=deadline)"
               + re.escape(" that determines this market's outcome, this market will resolve based on the awarded result, regardless of the score as played. If no such result is awarded and the result as played at the time of abandonment is declared to stand by ")
               + r"(?P=deadline)"
               + re.escape(", this market will resolve based on the game as played. If the game is replayed or resumed from the point of abandonment and has an official result by ")
               + r"(?P=deadline)"
               + re.escape(", this market will resolve based on the completed result. In any other case, this market will resolve to " + final + ". " + _SOCCER_SCOPE + " " + _NAME_RULES
                           + "The primary resolution source for this market will be official match information from ")
               + r"(?P=league)"
               + re.escape(" (" + raw["resolutionSource"] + "). " + _FALLBACK
                           + "If no acceptable data is available within 24 hours of the event's conclusion, this market will resolve to " + final + "."))
    match = re.fullmatch(pattern, _normal_description(raw))
    _need(match is not None, "contract_poly_soccer_rules_unrecognized")
    return {
        "profile": "poly_soccer_2026_deadline_awarded_result",
        "no_result_outcome": "Yes" if predicate == "draw" else "No",
        "deadline_text": match.group("deadline"),
        "abandoned_or_awarded_result": "official_result_may_stand",
        "missing_data": "24h_then_no_result_outcome",
        "requires_review": ["postponed", "canceled", "abandoned", "awarded_result", "missing_data"],
    }


def _hockey_rules(raw, teams, labels, match_team):
    # NHL template and the separately observed Liiga official-win template.
    lines = []
    for name in teams:
        label = labels[next(i for i, x in enumerate(labels) if _norm(x) == _norm(name))]
        # SHL markets 7347745/7347746 use the same complete rules but omit
        # the English article before the team name. No other text is relaxed.
        lines.append(r"If (?:the )?" + re.escape(f'{name} win, the market will resolve to "{label}".'))
    nhl = (_intro_pattern(teams) + " ".join(lines) + " "
           + _no_result_pattern("game", "50-50") + " " + re.escape(_NHL_SCOPE))
    match = re.fullmatch(nhl, _normal_description(raw))
    profile = "poly_hockey_final_score_ot_so_2026"
    if match is None:
        lines = [f'This market will resolve to "{name}" if {name} is officially credited with the match win.'
                 for name in teams]
        official = (_intro_pattern(teams, "match") + re.escape(" ".join(lines) + " " + _HOCKEY_OFFICIAL_SCOPE + " ")
                    + _no_result_pattern("match", "50-50") + " "
                    + re.escape("Resolution will be based on official ") + r"(?P=league)"
                    + re.escape(" results. If official results have not been published within 24 hours after the match concludes, a consensus of credible reporting may be used instead."))
        match = re.fullmatch(official, _normal_description(raw))
        profile = "poly_hockey_official_win_ot_so_2026"
    _need(match is not None, "contract_poly_hockey_rules_unrecognized")
    return {
        "profile": profile, "no_result_outcome": "50-50",
        "deadline_text": match.group("deadline"),
        "split_is_stake_refund": False,
        "requires_review": ["postponed", "canceled", "abandoned", "awarded_result", "draw", "split_payout"],
    }


def classify_poly_market(raw_market, event, sport, teams, match_team):
    sport, teams = _sport(sport), _teams(teams)
    raw = _snapshot(raw_market, POLY_FIELDS)
    _need(raw["sportsMarketType"] == "moneyline", "contract_poly_not_moneyline")
    _plain_lines(raw)
    market_id = _id(raw["id"], "contract_poly_invalid_id")
    _hash(raw["conditionId"], "contract_poly_condition_missing")
    _need(isinstance(event, dict) and event.get("id") not in (None, ""), "contract_poly_event_missing")
    event_id = str(event["id"])
    _text(event.get("title"), "contract_poly_event_title_missing")
    source = _text(raw["resolutionSource"], "contract_resolution_source_missing")
    _need(source.startswith("https://"), "contract_resolution_source_unrecognized")
    version = raw["version"]
    _need(isinstance(version, str) and version in {"v1", "v2"}, "contract_poly_version_unrecognized")
    identifier_field = "positionIds" if version == "v2" else "clobTokenIds"
    assets = [_id(x, "contract_poly_asset_invalid") for x in _array(raw[identifier_field])]
    labels = [_text(x, "contract_poly_label_missing") for x in _array(raw["outcomes"])]
    _need(len(labels) == 2 and len(set(labels)) == 2 and len(assets) == 2 and len(set(assets)) == 2,
          "contract_poly_not_binary")
    question = _text(raw["question"], "contract_poly_question_missing")
    universe = UNIVERSES[sport]
    if sport == "soccer":
        _need({_norm(x) for x in labels} == {"yes", "no"}, "contract_poly_soccer_not_yes_no")
        group = _text(raw["groupItemTitle"], "contract_poly_predicate_missing")
        if group == f"Draw ({event['title']})" and question == f"Will {event['title']} end in a draw?":
            predicate = "draw"
        else:
            team_index = _team_index(group, teams, match_team)
            _need(re.fullmatch(r"Will " + re.escape(teams[team_index]) + r" win on \d{4}-\d{2}-\d{2}\?", question)
                  is not None, "contract_poly_question_predicate_mismatch")
            predicate = "home" if team_index == 0 else "away"
        exceptional_rules = _soccer_rules(raw, teams, predicate)
        winning_states = [[predicate] if _norm(label) == "yes" else [x for x in universe if x != predicate]
                          for label in labels]
    else:
        indices = [_team_index(label, teams, match_team) for label in labels]
        _need(set(indices) == {0, 1}, "contract_poly_hockey_labels_not_teams")
        _need(question == event["title"], "contract_poly_hockey_question_mismatch")
        _need(raw["groupItemTitle"] in (None, ""), "contract_poly_hockey_group_unknown")
        predicate = None
        exceptional_rules = _hockey_rules(raw, teams, labels, match_team)
        winning_states = [["home" if idx == 0 else "away"] for idx in indices]
    start = _epoch(raw["gameStartTime"])
    exceptional_rules["deadline_epoch"] = _deadline_epoch(exceptional_rules["deadline_text"])
    _need(start <= exceptional_rules["deadline_epoch"],
          "contract_poly_start_after_resolution_deadline")
    return {
        "schema_version": SCHEMA_VERSION, "source": "poly", "sport": sport,
        "scope": SCOPES[sport], "universe": list(universe),
        "winning_states": winning_states, "labels": labels, "teams": teams,
        "market_id": market_id, "event_id": event_id, "event_title": event["title"],
        "start": start, "assets": assets,
        "identifier_field": identifier_field, "predicate": predicate,
        "source_fields": raw, "source_fingerprint": _digest(raw),
        "exception_policy": "review", "exceptional_rules": exceptional_rules,
    }


def _sx_soccer_predicate(label, teams, match_team):
    negative = label.casefold().startswith("not ")
    name = label[4:] if negative else label
    if _norm(name) in {"tie", "draw"}:
        state = "draw"
    else:
        state = "home" if _team_index(name, teams, match_team) == 0 else "away"
    return state, negative


def classify_sx_market(raw_market, sport, teams, match_team):
    sport, teams = _sport(sport), _teams(teams)
    raw = _snapshot(raw_market, SX_FIELDS)
    _plain_lines(raw)
    expected_type = 1 if sport == "soccer" else 226
    _need(type(raw["type"]) is int and raw["type"] == expected_type, "contract_sx_period_type_mismatch")
    market_hash = _hash(raw["marketHash"], "contract_sx_hash_invalid")
    event_id = _text(raw["sportXeventId"], "contract_sx_event_missing")
    _need(type(raw["sportId"]) is int and type(raw["leagueId"]) is int,
          "contract_sx_sport_league_missing")
    sport_label = _norm(raw["sportLabel"])
    _need(sport_label in ({"soccer", "football"} if sport == "soccer" else {"hockey", "ice hockey"}),
          "contract_sx_sport_label_mismatch")
    _need(raw["outcomeVoidName"] == "NO_CONTEST", "contract_sx_void_rule_unrecognized")
    for i, key in enumerate(("teamOneName", "teamTwoName")):
        _need(_team_index(raw[key], teams, match_team) == i, "contract_sx_participant_order_mismatch")
    labels = [_text(raw[key], "contract_sx_label_missing") for key in ("outcomeOneName", "outcomeTwoName")]
    _need(len(set(labels)) == 2, "contract_sx_duplicate_labels")
    universe = UNIVERSES[sport]
    if sport == "soccer":
        predicates = [_sx_soccer_predicate(label, teams, match_team) for label in labels]
        _need(predicates[0][0] == predicates[1][0] and predicates[0][1] != predicates[1][1],
              "contract_sx_soccer_not_binary_complements")
        winning_states = [[x for x in universe if x != state] if negative else [state]
                          for state, negative in predicates]
        exceptional_rules = {
            "profile": "sx_soccer_1x2_rules_2026_11_27",
            "postponement": "void_if_not_played_within_12h_original_start",
            "incomplete": "uncompleted_period_void_after_12h",
            "live_abandoned": "void_if_not_completed_same_local_day",
            "forfeit": "no_action_refund",
            "requires_review": ["postponed", "canceled", "abandoned", "awarded_result", "void"],
        }
    else:
        indices = [_team_index(label, teams, match_team) for label in labels]
        _need(set(indices) == {0, 1}, "contract_sx_hockey_labels_not_teams")
        winning_states = [["home" if idx == 0 else "away"] for idx in indices]
        exceptional_rules = {
            "profile": "sx_hockey_226_rules_2026_11_27",
            "minimum_play": "55_minutes_for_action",
            "early_suspension": "refund_before_55_minutes",
            "void": "stake_refund",
            "requires_review": ["postponed", "canceled", "abandoned", "awarded_result", "void"],
        }
    exceptional_rules["rules_url"] = SX_RULES_URL
    exceptional_rules["types_url"] = SX_TYPES_URL
    return {
        "schema_version": SCHEMA_VERSION, "source": "sx", "sport": sport,
        "scope": SCOPES[sport], "universe": list(universe),
        "winning_states": winning_states, "labels": labels, "teams": teams,
        "market_hash": market_hash, "event_id": event_id,
        "start": _epoch(raw["gameTime"]), "market_type": expected_type,
        "source_fields": raw, "source_fingerprint": _digest(raw),
        "exception_policy": "review", "exceptional_rules": exceptional_rules,
    }


def _validate_source(source):
    _need(isinstance(source, dict), "contract_source_missing")
    raw = source.get("source_fields")
    _need(isinstance(raw, dict) and source.get("source_fingerprint") == _digest(raw),
          "contract_source_fingerprint_invalid")
    matcher = lambda a, b: _norm(a) == _norm(b)
    if source.get("source") == "poly":
        expected = classify_poly_market(raw, {"id": source.get("event_id"), "title": source.get("event_title")},
                                        source.get("sport"), source.get("teams"), matcher)
    else:
        _need(source.get("source") == "sx", "contract_source_unknown")
        expected = classify_sx_market(raw, source.get("sport"), source.get("teams"), matcher)
    for key, value in expected.items():
        _need(source.get(key) == value, "contract_source_semantics_changed")
    return expected


def _mapping(value):
    _need(isinstance(value, dict), "contract_team_mapping_invalid")
    _need(set(value) in ({0, 1}, {"0", "1"}), "contract_team_mapping_invalid")
    result = {str(k): v for k, v in value.items()}
    _need(all(type(v) is int for v in result.values()) and set(result.values()) == {0, 1},
          "contract_team_mapping_invalid")
    return result


def soccer_live_period_is_regulation(value):
    """Positive period evidence only; a clock/minute never proves the scope.

    The soccer contract excludes extra time and penalties. An unknown period
    is therefore insufficient even when a provider also says the event is live.
    """
    if not isinstance(value, str):
        return False
    period = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    return period in {
        "1h", "2h", "ht", "first half", "second half", "1st half", "2nd half",
        "half time", "halftime", "firsthalf", "secondhalf",
    }


def _selected_display(source, index):
    states = source["winning_states"][index]
    teams = source["teams"]
    if states == ["home"]:
        return teams[0] + " vence"
    if states == ["away"]:
        return teams[1] + " vence"
    if states == ["draw"]:
        return "Empate"
    if set(states) == {"draw", "away"}:
        return teams[0] + " não vence (inclui empate)"
    if set(states) == {"home", "draw"}:
        return teams[1] + " não vence (inclui empate)"
    if set(states) == {"home", "away"}:
        return "Sem empate (qualquer equipa vence)"
    raise ContractError("contract_display_state_unsupported")


def _build_certificate(poly, sx, poly_index, sx_target, team_mapping):
    poly, sx = _validate_source(poly), _validate_source(sx)
    _need(poly["sport"] == sx["sport"] and poly["scope"] == sx["scope"], "contract_scope_mismatch")
    _need(type(poly_index) is int and poly_index in (0, 1)
          and type(sx_target) is int and sx_target in (0, 1), "contract_selection_invalid")
    mapping = _mapping(team_mapping)
    # Fixture matching is performed by discovery; retain and constrain that
    # mapping rather than pretending the two providers share participant IDs.
    # Never let a supplied mapping contradict an unambiguous exact normalized
    # identity. Alias/fuzzy evidence still belongs to discovery's matcher.
    for i, name in enumerate(poly["teams"]):
        exact = [j for j, other in enumerate(sx["teams"]) if _norm(name) == _norm(other)]
        if len(exact) == 1:
            _need(mapping[str(i)] == exact[0], "contract_team_mapping_contradicts_identity")
    _need(abs(poly["start"] - sx["start"]) <= 10800, "contract_fixture_start_mismatch")
    _need(sx["start"] <= poly["exceptional_rules"]["deadline_epoch"],
          "contract_fixture_start_after_resolution_deadline")
    state_map = {"home": "home" if mapping["0"] == 0 else "away",
                 "away": "home" if mapping["1"] == 0 else "away", "draw": "draw"}
    p_states = {state_map[x] for x in poly["winning_states"][poly_index]}
    s_states = set(sx["winning_states"][sx_target])
    universe = sx["universe"]
    _need(not p_states & s_states and p_states | s_states == set(universe),
          "contract_selections_not_complementary")
    poly.update(selection_index=poly_index, selection_label=poly["labels"][poly_index],
                asset_id=poly["assets"][poly_index])
    sx.update(selection_index=sx_target, selection_label=sx["labels"][sx_target])
    certificate = {
        "schema_version": SCHEMA_VERSION, "sport": sx["sport"], "scope": sx["scope"],
        "mode": "PAPER", "compatibility": "normal_result_only", "exception_policy": "review",
        "universe": universe, "team_mapping": mapping, "poly": poly, "sx": sx,
        "selected_states": {"poly": [x for x in universe if x in p_states],
                            "sx": [x for x in universe if x in s_states]},
        "poly_display": _selected_display(poly, poly_index),
        "sx_display": _selected_display(sx, sx_target),
    }
    certificate["digest"] = _digest(certificate)
    return certificate


def _matches_normalized(source, normalized):
    _need(isinstance(normalized, dict), "contract_normalized_source_missing")
    _need(normalized.get("sport") == source["sport"]
          and list(normalized.get("teams") or []) == source["teams"]
          and list(normalized.get("outcomes") or []) == source["labels"]
          and str(normalized.get("event_id")) == source["event_id"]
          and normalized.get("start") == source["start"], "contract_normalized_identity_mismatch")
    if source["source"] == "poly":
        _need(str(normalized.get("id")) == source["market_id"]
              and list(normalized.get("assets") or []) == source["assets"]
              and normalized.get("version") == source["source_fields"]["version"]
              and normalized.get("type") == "moneyline", "contract_poly_identity_mismatch")
    else:
        _need(str(normalized.get("hash", "")).lower() == source["market_hash"]
              and normalized.get("type") == source["market_type"]
              and normalized.get("sport_id") == source["source_fields"]["sportId"]
              and normalized.get("league_id") == source["source_fields"]["leagueId"],
              "contract_sx_identity_mismatch")


def make_pair_contract(p, s, poly_index, sx_target, team_mapping):
    """Return evidence for a normal-result complementary PAPER pair, or raise."""
    _need(isinstance(p, dict) and isinstance(s, dict), "contract_normalized_source_missing")
    poly, sx = p.get("contract"), s.get("contract")
    certificate = _build_certificate(poly, sx, poly_index, sx_target, team_mapping)
    _matches_normalized(certificate["poly"], p)
    _matches_normalized(certificate["sx"], s)
    return certificate


def _validate_certificate(contract):
    _need(isinstance(contract, dict), "contract_missing")
    _need(len(_json(contract).encode("utf-8")) <= MAX_CONTRACT_BYTES, "contract_too_large")
    poly, sx = contract.get("poly"), contract.get("sx")
    _need(isinstance(poly, dict) and isinstance(sx, dict), "contract_sources_missing")
    rebuilt = _build_certificate(poly, sx, poly.get("selection_index"), sx.get("selection_index"),
                                 contract.get("team_mapping"))
    # Recompute semantics as well as digest: a modified state set with a freshly
    # recomputed checksum is still not a valid proof.
    _need(contract == rebuilt, "contract_certificate_invalid")
    return contract


def validate_pair_contract(pair):
    """Strict validator for new-family pairs; no timestamp freshness decisions."""
    try:
        _need(isinstance(pair, dict), "contract_pair_missing")
        contract = _validate_certificate(pair.get("settlement_contract"))
        p, s = pair["poly"], pair["sx"]
        _matches_normalized(contract["poly"], p)
        _matches_normalized(contract["sx"], s)
        _need(p.get("contract") == {k: v for k, v in contract["poly"].items()
                                   if k not in {"selection_index", "selection_label", "asset_id"}}
              and s.get("contract") == {k: v for k, v in contract["sx"].items()
                                       if k not in {"selection_index", "selection_label"}},
              "contract_discovery_proof_mismatch")
        _need(str(pair.get("poly_asset")) == contract["poly"]["asset_id"]
              and pair.get("poly_label") == contract["poly"]["selection_label"]
              and type(pair.get("sx_target")) is int
              and pair["sx_target"] == contract["sx"]["selection_index"]
              and pair.get("sx_label") == contract["sx"]["selection_label"],
              "contract_pair_selection_mismatch")
        return True
    except (ContractError, TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        return False


def validate_record_contract(record):
    """Bind frozen evidence to ledger identities; ignore unrelated DB columns."""
    try:
        record = dict(record)
        raw = record.get("contract_evidence_json")
        _need(isinstance(raw, str) and len(raw.encode("utf-8")) <= MAX_CONTRACT_BYTES,
              "contract_record_evidence_missing")
        contract = _validate_certificate(json.loads(raw))
        p, s = contract["poly"], contract["sx"]
        _need(record.get("sport") == contract["sport"]
              and str(record.get("poly_market")) == p["market_id"]
              and str(record.get("poly_token_id")) == p["asset_id"]
              and record.get("poly_side") == p["selection_label"]
              and str(record.get("sx_market", "")).lower() == s["market_hash"]
              and record.get("sx_side") == s["selection_label"]
              and type(record.get("sx_outcome_one")) is int
              and record["sx_outcome_one"] == (1 if s["selection_index"] == 0 else 0),
              "contract_record_identity_mismatch")
        if record.get("game_start_epoch") is not None:
            _need(record["game_start_epoch"] == s["start"], "contract_record_start_mismatch")
        if record.get("fixture_key") is not None:
            _need(record["fixture_key"] == contract["sport"] + ":sx:" + s["event_id"],
                  "contract_record_fixture_mismatch")
        return True
    except (ContractError, TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        return False


def verify_poly_source(contract, raw_market):
    """Check fresh official resolution fields against the complete certificate."""
    try:
        contract = _validate_certificate(contract)
        return _snapshot(raw_market, POLY_FIELDS) == contract["poly"]["source_fields"]
    except (ContractError, TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        return False


def verify_sx_source(contract, raw_market):
    """Status and result can change; rule/identity changes require review."""
    try:
        contract = _validate_certificate(contract)
        return _snapshot(raw_market, SX_FIELDS) == contract["sx"]["source_fields"]
    except (ContractError, TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
        return False
