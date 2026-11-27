"""Official-result reconciliation for existing VIRTUAL operations only.

No orders, signals, wallet keys or real payments are made by this module.
Public GET sources (verified 2026-11-27):
https://docs.polymarket.com/api-reference/markets/get-market-by-id
https://docs.polymarket.com/market-data/market-details (version selects IDs)
https://data-api.polymarket.com/v2/openapi.json (TokenRef: on-chain payout)
https://docs.sx.bet/api-reference/get-markets-find
https://docs.sx.bet/api-reference/get-fixture-status
https://docs.sx.bet/api-reference/fixture-statuses
https://github.com/sx-bet/sx-bet-api-docs/blob/main/source/includes/_markets.md

SX outcome: 0=void, 1=outcomeOneName, 2=outcomeTwoName; reportedDate is
the official report time. ACTIVE/INACTIVE alone proves no result.
Polymarket TokenRef.resolved means an on-chain payout exists; final_price
is that payout, NOT a quoted price. Only exact 0/1 payouts are automatic.
"""
from __future__ import annotations

import json
import math
import re
import time
from urllib.parse import quote

import paper_v12 as paper
import market_contracts_v16 as contracts

GAMMA = "https://gamma-api.polymarket.com/markets/"
POLY_TOKENS = "https://data-api.polymarket.com/v2/tokens"
SX_MARKETS = "https://api.sx.bet/markets/find"
SX_FIXTURES = "https://api.sx.bet/fixture/status"
MAX_PASS_SECONDS = 45.0
REQUEST_TIMEOUT = 4.0
RECHECK_SECONDS = 60
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_DECIMAL_ID = re.compile(r"[0-9]+\Z")


class PendingResult(Exception):
    """A source is missing or has not published a final result yet."""

    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.evidence = evidence


class ReviewResult(Exception):
    """Identity, format or exceptional outcome needs human verification."""

    def __init__(self, reason, evidence=None):
        super().__init__(reason)
        self.evidence = evidence


class PassDeadline(Exception):
    pass


def _record_contract(record):
    """Return the immutable opening contract, or preserve a legacy record.

    Soccer/hockey can only have been opened with a V16 certificate. A missing
    or malformed certificate therefore requires review, never legacy fallback.
    """
    raw = record.get("contract_evidence_json")
    if raw is None and record.get("sport") not in {"soccer", "hockey"}:
        return None
    if not contracts.validate_record_contract(record):
        raise ReviewResult("settlement_contract_invalid")
    return json.loads(raw)


def _changed_contract_evidence(contract, source, raw, source_url, now):
    """Retain the changed rules/identity for review, alongside the frozen proof."""
    fields = contracts.POLY_FIELDS if source == "poly" else contracts.SX_FIELDS
    return {
        "source_url": source_url, "observed_epoch": now, "resolved": False,
        "reason": source + "_settlement_contract_changed",
        "expected_source_fingerprint": contract[source]["source_fingerprint"],
        "observed_source_fields": {key: raw.get(key) for key in fields},
    }


def _finished_fixture(contract, fetch, now):
    """Require a normally finished fixture for V16's normal-result contract.

    Official payouts can also follow an abandoned or awarded match. Check the
    fixture on each pass, even before a payout exists, so an observed exception
    is preserved as REVIEW_REQUIRED and cannot later become an automatic win.
    """
    event_id = contract["sx"]["event_id"]
    evidence = {"source_url": SX_FIXTURES, "event_id": event_id,
                "observed_epoch": now, "status": None}
    response = fetch(SX_FIXTURES, {"sportXEventIds": event_id})
    if not isinstance(response, dict) or response.get("status") != "success":
        raise PendingResult("sx_fixture_response_unavailable", evidence)
    data = response.get("data")
    fixture = data.get(event_id) if isinstance(data, dict) else None
    if not isinstance(fixture, dict):
        raise PendingResult("sx_fixture_unavailable", evidence)
    status = fixture.get("status")
    evidence["status"] = status
    if "sportXeventId" in fixture:
        evidence["reported_event_id"] = fixture["sportXeventId"]
        if fixture["sportXeventId"] != event_id:
            raise ReviewResult("sx_fixture_event_mismatch", evidence)
    labels = {1: "not_started", 2: "in_progress", 3: "finished",
              4: "cancelled", 5: "postponed", 6: "interrupted",
              7: "abandoned", 8: "coverage_lost", 9: "about_to_start"}
    if type(status) is not int or status not in labels:
        raise PendingResult("sx_fixture_status_unavailable", evidence)
    evidence["status_label"] = labels[status]
    if status in (4, 5, 6, 7):
        raise ReviewResult("sx_fixture_exception_" + labels[status], evidence)
    if status != 3:
        raise PendingResult("sx_fixture_not_finished:" + labels[status], evidence)
    return evidence


