"""Short-lived, public evidence for LIVE_CONFIRMADO in the PAPER simulator.

Only four public GETs are made. No orders, wallet operations, price caches or
unverified fallback are provided. Source contracts checked 2026-11-27:
https://docs.polymarket.com/api-reference/events/get-event-by-id
https://docs.polymarket.com/market-data/market-details#market-timing
https://docs.sx.bet/api-reference/get-fixture-status
https://docs.sx.bet/api-reference/fixture-statuses
https://docs.sx.bet/developers/betting-delays
https://docs.sx.bet/api-reference/get-metadata-obv3

The three-second validity starts BEFORE the requests. This is a local upper
bound on observation age, not a guarantee about either provider's data lag.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
import math
import threading
import time
from urllib.parse import quote


GAMMA_EVENTS = "https://gamma-api.polymarket.com/events/"
SX_MARKETS = "https://api.sx.bet/markets/find"
SX_FIXTURES = "https://api.sx.bet/fixture/status"
SX_METADATA = "https://api.sx.bet/metadata/obv3"
REQUEST_TIMEOUT = 2.0
EVIDENCE_TTL = 3.0
METHOD = "official_live_v1"

# Global limits also apply when callers overlap. Four slots must be reserved
# before submitting a group, so timed-out HTTP workers cannot create a queue.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-public")
_SLOTS = threading.BoundedSemaphore(4)


class LiveCheckError(Exception):
    """No live evidence may be used; reason is a safe, public diagnostic token."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _require(condition, reason):
    if not condition:
        raise LiveCheckError(reason)


def _identifier(value, reason):
    if type(value) is int and value >= 0:
        value = str(value)
    _require(isinstance(value, str) and 0 < len(value) <= 256
             and value == value.strip()
             and not any(ord(c) < 32 or ord(c) == 127 for c in value), reason)
    return value


def _label(value, reason):
    _require(isinstance(value, str) and 0 < len(value) <= 256
             and value == value.strip()
             and not any(ord(c) < 32 or ord(c) == 127 for c in value), reason)
    return value


def _number(value, reason, *, positive=False):
    _require(type(value) in (int, float), reason)
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise LiveCheckError(reason) from None
    _require(math.isfinite(result) and (result > 0 if positive else result >= 0), reason)
    return result


def _integer(value, reason, *, positive=False):
    result = _number(value, reason, positive=positive)
    _require(result.is_integer(), reason)
    return int(result)


def _source_id(value, expected, reason):
    _require(_identifier(value, reason) == expected, reason)


def _array(value, reason):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise LiveCheckError(reason) from None
    _require(isinstance(value, (list, tuple)) and len(value) == 2, reason)
    return list(value)


def _labels(value, reason):
    result = [_label(item, reason) for item in _array(value, reason)]
    _require(result[0] != result[1], reason)
    return result


def _assets(value, reason):
    result = [_identifier(item, reason) for item in _array(value, reason)]
    _require(result[0] != result[1], reason)
    return result


def _one(rows, key, expected, reason):
    _require(isinstance(rows, list), reason)
    matches = [row for row in rows if isinstance(row, dict)
               and type(row.get(key)) in (str, int)
               and str(row[key]) == expected]
    _require(len(matches) == 1, reason)
    return matches[0]


def _success(payload, reason):
    _require(isinstance(payload, dict) and payload.get("status") == "success", reason)
    return payload.get("data")


