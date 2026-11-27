"""Bounded, sampled audit of Paper decisions; never changes a wallet.

The caller supplies already computed facts. This module performs no network
requests, never stores an entire market/request object, and does not infer why
a trade failed. All writes use their own short transaction on the monitor DB.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TABLE = "decision_audit_v15"
RETENTION_HOURS = 12
MAX_ROWS = 10_000
MAX_DETAILS_BYTES = 16_384  # comfortably below the 50 KiB evidence ceiling
MAX_LEVELS = 8
MAX_BUSY_MS = 100
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{0,95}\Z")
_LISBON = ZoneInfo("Europe/Lisbon")

# These are *observed reasons*, not guesses derived from an indicative ROI.
REASON_LABELS = {
    "cambio_invalido": "Câmbio indisponível ou inválido",
    "sem_liquidez": "Sem liquidez numa das plataformas",
    "reserva_banca_insuficiente": "Reserva ou exposição não permite o montante mínimo",
    "margem_liquida_insuficiente": "Margem líquida inferior ao mínimo configurado",
    "liquidez_montante_ou_custos_inelegiveis": "Sem montante elegível com a liquidez e os custos atuais",
    "recheck_api_error": "Falha da API na segunda leitura",
    "recheck_no_liquidity": "Liquidez desapareceu na segunda leitura",
    "recheck_margin_changed": "Margem mudou demasiado durante a confirmação",
    "recheck_rate_limit_backoff": "Confirmação adiada pelo limite de pedidos da API",
    "rate_limit_backoff": "A aguardar o recuo exigido pela API",
    "validacao_final_sem_registo": "Validação final de saldo, identidade ou prazo recusou o registo",
    "book_api_error": "Falha ao consultar os preços",
    "sx_fees_pending": "A aguardar as taxas oficiais da conta SX",
    "sx_fees_unavailable": "Taxas oficiais da conta SX indisponíveis",
    "sx_fees_expired": "Confirmação das taxas SX expirada",
    "poly_fees_pending": "A aguardar as taxas oficiais do mercado Polymarket",
    "poly_fees_unavailable": "Taxas oficiais Polymarket indisponíveis",
    "poly_fees_expired": "Confirmação das taxas Polymarket expirada",
    "poly_fee_curve_unsupported": "Curva de comissão Polymarket ainda não suportada",
    "poly_fee_schedule_missing": "Mercado Polymarket sem configuração de taxas confirmada",
    "poly_fees_enabled_missing": "Mercado Polymarket não confirmou se cobra taxas",
    "fees_market_not_watched": "Mercado ainda não incluído na confirmação de custos",
    "fee_context_invalid": "Confirmação dos custos inválida",
    "registada": "Operação virtual registada",
    "booked": "Operação virtual registada",
    "abaixo_margem": "Margem inferior ao mínimo configurado",
    "margem_anomala": "Margem dimensionada fora do intervalo automático",
    "validacao_sem_registo": "Validação sem registo; ver a etapa indicada",
    "sem_cotacao_valida": "Sem cotação válida nas duas plataformas",
    "ja_registada": "Já existe uma operação neste par de mercados",
    "fora_da_janela": "Jogo fora da janela de análise",
    "inicio_durante_validacao": "O jogo começou durante a validação",
    "costs_unknown": "Custos ainda não confirmados",
    "costs_expired": "Confirmação dos custos expirada",
    "fees_unknown": "Taxas ainda não confirmadas",
    "fees_expired": "Confirmação das taxas expirada",
    "cost_context_expired": "Confirmação dos custos expirada",
    "cost_context_unavailable": "Custos indisponíveis para validação",
    "initial_quote_stale": "Primeira leitura de preços demasiado antiga",
    "initial_snapshots_skewed": "Primeiras leituras de preços desfasadas",
    "initial_quote_ineligible": "Primeira cotação sem montante elegível",
    "initial_margin_low": "Margem inicial inferior ao mínimo",
    "insufficient_balance": "Saldo disponível insuficiente",
    "insufficient_liquidity": "Liquidez insuficiente para o montante mínimo",
    "reserve_limit": "Reserva de uma carteira atingida",
    "exposure_limit": "Limite de exposição da banca atingido",
    "recheck_price_changed": "Preço mudou durante a confirmação",
    "recheck_margin_lost": "Margem desapareceu na segunda leitura",
    "recheck_roi_unstable": "Margem instável entre as duas leituras",
    "recheck_margin_anomalous": "Margem final fora do intervalo automático",
    "recheck_quote_stale": "Segunda leitura de preços demasiado antiga",
    "recheck_snapshots_skewed": "Segundas leituras de preços desfasadas",
    "recheck_empty_book": "Sem liquidez na segunda leitura",
    "recheck_source_error": "Falha de uma fonte na segunda leitura",
    "book_source_error": "Falha ao consultar os preços",
    "ledger_rejected": "Registo recusado na validação final da carteira",
    "live_desativado_ou_sem_coordenador": "Validação de direto indisponível",
    "live_preco_margem_saldo_inelegivel": "Direto sem cotação elegível; ver a etapa indicada",
    "live_em_verificacao": "A confirmar o direto nas duas fontes",
    "live_aguarda_atraso": "A aguardar o atraso obrigatório da plataforma",
    "live_confirmado": "Direto confirmado; aguarda a validação de preços",
    "live_fila_cheia": "Fila de validação de diretos cheia",
    "live_parado": "Coordenador de direto parado",
    "live_par_invalido": "Identidade do par de mercados inválida",
    "live_par_fora_catalogo": "Par deixou de pertencer ao catálogo",
    "live_par_desatualizado": "Identidade do mercado mudou no catálogo",
    "live_candidato_expirado": "Candidato em direto expirou",
    "live_prova_expirada": "Confirmação oficial de direto expirou",
    "live_evidence_expired": "Confirmação oficial de direto expirou",
    "live_prova_invalida": "Confirmação oficial de direto inválida",
    "live_sources_busy": "Fontes de confirmação ocupadas",
    "live_sources_timeout": "Tempo limite nas fontes de confirmação",
    "live_sources_unavailable": "Fontes de confirmação indisponíveis",
    "poly_not_live": "Polymarket ainda não confirma o direto",
    "poly_market_not_open": "Mercado Polymarket não está aberto",
    "poly_ended_or_unknown": "Fim do jogo ou estado Polymarket por confirmar",
    "poly_game_status_not_live": "Estado oficial Polymarket não é direto",
    "poly_period_not_live": "Período Polymarket incompatível com direto",
    "poly_delay_unknown": "Atraso Polymarket não confirmado",
    "poly_source_unavailable": "Fonte Polymarket indisponível",
    "sx_fixture_not_live": "SX ainda não confirma o jogo em direto",
    "sx_market_not_active": "Mercado SX não está ativo",
    "sx_live_not_enabled": "Mercado SX não permite direto",
    "sx_delay_unknown": "Atraso SX não confirmado",
    "sx_fixture_missing": "Estado oficial do jogo SX indisponível",
    "sx_result_already_reported": "SX já publicou o resultado",
    "sx_market_source_unavailable": "Mercado SX indisponível para confirmação",
    "sx_fixture_source_unavailable": "Estado do jogo SX indisponível",
    "sx_metadata_source_unavailable": "Metadados SX indisponíveis",
    "diagnostico_sem_motivo": "Motivo individual não disponível",
}

_PENDING_REASONS = {
    "live_em_verificacao", "live_aguarda_atraso", "live_confirmado",
}
_NUMERIC_FIELDS = frozenset({
    "first_roi_pct", "final_roi_pct", "sized_roi_pct", "roi_pct",
    "initial_roi_pct", "second_roi_pct", "stake_eur", "total_eur",
    "poly_stake_eur", "sx_stake_eur", "net_profit_eur", "profit_eur",
    "poly_fee_eur", "sx_fee_eur", "poly_fee_cents", "sx_fee_cents",
    "poly_debit_cents", "sx_debit_cents", "total_cents",
    "conditional_profit_cents", "worst_profit_cents", "payout_usdc",
    "poly_payout_cents", "sx_payout_cents", "poly_win_credit_cents",
    "sx_win_credit_cents", "payout_eur_cents", "fx_eur_per_usd",
    "quote_age_ms", "snapshot_gap_ms", "quote_age_seconds",
    "snapshot_gap_seconds", "skew_seconds", "evidence_age_seconds",
    "best_poly_price", "best_sx_price", "final_poly_price", "final_sx_price",
    "first_poly_price", "first_sx_price", "second_poly_price", "second_sx_price",
    "poly_price", "sx_price", "elapsed_ms", "queue_ms", "rest_ms",
    "required_delay_ms", "poly_delay_ms", "sx_delay_ms", "delay_ms",
    "poly_capacity", "sx_capacity", "liquidity_usdc", "capacity_usdc",
    "poly_max_price", "sx_max_price", "available_poly_cents",
    "available_sx_cents", "capital_cents", "requested_total_cents",
    "observed_epoch", "reset_epoch", "game_start_epoch", "fetched_epoch",
    "expires_epoch", "rate", "exponent", "poly_fee_rate", "sx_taker_rate",
    "taker_payout_fee", "fee_schedule_exponent", "seconds_delay",
})
_CODE_FIELDS = frozenset({
    "failure_stage", "provider_reason", "exception_type", "phase", "step",
    "reason", "cost_status", "fee_status", "cost_reason", "validation_reason",
    "fee_source", "source", "reset_request_id", "bankroll_cycle", "cycle_id",
})
_BOOL_FIELDS = frozenset({"verified", "fees_enabled", "taker_only", "stale"})
_BOOK_FIELDS = frozenset({
    "poly_levels", "sx_levels", "first_poly_levels", "first_sx_levels",
    "final_poly_levels", "final_sx_levels",
})
_OBJECT_FIELDS = frozenset({
    "quote", "first_quote", "second_quote", "costs", "fee_evidence", "bankroll",
})


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value if abs(value) <= 1e18 else None


def _epoch(value):
    result = _number(time.time() if value is None else value)
    if result is None or not 0 <= result <= 253402300799:
        raise ValueError("Instante de auditoria inválido")
    return float(result)


def _text(value, limit=160):
    if type(value) not in (str, int):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())[:limit]


def _code(value, fallback="diagnostico_sem_motivo"):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def reason_label(reason):
    """Translate known observed reasons; never invent an unknown explanation."""
    value = _code(reason)
    return REASON_LABELS.get(value, "Motivo registado: " + value)


def sanitize_details(details, _depth=0):
    """Copy only bounded, explicitly supported facts, never arbitrary strings."""
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError("Detalhes de auditoria devem ser um objeto")
    result = {}
    for key in sorted(_NUMERIC_FIELDS):
        if key in details:
            result[key] = _number(details[key])
    for key in sorted(_CODE_FIELDS):
        if key in details and _code(details[key], None) is not None:
            result[key] = details[key]
    for key in sorted(_BOOL_FIELDS):
        if type(details.get(key)) is bool:
            result[key] = details[key]
    for key in sorted(_BOOK_FIELDS):
        levels = details.get(key)
        if not isinstance(levels, (tuple, list)):
            continue
        safe = []
        for level in levels[:MAX_LEVELS]:
            if not isinstance(level, (tuple, list)) or len(level) != 2:
                continue
            price, capacity = (_number(value) for value in level)
            if price is not None and capacity is not None and 0 < price < 1 and capacity >= 0:
                safe.append([price, capacity])
        result[key] = safe
    if _depth < 2:
        for key in sorted(_OBJECT_FIELDS):
            if isinstance(details.get(key), dict):
                result[key] = sanitize_details(details[key], _depth + 1)
    return result


@contextmanager
def _write(conn):
    # Using ``with conn`` alone would commit an unrelated caller transaction.
    if conn.in_transaction:
        raise ValueError("Auditoria exige uma conexão sem transação em curso")
    previous_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute(f"PRAGMA busy_timeout={min(MAX_BUSY_MS, previous_timeout)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")


def create_schema(conn):
    """Add only the audit table/indexes; retain all existing data and balances."""
    with _write(conn):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_epoch REAL NOT NULL,
                observed_at TEXT NOT NULL,
                pair_key TEXT NOT NULL,
                poly_market TEXT NOT NULL,
                sx_market TEXT NOT NULL,
                sport TEXT NOT NULL,
                event TEXT NOT NULL,
                game_start_epoch REAL,
                poly_outcome TEXT NOT NULL,
                sx_outcome TEXT NOT NULL,
                stage TEXT NOT NULL,
                indicative_roi_pct REAL,
                reason TEXT NOT NULL,
                importance INTEGER NOT NULL CHECK(importance IN (0,1,2)),
                trade_id INTEGER,
                details_json TEXT NOT NULL
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS audit_v15_time ON {TABLE}(observed_epoch,id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS audit_v15_pair ON {TABLE}(pair_key,id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS audit_v15_reason ON {TABLE}(pair_key,reason,stage,observed_epoch)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS audit_v15_priority ON {TABLE}(importance,observed_epoch,id)")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS audit_v15_trade ON {TABLE}(trade_id) WHERE trade_id IS NOT NULL")


def _prune(conn, now, retention_hours, max_rows):
    removed = conn.execute(
        f"DELETE FROM {TABLE} WHERE observed_epoch < ?", (now - retention_hours * 3600,),
    ).rowcount
    excess = conn.execute(f"SELECT MAX(COUNT(*)-?,0) FROM {TABLE}", (max_rows,)).fetchone()[0]
    if excess:
        # Keep positive evidence ahead of routine negatives, and bookings last.
        removed += conn.execute(
            f"DELETE FROM {TABLE} WHERE id IN (SELECT id FROM {TABLE} "
            "ORDER BY importance,observed_epoch,id LIMIT ?)", (excess,),
        ).rowcount
    return removed


def prune_decisions(conn, now_epoch=None, retention_hours=RETENTION_HOURS, max_rows=MAX_ROWS):
    """Prune bounded samples; SQLite reuses freed pages, without a VACUUM."""
    now = _epoch(now_epoch)
    hours = _number(retention_hours)
    if hours is None or not 0 < hours <= RETENTION_HOURS:
        raise ValueError("Retenção de auditoria deve ser positiva e no máximo 12h")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError("Limite de auditoria deve estar entre 1 e 10000 amostras")
    with _write(conn):
        return _prune(conn, now, hours, max_rows)


def record_decision(conn, pair, stage, indicative_roi_pct, reason,
                    details=None, trade_id=None, now_epoch=None):
    """Persist one sampled decision. Return False for a throttled/replayed row.

    Negative decisions and pending LIVE phases are sampled at most once per
    minute for each pair/reason/stage. Positive decisions have a 15-second
    interval, but a change of reason or stage is recorded immediately. Booked
    trade IDs bypass sampling and are idempotent across process restarts.
    """
    now = _epoch(now_epoch)
    if not isinstance(pair, dict):
        raise ValueError("Par de mercados inválido para auditoria")
    key = _text(pair.get("key"), 320)
    if not key:
        raise ValueError("Identificador do par obrigatório para auditoria")
    if trade_id is not None and (type(trade_id) is not int or trade_id <= 0):
        raise ValueError("Identificador de operação virtual inválido")
    safe = sanitize_details(details)
    encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DETAILS_BYTES:
        # Preserve scalar decision facts; nested evidence is optional.
        safe = {k: v for k, v in safe.items() if k not in _BOOK_FIELDS | _OBJECT_FIELDS}
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DETAILS_BYTES:
        raise ValueError("Detalhes de auditoria excessivamente grandes")
    stage = _code(stage, "DESCONHECIDO")
    reason = _code(reason)
    roi = _number(indicative_roi_pct)
    positive = roi is not None and roi > 0
    importance = 2 if trade_id is not None else 1 if positive else 0
    pending = reason in _PENDING_REASONS
    interval = 15 if positive and not pending else 60
    poly = pair.get("poly") if isinstance(pair.get("poly"), dict) else {}
    sx = pair.get("sx") if isinstance(pair.get("sx"), dict) else {}
    teams = sx.get("teams")
    event = " vs ".join(_text(team, 120) for team in teams[:2]) if isinstance(teams, (tuple, list)) else ""
    event = event or _text(poly.get("title"), 240) or "Jogo não identificado"
    start = _number(sx.get("start"))
    with _write(conn):
        if trade_id is not None:
            if conn.execute(f"SELECT 1 FROM {TABLE} WHERE trade_id=?", (trade_id,)).fetchone():
                return False
        else:
            latest = conn.execute(
                f"SELECT observed_epoch,reason,stage,importance FROM {TABLE} "
                "WHERE pair_key=? ORDER BY id DESC LIMIT 1", (key,),
            ).fetchone()
            changed = latest is not None and (latest[1] != reason or latest[2] != stage or latest[3] != importance)
            if not (positive and not pending and changed):
                last_same = conn.execute(
                    f"SELECT observed_epoch,importance FROM {TABLE} "
                    "WHERE pair_key=? AND reason=? AND stage=? ORDER BY observed_epoch DESC LIMIT 1",
                    (key, reason, stage),
                ).fetchone()
                if last_same and last_same[1] == importance and now - last_same[0] < interval:
                    return False
        conn.execute(f"""
            INSERT INTO {TABLE} (
                observed_epoch,observed_at,pair_key,poly_market,sx_market,sport,
                event,game_start_epoch,poly_outcome,sx_outcome,stage,
                indicative_roi_pct,reason,importance,trade_id,details_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now, datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            key, _text(poly.get("id"), 160), _text(sx.get("hash"), 160),
            _text(sx.get("sport"), 60), event, start,
            _text(pair.get("poly_label"), 120), _text(pair.get("sx_label"), 120),
            stage, roi, reason, importance, trade_id, encoded,
        ))
        _prune(conn, now, RETENTION_HOURS, MAX_ROWS)
    return True