def _array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise PendingResult("metadata_array_unavailable") from None
    if not isinstance(value, list):
        raise PendingResult("metadata_array_unavailable")
    return value


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ReviewResult("invalid_outcome_label")
    return value.strip()


def _source_label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise PendingResult("source_outcome_label_unavailable")
    return value.strip()


def _required(source, key):
    value = source.get(key)
    if value is None or value == "":
        raise PendingResult("source_field_unavailable:" + key)
    return value


def _decimal_id(value):
    if isinstance(value, bool):
        raise ReviewResult("invalid_decimal_identifier")
    result = str(value)
    if not _DECIMAL_ID.fullmatch(result) or len(result) > 100:
        raise ReviewResult("invalid_decimal_identifier")
    return result


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ReviewResult("invalid_market_hash")
    return value.lower()


def _unique_match(rows, key, value, *, hash_key=False):
    if not isinstance(rows, list):
        raise PendingResult("source_data_unavailable")
    def equal(row):
        if not isinstance(row, dict):
            return False
        found = str(row.get(key, ""))
        return (found.lower() if hash_key else found) == value
    matches = [row for row in rows if equal(row)]
    if not matches and (not rows or any(not isinstance(row, dict)
                                        or row.get(key) in (None, "") for row in rows)):
        raise PendingResult("source_record_not_found")
    if len(matches) != 1:
        raise ReviewResult("source_identifier_missing_or_duplicated")
    return matches[0]


def _poly_result(record, fetch, now):
    contract = _record_contract(record)
    market_id = _decimal_id(record["poly_market"])
    metadata_url = GAMMA + quote(market_id, safe="")
    metadata = fetch(metadata_url)
    if not isinstance(metadata, dict):
        raise PendingResult("poly_gamma_metadata_unavailable")
    if str(_required(metadata, "id")) != market_id:
        raise ReviewResult("poly_gamma_market_mismatch")
    if contract is not None and not contracts.verify_poly_source(contract, metadata):
        raise ReviewResult("poly_settlement_contract_changed",
                           _changed_contract_evidence(contract, "poly", metadata,
                                                      metadata_url, now))
    condition = _hash(_required(metadata, "conditionId"))
    labels = [_source_label(x) for x in _array(metadata.get("outcomes"))]
    version = metadata.get("version")
    if version not in (None, "", "v1", "v2"):
        raise PendingResult("poly_market_version_not_supported")
    # Both fields can coexist. V2 must use positionIds, as the scanner does.
    # Legacy responses without version still need a corroborating v1_ctf token.
    id_field = "positionIds" if version == "v2" else "clobTokenIds"
    raw_tokens = _array(metadata.get(id_field))
    if not labels or not raw_tokens or any(value in (None, "") for value in raw_tokens):
        raise PendingResult("poly_outcome_identifiers_unavailable")
    tokens = [_decimal_id(x) for x in raw_tokens]
    if len(labels) != 2 or len(tokens) != 2 or len(set(labels)) != 2 or len(set(tokens)) != 2:
        raise ReviewResult("poly_not_an_unambiguous_binary_market")
    selected_label = _label(record["poly_side"])
    indices = [i for i, name in enumerate(labels) if name == selected_label]
    if len(indices) != 1:
        raise ReviewResult("poly_selected_label_not_exact_unique_match")
    token_id = tokens[indices[0]]
    stored_token = record.get("poly_token_id")
    if stored_token not in (None, "") and _decimal_id(stored_token) != token_id:
        raise ReviewResult("poly_stored_token_label_mismatch")
    response = fetch(POLY_TOKENS, {"token_id": token_id})
    token = _unique_match(response.get("data") if isinstance(response, dict) else None,
                          "token_id", token_id)
    token_condition = _hash(_required(token, "condition_id"))
    structural_condition = token.get("structural_condition_id")
    # The API explicitly documents a second condition ID for carried-over V2
    # markets. Accept only an alias stated on this exact official token row.
    confirmed_conditions = {token_condition}
    if structural_condition is not None:
        confirmed_conditions.add(_hash(structural_condition))
    if condition not in confirmed_conditions:
        raise ReviewResult("poly_token_condition_mismatch")
    if _source_label(token.get("outcome")) != selected_label:
        raise ReviewResult("poly_token_outcome_mismatch")
    module = _required(token, "module")
    if module not in {"v1_ctf", "binary", "neg_risk"}:
        raise ReviewResult("poly_unsupported_token_module")
    if (version == "v2") != (module != "v1_ctf"):
        raise ReviewResult("poly_market_version_token_module_mismatch")
    evidence = {
        "source_url": POLY_TOKENS, "metadata_url": metadata_url,
        "market_id": market_id, "token_id": token_id,
        "condition_id": token_condition, "metadata_condition_id": condition,
        "structural_condition_id": structural_condition,
        "market_version": version or "legacy_v1", "identifier_field": id_field,
        "outcome_label": selected_label,
        "observed_epoch": now, "resolved": False,
        "resolution_status": "pending_onchain_payout",
    }
    if token.get("resolved") is not True:
        if token.get("resolved") is not False:
            raise PendingResult("poly_resolved_boolean_unavailable")
        return evidence, token_id
    payout = token.get("final_price")
    if payout is None:
        raise PendingResult("poly_final_payout_unavailable")
    if isinstance(payout, bool) or not isinstance(payout, (int, float)) or not math.isfinite(payout):
        raise ReviewResult("poly_invalid_final_payout")
    if payout not in (0, 1):
        raise ReviewResult("poly_fractional_void_or_split_payout",
                           {**evidence, "reported_resolved": True, "payout_rate": payout})
    evidence.update(resolved=True, resolution_status="onchain_payout",
                    payout_rate=payout, won=(payout == 1))
    return evidence, token_id