def _catalogue(pair):
    _require(isinstance(pair, dict), "invalid_pair")
    poly, sx = pair.get("poly"), pair.get("sx")
    _require(isinstance(poly, dict) and isinstance(sx, dict), "invalid_pair")
    result = {
        "pair_key": _identifier(pair.get("key"), "invalid_pair_key"),
        "poly_event_id": _identifier(poly.get("event_id"), "missing_poly_event_id"),
        "poly_market_id": _identifier(poly.get("id"), "missing_poly_market_id"),
        "poly_asset_id": _identifier(pair.get("poly_asset"), "missing_poly_asset_id"),
        "poly_side": _label(pair.get("poly_label"), "missing_poly_side"),
        "sx_event_id": _identifier(sx.get("event_id"), "missing_sx_event_id"),
        "sx_market_hash": _identifier(sx.get("hash"), "missing_sx_market_hash"),
        "sx_side": _label(pair.get("sx_label"), "missing_sx_side"),
    }
    _require(isinstance(poly.get("type"), str)
             and poly["type"].strip().lower() == "moneyline", "poly_type_mismatch")
    _require(type(sx.get("type")) is int and sx["type"] in (52, 226), "sx_type_mismatch")
    _require(sx.get("live_enabled") is True, "sx_catalogue_live_not_enabled")
    _require(type(pair.get("sx_target")) is int and pair["sx_target"] in (0, 1),
             "invalid_sx_target")
    result["sx_target"] = pair["sx_target"]
    for name in ("sport_id", "league_id"):
        _require(type(sx.get(name)) is int and sx[name] > 0, "invalid_sx_" + name)
    _number(sx.get("start"), "invalid_sx_game_time", positive=True)
    poly_labels = _labels(poly.get("outcomes"), "invalid_poly_catalogue_outcomes")
    poly_assets = _assets(poly.get("assets"), "invalid_poly_catalogue_assets")
    _require(result["poly_side"] in poly_labels, "poly_side_mismatch")
    _require(poly_assets[poly_labels.index(result["poly_side"])] == result["poly_asset_id"],
             "poly_asset_side_mismatch")
    sx_labels = _labels(sx.get("outcomes"), "invalid_sx_catalogue_outcomes")
    _require(sx_labels[result["sx_target"]] == result["sx_side"], "sx_side_mismatch")
    _labels(sx.get("teams"), "invalid_sx_catalogue_teams")
    return result


def _parallel_gets(requests, get_json):
    reserved = 0
    while reserved < 4 and _SLOTS.acquire(blocking=False):
        reserved += 1
    if reserved != 4:
        for _ in range(reserved):
            _SLOTS.release()
        raise LiveCheckError("live_sources_busy")
    futures = {}
    submitted = 0
    try:
        for name, url, params in requests:
            future = _POOL.submit(get_json, url, params=params, timeout=REQUEST_TIMEOUT)
            submitted += 1
            future.add_done_callback(lambda _: _SLOTS.release())
            futures[name] = future
    except Exception:
        for _ in range(4 - submitted):
            _SLOTS.release()
        for future in futures.values():
            future.cancel()
        raise LiveCheckError("live_sources_unavailable") from None
    _, pending = wait(futures.values(), timeout=REQUEST_TIMEOUT)
    if pending:
        for future in pending:
            future.cancel()
        raise LiveCheckError("live_sources_timeout")
    result = {}
    for name, future in futures.items():
        try:
            result[name] = future.result()
        except Exception:
            # API exceptions can contain URLs or credentials. Never expose them.
            raise LiveCheckError(name + "_source_unavailable") from None
    return result