def _row(cursor):
    result = cursor.fetchone()
    if result is None:
        return None
    data = dict(zip((column[0] for column in cursor.description), result))
    data["details"] = json.loads(data.pop("details_json"))
    data["reason_label"] = reason_label(data["reason"])
    return data


def summary_decisions(conn, since_epoch, reset_epoch=None, now_epoch=None):
    """Summarize stored samples, never present them as every scan or all bets."""
    since, now = _epoch(since_epoch), _epoch(now_epoch)
    if since > now:
        raise ValueError("Janela de auditoria inválida")
    reset = None if reset_epoch is None else _epoch(reset_epoch)
    params = (since, now)
    scope = "observed_epoch >= ? AND observed_epoch <= ?"
    counts = conn.execute(f"""
        SELECT COUNT(*),
               COALESCE(SUM(indicative_roi_pct > 0),0),
               COALESCE(SUM(indicative_roi_pct >= 10),0),
               COUNT(trade_id),
               COUNT(DISTINCT CASE WHEN indicative_roi_pct > 0 THEN poly_market || '|' || sx_market END),
               MIN(observed_epoch),MAX(observed_epoch)
        FROM {TABLE} WHERE {scope}
    """, params).fetchone()
    reasons = dict(conn.execute(
        f"SELECT reason,COUNT(*) FROM {TABLE} WHERE {scope} GROUP BY reason ORDER BY COUNT(*) DESC,reason", params,
    ).fetchall())
    last_positive = _row(conn.execute(
        f"SELECT * FROM {TABLE} WHERE {scope} AND indicative_roi_pct > 0 "
        "ORDER BY observed_epoch DESC,id DESC LIMIT 1", params,
    ))
    before = None
    after = None
    if reset is not None:
        before = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE {scope} AND observed_epoch < ?", (*params, reset),
        ).fetchone()[0]
        after = counts[0] - before
        if last_positive is not None:
            last_positive["before_reset"] = last_positive["observed_epoch"] < reset
    return {
        "since_epoch": since, "until_epoch": now, "reset_epoch": reset,
        "samples": counts[0], "positive_samples": counts[1],
        "anomalous_samples": counts[2], "booked_samples": counts[3],
        "positive_market_pairs": counts[4], "first_sample_epoch": counts[5],
        "last_sample_epoch": counts[6], "reasons": reasons,
        "before_reset_samples": before, "after_reset_samples": after,
        "last_positive": last_positive,
    }