def _sx_result(record, fetch, now):
    contract = _record_contract(record)
    market_hash = _hash(record["sx_market"])
    response = fetch(SX_MARKETS, {"marketHashes": market_hash})
    if not isinstance(response, dict) or response.get("status") != "success":
        raise PendingResult("sx_response_not_success")
    market = _unique_match(response.get("data"), "marketHash", market_hash,
                           hash_key=True)
    if contract is not None and not contracts.verify_sx_source(contract, market):
        raise ReviewResult("sx_settlement_contract_changed",
                           _changed_contract_evidence(contract, "sx", market,
                                                      SX_MARKETS, now))
    labels = [_source_label(market.get("outcomeOneName")),
              _source_label(market.get("outcomeTwoName"))]
    selected_label = _label(record["sx_side"])
    indices = [i for i, name in enumerate(labels) if name == selected_label]
    if len(indices) != 1:
        raise ReviewResult("sx_selected_label_not_exact_unique_match")
    outcome_one = indices[0] == 0
    stored_side = record.get("sx_outcome_one")
    if stored_side is not None and (stored_side not in (0, 1) or bool(stored_side) != outcome_one):
        raise ReviewResult("sx_stored_side_label_mismatch")
    evidence = {
        "source_url": SX_MARKETS, "market_hash": market_hash,
        "outcome_one": outcome_one, "outcome_label": selected_label,
        "observed_epoch": now, "resolved": False,
        "resolution_status": "awaiting_official_report",
        "market_status": market.get("status"),
    }
    outcome, reported = market.get("outcome"), market.get("reportedDate")
    if outcome is None or reported is None:
        return evidence, int(outcome_one)
    if type(outcome) is not int or outcome not in (0, 1, 2):
        raise ReviewResult("sx_unsupported_reported_outcome")
    if (isinstance(reported, bool) or not isinstance(reported, (int, float))
            or not math.isfinite(reported) or reported <= 0 or reported > now + 120
            or int(reported) != reported):
        raise ReviewResult("sx_invalid_report_time")
    if outcome == 0:
        raise ReviewResult("sx_void_or_refund_requires_review",
                           {**evidence, "outcome": 0, "reported_date": int(reported)})
    if market.get("isQuarterLineMarket") is True or market.get("legs"):
        raise ReviewResult("sx_nonbinary_settlement_requires_review")
    evidence.update(resolved=True, resolution_status="reported", outcome=outcome,
                    reported_date=int(reported), won=(outcome == (1 if outcome_one else 2)))
    return evidence, int(outcome_one)