def _check_poly(event, identity, catalogue):
    _require(isinstance(event, dict), "poly_event_unavailable")
    _source_id(event.get("id"), identity["poly_event_id"], "poly_event_mismatch")
    _require(event.get("live") is True, "poly_not_live")
    _require(event.get("ended") is False, "poly_ended_or_unknown")
    # Optional fields may corroborate, but may never contradict, the live flags.
    for field, expected in (("active", True), ("closed", False), ("archived", False)):
        if event.get(field) is not None:
            _require(event[field] is expected, "poly_event_state_conflict")
    for field in ("finishedTimestamp", "closedTime"):
        _require(event.get(field) in (None, ""), "poly_event_finished")
    status = event.get("gameStatus")
    if status not in (None, ""):
        _require(isinstance(status, str) and status.strip().casefold() in
                 {"inprogress", "break", "penaltyshootout", "running"},
                 "poly_game_status_not_live")
    period = event.get("period")
    if isinstance(period, str):
        _require(not period.strip().upper().startswith("FT")
                 and period.strip().casefold() not in
                 {"final", "finished", "suspended", "postponed", "cancelled", "canceled"},
                 "poly_period_not_live")
    market = _one(event.get("markets"), "id", identity["poly_market_id"],
                  "poly_market_mismatch")
    _require(isinstance(market.get("sportsMarketType"), str)
             and market["sportsMarketType"].strip().lower() == "moneyline", "poly_type_mismatch")
    for field, expected in (("active", True), ("closed", False), ("acceptingOrders", True)):
        _require(market.get(field) is expected, "poly_market_not_open")
    for field, expected in (("archived", False), ("enableOrderBook", True)):
        if market.get(field) is not None:
            _require(market[field] is expected, "poly_market_state_conflict")
    _require(market.get("closedTime") in (None, ""), "poly_market_finished")
    version = market.get("version")
    _require(version in ("v1", "v2"), "poly_version_unknown")
    if "version" in catalogue:
        _require(catalogue["version"] == version, "poly_version_changed")
    asset_field = "positionIds" if version == "v2" else "clobTokenIds"
    if version == "v2":
        _require(isinstance(market.get(asset_field), list), "poly_position_ids_invalid")
    labels = _labels(market.get("outcomes"), "poly_outcomes_invalid")
    assets = _assets(market.get(asset_field), "poly_assets_invalid")
    _require(labels == _labels(catalogue.get("outcomes"), "invalid_poly_catalogue_outcomes"),
             "poly_outcomes_changed")
    _require(assets == _assets(catalogue.get("assets"), "invalid_poly_catalogue_assets"),
             "poly_assets_changed")
    index = labels.index(identity["poly_side"])
    _require(assets[index] == identity["poly_asset_id"], "poly_asset_side_mismatch")
    return _integer(market.get("secondsDelay"), "poly_delay_unknown") * 1000


def _check_sx(response, fixtures, identity, catalogue):
    markets = _success(response, "sx_market_response_invalid")
    market = _one(markets, "marketHash", identity["sx_market_hash"], "sx_market_mismatch")
    _source_id(market.get("sportXeventId"), identity["sx_event_id"], "sx_event_mismatch")
    _require(type(market.get("type")) is int and market["type"] == catalogue["type"],
             "sx_type_mismatch")
    for source_key, catalog_key in (("sportId", "sport_id"), ("leagueId", "league_id")):
        _require(type(market.get(source_key)) is int
                 and market[source_key] == catalogue[catalog_key], "sx_" + catalog_key + "_mismatch")
    game_time = _number(market.get("gameTime"), "sx_game_time_unknown", positive=True)
    _require(game_time == catalogue["start"], "sx_game_time_changed")
    _require(market.get("status") == "ACTIVE", "sx_market_not_active")
    _require(market.get("liveEnabled") is True, "sx_live_not_enabled")
    # Even outcome 0 is an official void result, never an unreported sentinel.
    _require(market.get("reportedDate") is None and market.get("outcome") is None,
             "sx_result_already_reported")
    _require((market.get("isQuarterLineMarket") is None
              or market.get("isQuarterLineMarket") is False)
             and not market.get("legs"), "sx_nonbinary_market")
    labels = [_label(market.get("outcomeOneName"), "sx_outcomes_invalid"),
              _label(market.get("outcomeTwoName"), "sx_outcomes_invalid")]
    _require(labels == _labels(catalogue.get("outcomes"), "invalid_sx_catalogue_outcomes"),
             "sx_outcomes_changed")
    _require(labels[identity["sx_target"]] == identity["sx_side"], "sx_side_mismatch")
    teams = [_label(market.get("teamOneName"), "sx_teams_invalid"),
             _label(market.get("teamTwoName"), "sx_teams_invalid")]
    _require(teams == _labels(catalogue.get("teams"), "invalid_sx_catalogue_teams"),
             "sx_teams_changed")
    states = _success(fixtures, "sx_fixture_response_invalid")
    _require(isinstance(states, dict), "sx_fixture_missing")
    fixture = states.get(identity["sx_event_id"])
    _require(isinstance(fixture, dict), "sx_fixture_missing")
    # If a provider adds a redundant ID, it must agree with the response key.
    if "sportXeventId" in fixture:
        _source_id(fixture["sportXeventId"], identity["sx_event_id"], "sx_fixture_mismatch")
    _require(type(fixture.get("status")) is int and fixture["status"] == 2,
             "sx_fixture_not_live")
    return market