def _pt_number(value, decimals=2, signed=False):
    text = format(value, ("+" if signed else "") + f".{decimals}f")
    return text.replace(".", ",")


def format_audit_summary(stats):
    """Compact Telegram appendix with explicit sampling and historical timing."""
    if not stats.get("samples"):
        return "🔎 Diagnóstico de entradas: ainda sem amostras nesta janela."
    lines = [
        "🔎 Diagnóstico de entradas · amostras guardadas",
        f"{stats['samples']} amostras · {stats['positive_samples']} com margem indicativa positiva"
        f" · {stats['booked_samples']} registos virtuais auditados",
    ]
    reasons = list(stats.get("reasons", {}).items())[:3]
    if reasons:
        lines.append("Motivos mais frequentes nas amostras:")
        lines.extend(f"• {reason_label(reason)}: {count}" for reason, count in reasons)
    last = stats.get("last_positive")
    if last:
        when = datetime.fromtimestamp(last["observed_epoch"], _LISBON).strftime("%d/%m %H:%M:%S")
        historic = " · antes do reset" if last.get("before_reset") else ""
        lines.append(f"Último sinal positivo: {when} Lisboa{historic}")
        lines.append(_text(last["event"], 160))
        detail = last.get("details", {})
        numeric = "Indicativa " + _pt_number(last["indicative_roi_pct"], signed=True) + "%"
        final_roi = _number(detail.get("final_roi_pct", detail.get("sized_roi_pct")))
        if final_roi is not None:
            numeric += " · dimensionada " + _pt_number(final_roi, signed=True) + "%"
        stake = _number(detail.get("stake_eur", detail.get("total_eur")))
        if stake is not None:
            numeric += " · " + _pt_number(stake) + " €"
        lines.append(numeric)
        concrete = detail.get("reason") or detail.get("failure_stage")
        if concrete and concrete in REASON_LABELS and concrete != last["reason"]:
            lines.append("Decisão: " + reason_label(concrete))
        else:
            lines.append("Decisão: " + reason_label(last["reason"]))
    if stats.get("before_reset_samples"):
        lines.append(f"Janela: {stats['before_reset_samples']} amostras antes do reset"
                     f" · {stats['after_reset_samples']} depois.")
    return "\n".join(lines)