def ensure_result_schema(conn):
    """One bounded evidence row per operation; does not change old ledger rows."""
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_v13_result_checks(
          trade_id INTEGER PRIMARY KEY, checked_epoch INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, verdict TEXT NOT NULL,
          reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
          next_check_epoch INTEGER NOT NULL DEFAULT 0
        )""")


def probe_public_sources(get_json=None):
    """Diagnóstico público GET-only; não lê carteiras nem altera operações.

    Confirma contratos atuais a partir da região do serviço. O resultado não
    prova uma operação do utilizador e nunca é usado para creditar saldos.
    """
    if get_json is None:
        from trafulha_arbitragem import get_json
    deadline = time.monotonic() + 25
    now = int(time.time())
    report = {}

    def fetch(url, params=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            raise PassDeadline()
        return get_json(url, params=params, timeout=min(REQUEST_TIMEOUT, remaining))

    try:
        markets = fetch(GAMMA.rstrip("/"), {"closed": "true", "limit": 1,
                                             "order": "id", "ascending": "false"})
        if not isinstance(markets, list) or not markets:
            raise PendingResult("metadata_sample_unavailable")
        metadata = markets[0]
        labels = _array(metadata.get("outcomes"))
        record = {"poly_market": str(metadata["id"]),
                  "poly_side": _label(labels[0]), "poly_token_id": None}
        evidence, _ = _poly_result(record, fetch, now)
        report["polymarket"] = "OK"
        report["poly_sample_resolved"] = evidence["resolved"]
    except Exception as exc:
        report["polymarket"] = "PENDENTE:" + type(exc).__name__
    try:
        response = fetch("https://api.sx.bet/markets/active", {"pageSize": 1})
        markets = response.get("data", {}).get("markets", [])
        if not markets:
            raise PendingResult("metadata_sample_unavailable")
        market = markets[0]
        record = {"sx_market": market["marketHash"],
                  "sx_side": _label(market.get("outcomeOneName")),
                  "sx_outcome_one": 1}
        evidence, _ = _sx_result(record, fetch, now)
        report["sx_bet"] = "OK"
        report["sx_sample_reported"] = evidence["resolved"]
    except Exception as exc:
        report["sx_bet"] = "PENDENTE:" + type(exc).__name__
    return report


def _save_check(conn, trade_id, now, verdict, reason, evidence):
    with conn:
        conn.execute("""INSERT INTO paper_v13_result_checks
          (trade_id,checked_epoch,attempts,verdict,reason,evidence_json,next_check_epoch)
          VALUES(?,?,1,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET
          checked_epoch=excluded.checked_epoch, attempts=attempts+1,
          verdict=excluded.verdict, reason=excluded.reason,
          evidence_json=excluded.evidence_json,next_check_epoch=excluded.next_check_epoch
        """, (trade_id, now, verdict, reason[:500],
              json.dumps(evidence, ensure_ascii=False, sort_keys=True), now + RECHECK_SECONDS))


def run_settlement_pass(conn, get_json=None, now_epoch=None, limit=10):
    """Check at most 10 OPEN paper rows, with bounded GET time and no network writes.

    REVIEW_REQUIRED and legacy margins >=10% remain explicitly excluded.
    Network/availability failures leave an operation open and retryable.
    The caller should use a separate SQLite connection on a background worker.
    """
    if get_json is None:
        from trafulha_arbitragem import get_json
    now = int(time.time() if now_epoch is None else now_epoch)
    limit = max(0, min(10, int(limit)))
    counts = dict(checked=0, settled=0, pending=0, review=0, errors=0)
    ensure_result_schema(conn)
    if not limit:
        return counts
    deadline = time.monotonic() + MAX_PASS_SECONDS
    def fetch(url, params=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            raise PassDeadline()
        return get_json(url, params=params, timeout=min(REQUEST_TIMEOUT, remaining))

    cursor = conn.execute("""SELECT t.* FROM paper_v12_trades t
      LEFT JOIN paper_v13_result_checks c ON c.trade_id=t.id
      WHERE t.status='OPEN_CONDITIONAL'
        AND t.poly_debit_cents+t.sx_debit_cents > 0
        AND t.conditional_profit_cents*10 < t.poly_debit_cents+t.sx_debit_cents
        AND COALESCE(c.next_check_epoch,0) <= ?
      ORDER BY COALESCE(c.checked_epoch,0), COALESCE(c.attempts,0), t.id LIMIT ?
    """, (now, limit))
    names = [column[0] for column in cursor.description]
    records = [dict(zip(names, row)) for row in cursor.fetchall()]
    for record in records:
        if time.monotonic() >= deadline:
            break
        trade_id = record["id"]
        counts["checked"] += 1
        evidence = {"method": "official_api", "observed_epoch": now}
        verdict, reason = "pending", "awaiting_both_official_results"
        recovered = {}
        evidence_name = None
        try:
            contract = _record_contract(record)
            if contract is not None:
                evidence["contract_digest"] = contract["digest"]
                evidence_name = "fixture"
                evidence["fixture"] = _finished_fixture(contract, fetch, now)
            for name, resolver in (("poly", _poly_result), ("sx", _sx_result)):
                evidence_name = name
                try:
                    source_evidence, recovered_id = resolver(record, fetch, now)
                    evidence[name] = source_evidence
                    recovered[name] = recovered_id
                except PendingResult as exc:
                    evidence[name] = exc.evidence or {"resolved": False, "reason": str(exc)}
            # Legacy identifiers are restored only from exact, unique metadata.
            with conn:
                if "poly" in recovered and record.get("poly_token_id") in (None, ""):
                    conn.execute("UPDATE paper_v12_trades SET poly_token_id=? "
                                 "WHERE id=? AND status='OPEN_CONDITIONAL' "
                                 "AND (poly_token_id IS NULL OR poly_token_id='')",
                                 (recovered["poly"], trade_id))
                if "sx" in recovered and record.get("sx_outcome_one") is None:
                    conn.execute("UPDATE paper_v12_trades SET sx_outcome_one=? "
                                 "WHERE id=? AND status='OPEN_CONDITIONAL' AND sx_outcome_one IS NULL",
                                 (recovered["sx"], trade_id))
            if all(evidence.get(name, {}).get("resolved") is True for name in ("poly", "sx")):
                if (contract is not None
                        and evidence["poly"]["won"] == evidence["sx"]["won"]):
                    raise ReviewResult("contract_results_not_complementary")
                _save_check(conn, trade_id, now, "verified", "both_official_results", evidence)
                value = paper.settle_verified_outcomes(
                    conn, trade_id, evidence["poly"]["won"], evidence["sx"]["won"],
                    evidence, now_epoch=now,
                )
                with conn:
                    conn.execute("UPDATE paper_v13_result_checks SET verdict='settled' "
                                 "WHERE trade_id=?", (trade_id,))
                if value is not None:
                    counts["settled"] += 1
                continue
        except ReviewResult as exc:
            verdict, reason = "review", str(exc)
            if exc.evidence is not None and evidence_name is not None:
                evidence[evidence_name] = exc.evidence
            with conn:
                conn.execute("UPDATE paper_v12_trades SET status='REVIEW_REQUIRED',note=? "
                             "WHERE id=? AND status='OPEN_CONDITIONAL'",
                             ("Resultados oficiais: " + reason, trade_id))
            counts["review"] += 1
        except PendingResult as exc:
            reason = str(exc)
            if exc.evidence is not None and evidence_name is not None:
                evidence[evidence_name] = exc.evidence
        except PassDeadline:
            counts["pending"] += 1
            _save_check(conn, trade_id, now, "pending", "pass_time_budget", evidence)
            break
        except Exception as exc:
            # Never turn a timeout, HTTP error or transient schema failure into a win.
            verdict, reason = "pending", "source_or_ledger_unavailable:" + type(exc).__name__
            counts["errors"] += 1
        if verdict == "pending":
            counts["pending"] += 1
        _save_check(conn, trade_id, now, verdict, reason, evidence)
    return counts