def _sx_delay(response, market, now_epoch):
    metadata = _success(response, "sx_metadata_invalid")
    _require(isinstance(metadata, dict), "sx_metadata_invalid")
    delays = metadata.get("bettingDelay")
    _require(isinstance(delays, dict), "sx_delay_unknown")
    live_default = _integer(delays.get("liveMsDefault"), "sx_delay_unknown")
    pregame_default = _integer(delays.get("pregameMsDefault"), "sx_delay_unknown")
    sports, leagues = delays.get("sportIdToMs"), delays.get("leagueIdToMs")
    _require(isinstance(sports, dict) and isinstance(leagues, dict), "sx_delay_unknown")
    # SX uses the scheduled gameTime for this policy, not fixture.status.
    if now_epoch < market["gameTime"]:
        return pregame_default
    league, sport = str(market["leagueId"]), str(market["sportId"])
    if league in leagues:
        return _integer(leagues[league], "sx_delay_unknown")
    if sport in sports:
        return _integer(sports[sport], "sx_delay_unknown")
    return live_default


def _display_string(value):
    if not isinstance(value, str):
        return None
    printable = "".join(c if c.isprintable() else " " for c in value)
    text = " ".join(printable.split())[:80]
    return text or None


def fetch_live_snapshot(pair, get_json=None, clock=time.monotonic, wall=time.time):
    """Return exact, expiring live evidence or raise LiveCheckError.

    The caller must later repeat this whole check after the execution delay,
    independently refresh both books, and bind the evidence to the same pair.
    """
    started = _number(clock(), "invalid_monotonic_clock")
    identity = _catalogue(pair)
    if get_json is None:
        # Import only on use; importing this module never imports paper/monitor.
        from trafulha_arbitragem import get_json
    responses = _parallel_gets((
        ("poly", GAMMA_EVENTS + quote(identity["poly_event_id"], safe=""), None),
        ("sx_market", SX_MARKETS, {"marketHashes": identity["sx_market_hash"]}),
        ("sx_fixture", SX_FIXTURES, {"sportXEventIds": identity["sx_event_id"]}),
        ("sx_metadata", SX_METADATA, None),
    ), get_json)
    epoch = _number(wall(), "invalid_wall_clock", positive=True)
    poly_delay = _check_poly(responses["poly"], identity, pair["poly"])
    sx_market = _check_sx(responses["sx_market"], responses["sx_fixture"], identity, pair["sx"])
    sx_delay = _sx_delay(responses["sx_metadata"], sx_market, epoch)
    checked = _number(clock(), "invalid_monotonic_clock")
    deadline = started + EVIDENCE_TTL
    _require(checked >= started, "monotonic_clock_regressed")
    _require(checked < deadline, "live_evidence_expired")
    evidence = {
        "method": METHOD, **identity,
        "poly_live": True, "poly_ended": False,
        "poly_active": True, "poly_closed": False, "poly_accepting_orders": True,
        "sx_fixture_status": 2, "sx_status": "ACTIVE", "sx_live_enabled": True,
        "sx_reported": False,
        "checks_started_mono": started, "checked_mono": checked,
        "checked_epoch": epoch, "valid_until_mono": deadline,
        "sx_delay_ms": sx_delay, "poly_delay_ms": poly_delay,
        "required_delay_ms": max(sx_delay, poly_delay),
    }
    for key in ("score", "period"):
        value = _display_string(responses["poly"].get(key))
        if value is not None:
            evidence[key] = value
    return evidence
