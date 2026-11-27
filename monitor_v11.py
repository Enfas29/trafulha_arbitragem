"""Trafulha V13.2 — análise reativa e contabilidade exclusivamente virtual.

Nunca executa apostas reais. As operações Paper são hipóteses contabilísticas;
os resultados dependem de confirmação oficial em ambas as plataformas.
Início: python monitor_v11.py
Teste local: python monitor_v11.py --self-test
Teste das APIs (uma passagem): python monitor_v11.py --once
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import shutil
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import trafulha_arbitragem as bot
import paper_v12 as paper
import sx_ws_v12 as sx_hints
import poly_ws_v12 as poly_hints
import ws_priority_v12 as ws_priority
from paper_service_v13 import PaperLifecycleWorker
from reactive_scheduler_v13 import ReactiveScheduler
from latency_metrics_v13 import LatencyMetrics
from monitor_workers_v13 import SnapshotRefresher, AsyncNoticeSender

LOG = logging.getLogger("monitor_v11")
APP_VERSION = "13.2.0"
CATALOG_GAMES = None
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

DB = os.getenv("MONITOR_DB", "monitor_v11.sqlite3")
# Evitar crescimento de SQLite: histórico recente basta para o resumo de 6h.
OBSERVATION_RETENTION_HOURS = max(6, min(48, int(
    os.getenv("MONITOR_OBSERVATION_RETENTION_HOURS", "12")
)))
OBSERVATION_MAX_ROWS = max(5000, min(100000, int(
    os.getenv("MONITOR_OBSERVATION_MAX_ROWS", "25000")
)))
OBSERVATION_SAMPLE_SECONDS = max(10, min(600, int(
    os.getenv("MONITOR_OBSERVATION_SAMPLE_SECONDS", "60")
)))
STORAGE_FREE_FLOOR = 32 * 1024 * 1024
INTERVAL = max(10, int(os.getenv("MONITOR_INTERVAL_SECONDS", "20")))
CATALOG_INTERVAL = max(180, int(os.getenv("MONITOR_CATALOG_SECONDS", "600")))
MAX_PAIRS = min(60, max(1, int(os.getenv("MONITOR_PAIRS_PER_PASS", "15"))))
# Reação a eventos WS: intervalo conservador de 1s; medir carga e 429.
# NÃO é promessa de execução <10s, nem habilita apostas reais/virtuais LIVE.
WS_REACT_GAP_SECONDS = min(10.0, max(
    1.0, float(os.getenv("MONITOR_WS_REACT_GAP_SECONDS", "1.0"))
))
# Quantos eventos WS entram à frente dos restantes mercados em cada scan.
WS_PRIORITY_MAX = min(6, max(1, int(os.getenv("MONITOR_WS_PRIORITY_MAX", "4"))))
# Cache temporária SOMENTE de identificadores/timestamps, nunca de preços.
WS_EVENT_TTL = min(60.0, max(5.0,
    float(os.getenv("MONITOR_WS_EVENT_TTL_SECONDS", "20"))
))
WS_KEY_INTERVAL = max(0.25, min(5.0, float(
    os.getenv("MONITOR_WS_KEY_INTERVAL_SECONDS", "0.25")
)))
# Limite global de arranques de pares; cada par lê uma vez cada plataforma.
PAIR_START_GAP = 1.0 / min(12.0, max(1.0, float(
    os.getenv("MONITOR_MAX_PAIRS_PER_SECOND", "10")
)))
METRICS_INTERVAL = 30.0
MAX_QUOTE_AGE_SECONDS = 2.0
# Pré-análise rápida (REST de ambos os lados), apenas diagnóstico.
# Não abre operações nem lança alertas de lucro.
FAST_PREVIEW_ENABLED = os.getenv("MONITOR_FAST_PREVIEW_ENABLED", "1").lower() in {"1", "true", "yes"}
FAST_PREVIEW_LIMIT = 1  # máximo um par por ciclo para não sobrecarregar APIs
FAST_PREVIEW_TARGET_MS = 1000
ALERT_COOLDOWN = max(300, int(os.getenv("MONITOR_ALERT_COOLDOWN_SECONDS", "1800")))
SUMMARY_INTERVAL = max(3600, int(os.getenv("MONITOR_SUMMARY_SECONDS", "21600")))
OUTAGE_THRESHOLD = max(900, int(os.getenv("MONITOR_OUTAGE_SECONDS", "1800")))
OUTAGE_REPEAT = max(1800, int(os.getenv("MONITOR_OUTAGE_REPEAT_SECONDS", "3600")))
MAX_SKEW = 6.0
MIN_ROI = paper.MIN_NET_ROI    # Usar mesmo ROI do Paper V12, nunca o limiar legado V11
PAPER_V12_ENABLED = os.getenv("PAPER_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
SX_WS_V12_ENABLED = os.getenv("SX_WS_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
POLY_WS_V12_ENABLED = os.getenv("POLY_WS_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
PAPER_RESULTS_ENABLED = os.getenv("PAPER_RESULTS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}

# Continua a usar os filtros conservadores de winner/moneyline do V11.
bot.MAX_POLY_SERIES = min(120, max(10, int(os.getenv("MONITOR_POLY_SERIES", "60"))))
bot.MAX_SX_PAGES = min(30, max(1, int(os.getenv("MONITOR_SX_PAGES", "12"))))
bot.MAX_EVENTS_PER_SERIES = 150


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_database(path: str):
    parent = Path(path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY,
      observed_at TEXT NOT NULL,
      pair_key TEXT NOT NULL,
      sport TEXT NOT NULL,
      event TEXT NOT NULL,
      status TEXT NOT NULL,
      poly_market TEXT NOT NULL,
      sx_market TEXT NOT NULL,
      poly_outcome TEXT NOT NULL,
      sx_outcome TEXT NOT NULL,
      best_poly_price REAL,
      best_sx_price REAL,
      common_payout_usd REAL,
      gross_roi_pct REAL,
      buffered_roi_pct REAL,
      snapshot_gap_seconds REAL,
      hypothetical_total_eur REAL,
      hypothetical_profit_eur REAL,
      note TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS observations_time
      ON observations(observed_at);
    CREATE INDEX IF NOT EXISTS observations_pair_time
      ON observations(pair_key, observed_at);
    CREATE TABLE IF NOT EXISTS alerts (
      pair_key TEXT PRIMARY KEY,
      last_seen_epoch INTEGER NOT NULL,
      roi_pct REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS report_schedule (
      report_key TEXT PRIMARY KEY,
      last_report_epoch INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS monitor_health (
      key TEXT PRIMARY KEY, epoch INTEGER NOT NULL
    );
    """)
    conn.commit()
    return conn


def normalize_quote(poly_levels, sx_levels):
    """ROI demonstrativa com payout igual e profundidade real até 10 USDC.

    Não transforma diferenças de liquidação em arbitragem garantida.
    """
    if not (poly_levels and sx_levels):
        return None
    capacity = min(
        10.0,
        sum(size for _, size in poly_levels),
        sum(size for _, size in sx_levels),
    )
    if capacity < 1.0:
        return None
    pc = bot.cost_for_payout(poly_levels, capacity)
    sc = bot.cost_for_payout(sx_levels, capacity)
    if pc is None or sc is None or pc + sc <= 0:
        return None
    gross = 100.0 * (capacity - pc - sc) / (pc + sc)
    # Margem de referência do V12: taxa taker desportiva Poly e
    # buffers adversos separados para cada perna.
    model = paper.quote_for_payout(
        poly_levels, sx_levels, capacity, bot.FX if bot.FX > 0 else 1.0
    )
    if model is None:
        return None
    buffered = 100.0 * model.roi
    return capacity, gross, buffered


def hypothetical_100_eur(poly_levels, sx_levels, fx: float):
    """Tamanho máximo de 100 € total usando o modelo prudente existente.

    Uma cotação positiva NÃO constitui autorização para abrir posições.
    """
    if fx <= 0:
        return None
    bot.FX = fx
    return paper.select_shadow_quote(
        poly_levels, sx_levels, fx,
        paper.INITIAL_CENTS, paper.INITIAL_CENTS,
    )


def connection_free_bytes(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return shutil.disk_usage(Path(path).resolve().parent if path else Path.cwd()).free


def prune_observations(conn, now=None):
    """Limpeza limitada de histórico; SQLite reutiliza páginas libertadas."""
    cutoff = datetime.fromtimestamp(
        (time.time() if now is None else now) - OBSERVATION_RETENTION_HOURS * 3600,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")
    boundary = conn.execute("SELECT id FROM observations ORDER BY id DESC LIMIT 1 OFFSET ?", (OBSERVATION_MAX_ROWS,)).fetchone()
    removed = 0
    for _ in range(5):
        with conn:
            count = conn.execute("DELETE FROM observations WHERE id IN (SELECT id FROM observations WHERE observed_at < ? OR id <= ? ORDER BY id LIMIT 2000)", (cutoff, boundary[0] if boundary else -1)).rowcount
        removed += count
        if count < 2000:
            break
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return removed


def insert_observation(conn, pair, stage, poly_levels, sx_levels, skew, fx):
    quote = normalize_quote(poly_levels, sx_levels) if skew <= MAX_SKEW else None
    hypothetical = hypothetical_100_eur(poly_levels, sx_levels, fx) if quote else None
    p_best = poly_levels[0][0] if poly_levels else None
    s_best = sx_levels[0][0] if sx_levels else None
    event = " vs ".join(pair["sx"]["teams"])
    note = ("Livros lidos; equivalência de liquidação não validada."
            if quote else "Sem preço ou snapshots demasiado afastados.")

    # Guardar no máximo uma leitura habitual/minuto por direção.
    # Os cálculos e sinais continuam a ser feitos a cada passagem, com
    # REST fresco; só reduzimos o volume das amostras históricas.
    sample_seconds = (15 if quote and quote[2] >= MIN_ROI * 100
                      else OBSERVATION_SAMPLE_SECONDS)
    cutoff = datetime.fromtimestamp(
        time.time() - sample_seconds, tz=timezone.utc
    ).isoformat(timespec="seconds")
    last = conn.execute(
        "SELECT observed_at FROM observations WHERE pair_key=? "
        "ORDER BY observed_at DESC LIMIT 1", (pair["key"],)
    ).fetchone()
    if last and last[0] >= cutoff:
        return quote, hypothetical
    if connection_free_bytes(conn) < STORAGE_FREE_FLOOR:
        LOG.warning("STORAGE | disco com menos de 32MiB livres: observação ignorada")
        return quote, hypothetical
    with conn:
        conn.execute("""
          INSERT INTO observations (
            observed_at, pair_key, sport, event, status, poly_market, sx_market,
            poly_outcome, sx_outcome, best_poly_price, best_sx_price,
            common_payout_usd, gross_roi_pct, buffered_roi_pct,
            snapshot_gap_seconds, hypothetical_total_eur,
            hypothetical_profit_eur, note
          ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            utc_now(), pair["key"], pair["sx"]["sport"], event, stage,
            pair["poly"]["id"], pair["sx"]["hash"], pair["poly_label"],
            pair["sx_label"], p_best, s_best,
            quote[0] if quote else None,
            quote[1] if quote else None,
            quote[2] if quote else None,
            skew,
            hypothetical.total_eur if hypothetical else None,
            hypothetical.profit_eur if hypothetical else None,
            note,
        ))
    return quote, hypothetical


def telegram(text: str):
    # Esta chamada não executa qualquer ação nas exchanges.
    if not (bot.TG_TOKEN and bot.TG_CHAT):
        LOG.info("Telegram não configurado; mensagem: %s", text.replace("\n", " | "))
        return
    bot.telegram(text)


def maybe_alert(conn, pair, quote, hypothetical, now, sender=None):
    if (not hypothetical or not quote
            or quote[2] < MIN_ROI * 100
            or quote[2] >= 100 * paper.MAX_AUTO_PAPER_ROI):
        return False
    row = conn.execute(
        "SELECT last_seen_epoch FROM alerts WHERE pair_key=?", (pair["key"],)
    ).fetchone()
    if row and now - row[0] < ALERT_COOLDOWN:
        return False
    with conn:
        conn.execute("""
          INSERT INTO alerts(pair_key,last_seen_epoch,roi_pct)
          VALUES(?,?,?)
          ON CONFLICT(pair_key)
          DO UPDATE SET last_seen_epoch=excluded.last_seen_epoch,
                        roi_pct=excluded.roi_pct
        """, (pair["key"], now, 100 * hypothetical.roi))
    s = pair["sx"]
    total = hypothetical.total_eur
    (sender or telegram)(
        "🔎 SINAL V11 · NÃO EXECUTADO\n"
        f"{s['sport']} | {s['teams'][0]} vs {s['teams'][1]}\n"
        f"Poly: {pair['poly_label']} · SX: {pair['sx_label']}\n"
        f"Total hipotético: {total:.2f} €\n"
        f"Margem estimada: {100 * hypothetical.roi:+.2f}%\n"
        "⚠️ Liquidação e execução por validar"
    )
    return True


def select_pairs(pairs):
    # Coloca próximos inícios e partidas após início à frente, mas mantém rotação.
    now = time.time()
    return sorted(
        pairs,
        key=lambda p: (
            p["sx"]["start"] > now,
            abs(p["sx"]["start"] - now),
            p["key"],
        ),
    )


_BOOK_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="books-v13")
LATENCIES = LatencyMetrics()
_BOOK_BACKOFF_LOCK = threading.Lock()
_BOOK_BACKOFF_UNTIL = 0.0


def book_backoff_until():
    with _BOOK_BACKOFF_LOCK:
        return _BOOK_BACKOFF_UNTIL


def retry_after_seconds(value, now_epoch=None):
    """Respeitar Retry-After em segundos ou data HTTP; nunca acelerar um 429."""
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now_epoch
        except (TypeError, ValueError, OverflowError):
            seconds = 2.0
    return max(2.0, seconds) if math.isfinite(seconds) else 2.0


def _timed_book(source, function, *args):
    """Medir cada fonte, sem guardar preços nem dados de autenticação."""
    global _BOOK_BACKOFF_UNTIL
    started = time.monotonic()
    status_code, ok = None, False
    try:
        result = function(*args)
        ok = True
        return result
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            delay = retry_after_seconds(getattr(response, "headers", {}).get("Retry-After", "2"))
            with _BOOK_BACKOFF_LOCK:
                _BOOK_BACKOFF_UNTIL = max(
                    _BOOK_BACKOFF_UNTIL, time.monotonic() + delay
                )
        raise
    finally:
        LATENCIES.record_source(
            source, 1000 * (time.monotonic() - started), ok=ok,
            status_code=status_code,
        )


def fetch_books(pair):
    """Duas leituras independentes, simultâneas; nunca reutilizar preços."""
    poly = _BOOK_POOL.submit(_timed_book, "poly", bot.poly_book, pair["poly_asset"])
    sx = _BOOK_POOL.submit(_timed_book, "sx", bot.sx_book, pair["sx"]["hash"], pair["sx_target"])
    wait((poly, sx))  # terminar ambas mesmo quando uma falha
    return poly.result(), sx.result()


def fast_preview(pairs, priority_cache, fx, limit=FAST_PREVIEW_LIMIT):
    """Pré-análise imediata de sinais WebSocket antes do scanner completo.

    Faz SEMPRE novas leituras REST para as duas pernas; não usa preços
    WebSocket nem abre apostas virtuais ou reais. O tempo medido começa
    na receção do WS pelo processo, não na mudança original da plataforma.
    """
    if not pairs or not priority_cache.pending:
        return 0
    now_epoch = time.time()
    eligible_by_key = {p["key"]: p for p in pairs}
    previewed = 0
    for key, (received_mono, sources) in sorted(
        list(priority_cache.pending.items()), key=lambda item: item[1][0]
    ):
        if previewed >= limit:
            break
        pair = eligible_by_key.get(key)
        if pair is None:
            continue
        sx = pair["sx"]
        start = sx["start"]
        if not (now_epoch - bot.MAX_LIVE_AGE_H * 3600 <= start
                <= now_epoch + bot.MAX_FUTURE_H * 3600):
            continue
        if start <= now_epoch and not sx["live_enabled"]:
            continue
        started_mono = time.monotonic()
        status = "sem_livros"
        margin = None
        rest_ms = None
        try:
            (poly_levels, t_poly), (sx_levels, t_sx) = fetch_books(pair)
            rest_ms = round(1000 * (time.monotonic() - started_mono))
            if abs(t_poly - t_sx) > MAX_SKEW:
                status = "snapshots_desfasados"
            else:
                preview = normalize_quote(poly_levels, sx_levels)
                if preview is not None:
                    margin = round(preview[2], 2)
                    status = "margem_indicativa"
        except Exception as exc:
            status = "erro_" + type(exc).__name__
        decided_mono = time.monotonic()
        elapsed = 1000 * (decided_mono - received_mono)
        previewed += 1
        LOG.info(
            "FAST_PREVIEW | mercado=%s | fontes=%s | estado=%s | "
            "roi_indicativo=%s | REST_ms=%s | "
            "evento_ate_inicio_ms=%d | evento_ate_calculo_ms=%d | "
            "meta_1s=%s | SEM APOSTAS/DEBITOS",
            key[:32], ",".join(sorted(sources)), status,
            f"{margin:+.2f}%" if margin is not None else "N/D",
            rest_ms if rest_ms is not None else "N/D",
            round(1000 * max(0.0, started_mono - received_mono)),
            round(elapsed), "SIM" if elapsed < FAST_PREVIEW_TARGET_MS else "NAO",
        )
    return previewed


def confirm_paper_snapshot(pair, first_poly, first_sx, t_poly, t_sx, fx,
                           paper_conn, sleep_fn=time.sleep):
    """Revalidar dois livros antes de QUALQUER nova operação virtual.

    Confirmar estabilidade do preço no melhor nível e ROI num segundo
    snapshot fresco e independente; rejeitar ROI >= 10% ou variações
    fortes. Não tocar nas carteiras nem aceitar preços do WebSocket.
    """
    first_q = paper.select_shadow_quote(
        first_poly, first_sx, fx,
        *[paper.free_balances(paper_conn)[k] for k in ("poly", "sx")]
    )
    if first_q is None:
        return None
    first_age = time.monotonic()
    if (abs(t_poly - t_sx) > min(MAX_SKEW, paper.MAX_SNAPSHOT_GAP_SECONDS)
            or not 0 <= first_age - t_poly <= MAX_QUOTE_AGE_SECONDS
            or not 0 <= first_age - t_sx <= MAX_QUOTE_AGE_SECONDS):
        return None
    # Um pico de odds entre chamadas pode gerar um falso positivo.
    first_best = (min(price for price, _ in first_poly),
                  min(price for price, _ in first_sx))
    sleep_fn(0.25)
    try:
        (second_poly, p_time), (second_sx, s_time) = fetch_books(pair)
    except Exception as exc:
        LOG.warning("PAPER_RECHECK | rejeitado=%s | erro REST %s",
                    pair["key"][:36], type(exc).__name__)
        return None
    age = time.monotonic()
    if (abs(p_time - s_time) > min(MAX_SKEW, paper.MAX_SNAPSHOT_GAP_SECONDS)
            or abs(t_poly - s_time) > 3.0
            or not 0 <= age - p_time <= MAX_QUOTE_AGE_SECONDS
            or not 0 <= age - s_time <= MAX_QUOTE_AGE_SECONDS):
        LOG.info("PAPER_RECHECK | rejeitado=%s | snapshots desfasados",
                 pair["key"][:36])
        return None
    if not second_poly or not second_sx:
        return None
    second_best = (min(price for price, _ in second_poly),
                   min(price for price, _ in second_sx))
    if any(abs(a-b) > 0.01 for a, b in zip(first_best, second_best)):
        LOG.info("PAPER_RECHECK | rejeitado=%s | mudança de preço >1 cêntimo",
                 pair["key"][:36])
        return None
    balances = paper.free_balances(paper_conn)
    second_q = paper.select_shadow_quote(
        second_poly, second_sx, fx, balances["poly"], balances["sx"]
    )
    if (second_q is None or abs(first_q.roi-second_q.roi) > 0.01
            or second_q.roi >= paper.MAX_AUTO_PAPER_ROI):
        LOG.info("PAPER_RECHECK | rejeitado=%s | ROI instável/inválido",
                 pair["key"][:36])
        return None
    LOG.info(
        "PAPER_RECHECK | confirmado=%s | ROI1=%.2f%% ROI2=%.2f%%"
        " | intervalo_ms=%d | apenas simulação",
        pair["key"][:36], 100 * first_q.roi, 100 * second_q.roi,
        round(1000 * (s_time - t_sx)),
    )
    return (second_poly, second_sx, abs(p_time-s_time),
            min(p_time, s_time) + MAX_QUOTE_AGE_SECONDS)


def scan_pass(conn, pairs, offset: int, fx: float, paper_conn=None,
              priority_cache=None, work_item=None, notify_open=None,
              notice_sender=None, verbose=True):
    scan_started = time.monotonic()
    if not pairs:
        return {"checked": 0, "priced": 0, "positive": 0, "signals": 0,
                "next_offset": 0, "best": None}
    if work_item is not None:
        pair, event, _kind = work_item
        window, new_offset = [pair], offset
        ws_events = {pair["key"]: event} if event is not None else {}
    elif priority_cache is not None:
        window, new_offset, ws_events = priority_cache.choose(
            pairs, offset, MAX_PAIRS, now=scan_started
        )
    else:
        total = len(pairs)
        window = [pairs[(offset + n) % total]
                  for n in range(min(MAX_PAIRS, total))]
        new_offset = (offset + len(window)) % total
        ws_events = {}
    checked = priced = positive = signals = 0
    hot_checked = 0
    ws_skipped = 0
    ws_latencies = []
    book_max_ms = 0.0
    best = None
    paper_reasons = Counter()

    for pair in window:
        now = time.time()
        sx = pair["sx"]
        start = sx["start"]
        event = ws_events.get(pair["key"])
        if not (now - bot.MAX_LIVE_AGE_H * 3600 <= start
                <= now + bot.MAX_FUTURE_H * 3600):
            if event is not None:
                ws_skipped += 1
            continue
        # Início ≠ jogo oficialmente ao vivo.
        if start <= now and not sx["live_enabled"]:
            if event is not None:
                ws_skipped += 1
            continue
        checked += 1
        if event is not None:
            hot_checked += 1
        book_started = time.monotonic()
        book_completed = None
        try:
            (poly_levels, t_poly), (sx_levels, t_sx) = fetch_books(pair)
            book_completed = time.monotonic()
            book_max_ms = max(book_max_ms, 1000 * (book_completed - book_started))
            stage = "APOS_INICIO_NAO_CONFIRMADO" if start <= time.time() else "PRE_JOGO"
            quote, hypothetical = insert_observation(
                conn, pair, stage, poly_levels, sx_levels,
                abs(t_poly - t_sx), fx,
            )
            if quote:
                priced += 1
                best = quote[2] if best is None else max(best, quote[2])
                if quote[2] > 0:
                    positive += 1
                # Evitar duas mensagens por uma oportunidade: quando PAPER V12
                # está ativo, só a operação shadow emite alerta individual.
                if paper_conn is None and maybe_alert(
                    conn, pair, quote, hypothetical, int(time.time()),
                    sender=notice_sender,
                ):
                    signals += 1

                # A simulação é independente do relatório de odds. Nunca faz
                # chamadas de escrita à Polymarket ou SX Bet.
                if paper_conn is not None:
                    paper_stage = ("PRE_JOGO" if stage == "PRE_JOGO"
                                   else "LIVE_NAO_CONFIRMADO")
                    booked = None
                    # Evitar REST duplicado em todas as direções. Só repetir
                    # quando a primeira leitura sugerir entrada elegível.
                    if (stage == "PRE_JOGO"
                            or paper.ALLOW_LIVE_UNVERIFIED):
                        existing = paper_conn.execute(
                            "SELECT 1 FROM paper_v12_trades WHERE event_key=?",
                            (paper.event_key(pair),),
                        ).fetchone()
                        if existing:
                            paper_reasons["ja_registada"] += 1
                            validated = None
                        else:
                            validated = confirm_paper_snapshot(
                                pair, poly_levels, sx_levels, t_poly, t_sx,
                                fx, paper_conn
                            )
                        if validated:
                            final_poly, final_sx, final_skew, valid_until = validated
                            # O pré-jogo não é uma autorização reutilizável
                            # depois de atravessar o início durante o recheck.
                            if paper_stage == "PRE_JOGO" and start <= time.time():
                                paper_reasons["inicio_durante_validacao"] += 1
                            elif start < time.time() - bot.MAX_LIVE_AGE_H * 3600:
                                paper_reasons["fora_da_janela"] += 1
                            else:
                                booked = paper.try_paper_entry(
                                    paper_conn, pair, final_poly, final_sx, fx,
                                    paper_stage, final_skew,
                                    valid_until_mono=valid_until,
                                )
                        if not existing and not booked:
                            reason = (
                                "abaixo_margem" if quote[2] < 100 * paper.MIN_NET_ROI
                                else "margem_anomala" if quote[2] >= 100 * paper.MAX_AUTO_PAPER_ROI
                                else "validacao_sem_registo"
                            )
                            paper_reasons[reason] += 1
                    else:
                        paper_reasons["fase_nao_confirmada"] += 1
                    if booked:
                        paper_reasons["registada"] += 1
                        shadow_id, shadow_quote = booked
                        LOG.info(
                            "PAPER V12 #%d | %s | %.2f EUR | "
                            "lucro condicional +%.2f EUR | ROI %.2f%%",
                            shadow_id, " vs ".join(sx["teams"]),
                            shadow_quote.total_eur, shadow_quote.profit_eur,
                            100 * shadow_quote.roi,
                        )
                        # Mensagem completa guardada no SQLite na mesma
                        # transação da operação virtual. Nunca usar o envio
                        # genérico sem confirmação de entrega.
                        if notify_open is not None:
                            notify_open()
                            LOG.info("PAPER_ALERT | guardado #%d | envio em segundo plano", shadow_id)
                        elif paper.send_open_once(paper_conn, shadow_id):
                            LOG.info("PAPER_ALERT | enviado #%d | Telegram confirmado", shadow_id)
                        else:
                            LOG.warning("PAPER_ALERT | pendente #%d | Telegram não confirmou; retry automático", shadow_id)
            elif paper_conn is not None:
                paper_reasons["sem_cotacao_valida"] += 1
        except Exception as exc:
            book_max_ms = max(book_max_ms, 1000 * (time.monotonic() - book_started))
            LOG.warning(
                "Book falhou em %s (%s)",
                pair["key"][:35], type(exc).__name__,
            )
        finally:
            # WS -> fila -> REST de ambos os livros -> avaliação/alerta.
            # Mede tempo LOCAL do evento recebido, não latência da rede
            # anterior ao recebimento nem preenchimento de uma ordem real.
            if event is not None:
                received_mono, sources = event
                decided = time.monotonic()
                ws_latencies.append({
                    "queue": max(0, book_started-received_mono)*1000,
                    "rest": max(0, (book_completed or decided)-book_started)*1000,
                    "confirmed": (max(0, book_completed-received_mono)*1000
                                  if book_completed is not None else None),
                    "decision": max(0, decided-received_mono)*1000,
                    "sources": sources,
                })
    for timing in ws_latencies:
        LATENCIES.record_decision(
            timing["queue"], timing["rest"], timing["decision"],
            sources=timing["sources"],
        )
    result = {
        "checked": checked, "priced": priced, "positive": positive,
        "signals": signals, "next_offset": new_offset, "best": best,
        "ws_latencies": ws_latencies, "ws_skipped": ws_skipped,
        "book_max_ms": book_max_ms, "hot_checked": hot_checked,
        "duration_ms": 1000 * (time.monotonic() - scan_started),
        "paper_reasons": dict(paper_reasons),
    }
    if verbose:
        log_scan_results([result])
    return result


def log_scan_results(results):
    """Agrupar até MAX_PAIRS análises para comparar os máximos com V13.1."""
    ws_latencies = [entry for result in results for entry in result.get("ws_latencies", [])]
    if ws_latencies:
        confirmed = [x["confirmed"] for x in ws_latencies if x["confirmed"] is not None]
        LOG.info(
            "WS_LATENCY | mercados=%d | fontes=%s | "
            "evento_ate_inicio_ms_max=%d | REST_ms_max=%d | "
            "evento_ate_validacao_ms_max=%s | "
            "evento_ate_decisao_ms_max=%d | descartados=%d",
            len(ws_latencies),
            ",".join(sorted(set().union(*(x["sources"] for x in ws_latencies)))),
            round(max(x["queue"] for x in ws_latencies)),
            round(max(x["rest"] for x in ws_latencies)),
            str(round(max(confirmed))) if confirmed else "N/D",
            round(max(x["decision"] for x in ws_latencies)),
            sum(result.get("ws_skipped", 0) for result in results),
        )
    LOG.info(
        "SCAN | direções=%d | preços válidos=%d | margens positivas=%d "
        "| sinais=%d | melhor buffer=%s | duracao_ms=%d "
        "| maior_leitura_ms=%d | sinais_WS_verificados=%d",
        sum(r["checked"] for r in results), sum(r["priced"] for r in results),
        sum(r["positive"] for r in results), sum(r["signals"] for r in results),
        (f"{max(r['best'] for r in results if r['best'] is not None):+.2f}%"
         if any(r["best"] is not None for r in results) else "N/D"),
        round(sum(r.get("duration_ms", 0) for r in results)),
        round(max((r.get("book_max_ms", 0) for r in results), default=0)),
        sum(r.get("hot_checked", 0) for r in results),
    )
    paper_reasons = Counter()
    for result in results:
        paper_reasons.update(result.get("paper_reasons", {}))
    if paper_reasons:
        LOG.info("PAPER_DIAGNOSTICO | %s", dict(paper_reasons))


def summary(conn, since_hours=6, now_epoch=None):
    """Resumo de uma janela móvel, sem duplicar as duas direções por confronto."""
    current_epoch = time.time() if now_epoch is None else now_epoch
    cutoff = datetime.fromtimestamp(
        current_epoch - since_hours * 3600, tz=timezone.utc
    ).isoformat(timespec="seconds")
    n, priced, pos, best, anomalies, unique_events, potential_signals = conn.execute("""
        SELECT COUNT(*),
               COUNT(buffered_roi_pct),
               COALESCE(SUM(CASE WHEN buffered_roi_pct > 0 THEN 1 ELSE 0 END), 0),
               MAX(CASE WHEN buffered_roi_pct < 10 THEN buffered_roi_pct END),
               COALESCE(SUM(CASE WHEN buffered_roi_pct >= 10 THEN 1 ELSE 0 END),0),
               COUNT(DISTINCT poly_market || '|' || sx_market),
               COUNT(DISTINCT CASE
                 WHEN hypothetical_profit_eur > 0 AND hypothetical_total_eur > 0
                      AND buffered_roi_pct < 10
                 THEN poly_market || '|' || sx_market END)
        FROM observations WHERE observed_at >= ?
    """, (cutoff,)).fetchone()
    return {
        "records": int(n),
        "priced": int(priced),
        "positive": int(pos),
        "unique": int(unique_events),
        "best": best,
        "suspicious": int(anomalies),
        "potential_signals": int(potential_signals),
    }


def format_summary(stats, catalog_count, window_hours=6, fx_available=False):
    """Formato curto do Telegram, sem confundir sinais com apostas feitas."""
    best = f"{stats['best']:+.2f}%" if stats["best"] is not None else "N/D"
    potentials = str(stats["potential_signals"]) if fx_available else "N/D (sem câmbio)"
    return "\n".join([
        f"📊 TRAFULHA V{APP_VERSION} · ÚLTIMAS {window_hours:g}H",
        f"🏟️ Confrontos analisados: {stats['unique']}",
        (f"📚 Catálogo: {CATALOG_GAMES} jogos · {catalog_count} comparações"
         if CATALOG_GAMES is not None else
         f"📚 Direções comparáveis no catálogo: {catalog_count}"),
        f"📚 Odds válidas: {stats['priced']}/{stats['records']}",
        f"📈 Melhor margem indicativa (<10%, não validada): {best}",
        f"⚠️ Leituras anómalas (>=10%, excluídas): {stats.get('suspicious', 0)}",
        f"🔎 Sinais potenciais (<10%, por verificar): {potentials}",
        "🔒 Só leitura · sem apostas",
    ])


def report_due(conn, now_epoch=None):
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    last = conn.execute("""
        SELECT last_report_epoch FROM report_schedule WHERE report_key='six_hour'
    """).fetchone()
    return last is None or now_epoch - int(last[0]) >= SUMMARY_INTERVAL


def record_report_attempt(conn, now_epoch=None):
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    with conn:
        conn.execute("""
            INSERT INTO report_schedule(report_key,last_report_epoch)
            VALUES('six_hour', ?)
            ON CONFLICT(report_key) DO UPDATE
            SET last_report_epoch=excluded.last_report_epoch
        """, (now_epoch,))


def send_summary(conn, catalog_count, startup=False, now_epoch=None, paper_conn=None,
                 sender=None):
    """Envia resumo de 6h; memoriza o envio para não repetir após reinício."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    hours = SUMMARY_INTERVAL / 3600.0
    stats = summary(conn, since_hours=hours, now_epoch=now_epoch)
    message = format_summary(
        stats, catalog_count, window_hours=hours, fx_available=bot.FX > 0
    )
    if paper_conn is not None:
        totals = paper.paper_summary(paper_conn)
        message += (
            f"\n🧪 Paper: ROI mínimo configurado {100 * paper.MIN_NET_ROI:.2f}%"
            f"\n🧾 Paper: {totals['open_trades']} abertas · "
            f"{totals['settled']} liquidadas · {totals['requires_review']} em revisão"
            f"\n💰 Polymarket: {paper.eur(int(round(totals['poly_free_eur'] * 100)))} livres"
            f"\n💰 SX Bet: {paper.eur(int(round(totals['sx_free_eur'] * 100)))} livres"
            f"\n🔒 Em operações abertas: {paper.eur(int(round(totals['exposure_eur'] * 100)))}"
            f"\n📈 Resultado realizado: {paper.eur(int(round(totals['realized_eur'] * 100)))}"
            "\n⚠️ Saldos virtuais"
        )
        suspect_trades = paper_conn.execute("""
            SELECT COUNT(*) FROM paper_v12_trades
            WHERE status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED')
              AND conditional_profit_cents >=
                  (poly_debit_cents + sx_debit_cents) * 0.10
        """).fetchone()[0]
        if suspect_trades:
            message += (
                f"\n🚨 {suspect_trades} operação(ões) antigas com ROI >=10%"
                " — REVISÃO MANUAL RECOMENDADA, sem liquidação automática"
            )
    if sender is None:
        telegram(message)
    elif sender(message) is False:
        return False
    record_report_attempt(conn, now_epoch)
    LOG.info(
        "Relatório Telegram de %.0fh: confrontos=%d, odds=%d/%d, "
        "sinais potenciais=%d, melhor=%s",
        hours, stats["unique"], stats["priced"], stats["records"],
        stats["potential_signals"],
        f"{stats['best']:+.2f}%" if stats["best"] is not None else "N/D",
    )
    if startup:
        LOG.info("Monitor ativo; primeiro resumo periódico emitido.")


def update_health(conn, checked, priced, now_epoch=None):
    """Devolve alerta curto se ficarmos sem odds válidas muito tempo.

    Persistente: não reinicia o relógio da falha após reiniciar o Railway.
    """
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    if priced > 0:
        with conn:
            conn.execute("""
                INSERT INTO monitor_health(key,epoch) VALUES ('last_valid',?)
                ON CONFLICT(key) DO UPDATE SET epoch=excluded.epoch
            """, (now_epoch,))
        return None
    if checked <= 0:
        # Sem mercados verificados nesta passagem, não afirmar falha das APIs.
        return None
    row = conn.execute(
        "SELECT epoch FROM monitor_health WHERE key='last_valid'"
    ).fetchone()
    if row is None:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO monitor_health(key,epoch) VALUES ('start',?)",
                (now_epoch,),
            )
        row = conn.execute(
            "SELECT epoch FROM monitor_health WHERE key='start'"
        ).fetchone()
    last_valid = int(row[0])
    if now_epoch - last_valid < OUTAGE_THRESHOLD:
        return None
    previous = conn.execute(
        "SELECT epoch FROM monitor_health WHERE key='last_warning'"
    ).fetchone()
    if previous is not None and now_epoch - int(previous[0]) < OUTAGE_REPEAT:
        return None
    with conn:
        conn.execute("""
            INSERT INTO monitor_health(key,epoch) VALUES ('last_warning',?)
            ON CONFLICT(key) DO UPDATE SET epoch=excluded.epoch
        """, (now_epoch,))
    return (
        "⚠️ MONITOR V11/V12\n"
        f"Sem odds válidas há pelo menos {OUTAGE_THRESHOLD // 60} min.\n"
        "Verificar logs do Railway e APIs.\n"
        "🔒 Sem apostas"
    )


def catalog_snapshot():
    """Construir metadados completos; não toca no catálogo em utilização."""
    poly = bot.discover_poly()
    sx = bot.discover_sx()
    pairs = select_pairs(bot.pair_markets(poly, sx))
    stats = dict(Counter(p["sx"]["sport"] for p in pairs))
    games = len({
        (p["sx"]["sport"], p["sx"]["start"],
         tuple(sorted(bot.ascii_name(team) for team in p["sx"]["teams"])))
        for p in pairs
    })
    return pairs, games, len(poly), len(sx), stats


def accept_catalog(snapshot):
    global CATALOG_GAMES
    pairs, games, poly_count, sx_count, stats = snapshot
    CATALOG_GAMES = games
    LOG.info(
        "CATALOGO | Poly=%d SX=%d jogos=%d direções=%d | desportos=%s",
        poly_count, sx_count, games, len(pairs), stats,
    )
    return pairs


def catalog():
    return accept_catalog(catalog_snapshot())


def self_test():
    import tempfile

    # Modo manutenção não deve iniciar scanner nem escrever SQLite.
    saved_maintenance = os.environ.get("TRAFLUHA_STORAGE_MAINTENANCE")
    try:
        os.environ["TRAFLUHA_STORAGE_MAINTENANCE"] = "1"
        assert main(once=True) is None
    finally:
        if saved_maintenance is None:
            os.environ.pop("TRAFLUHA_STORAGE_MAINTENANCE", None)
        else:
            os.environ["TRAFLUHA_STORAGE_MAINTENANCE"] = saved_maintenance

    # Fila com prioridade sem bloquear rotação REST e sem preços em cache.
    ws_priority.self_test()
    # Preview é exclusivamente leitura, sem acesso à SQLite do paper.
    class PendingOnly:
        pending = {"test": (time.monotonic() - .01, {"POLY"})}
    sample = [{
        "key": "test", "poly_asset": "a",
        "sx": {"hash": "s", "sx_target": 0, "start": time.time()+600,
               "live_enabled": False},
        "sx_target": 0,
    }]
    old_poly_fn, old_sx_fn = bot.poly_book, bot.sx_book
    seen = []
    try:
        bot.poly_book = lambda asset: (seen.append("poly") or
                                      [(0.46, 200)], time.time())
        bot.sx_book = lambda hash_, target: (seen.append("sx") or
                                             [(0.51, 200)], time.time())
        assert fast_preview(sample, PendingOnly(), fx=0.91) == 1
        assert sorted(seen) == ["poly", "sx"]
    finally:
        bot.poly_book, bot.sx_book = old_poly_fn, old_sx_fn


    # Evento WS não pode impor os 8 segundos adicionais da versão antiga.
    assert websocket_reaction_pause(0.1, True, 2.0) == 1.9
    assert websocket_reaction_pause(2.5, True, 2.0) == 0.0
    assert websocket_reaction_pause(0.1, False, 2.0) == 0.0

    assert abs(normalize_quote([(0.45, 30)], [(0.50, 30)])[1] - 5.26315789) < 0.0001
    assert normalize_quote([(0.55, 30)], [(0.52, 30)])[2] < 0
    assert normalize_quote([], [(0.5, 10)]) is None
    with tempfile.TemporaryDirectory() as td:
        conn = open_database(str(Path(td) / "test.sqlite3"))
        fake = {
            "key": "poly:1/sx:1",
            "poly": {"id": "poly:1"},
            "sx": {"sport": "tennis", "teams": ("A", "B"), "hash": "sx:1"},
            "poly_label": "A", "sx_label": "B",
        }
        quote, hypo = insert_observation(
            conn, fake, "PRE_JOGO", [(0.45, 30)], [(0.50, 30)], 1.0, 0
        )
        assert quote and hypo is None
        report = summary(conn)
        assert report["records"] == 1 and report["positive"] == 1
        assert report["unique"] == 1 and report["priced"] == 1
        assert report["potential_signals"] == 0
        # Repetir a mesma direção não duplica continuamente o histórico.
        insert_observation(
            conn, fake, "PRE_JOGO", [(0.45, 30)], [(0.50, 30)], 1.0, 0
        )
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1

        # Uma segunda direção do mesmo confronto não conta como novo jogo.
        reverse = {**fake, "key": "poly:1/sx:1:reverse",
                   "poly_label": "B", "sx_label": "A"}
        insert_observation(
            conn, reverse, "PRE_JOGO", [(0.56, 30)], [(0.55, 30)], 1.0, 0
        )
        report = summary(conn)
        assert report["records"] == 2 and report["unique"] == 1
        assert report["best"] > 0

        msg = format_summary(report, 2, fx_available=False)
        assert "Confrontos analisados: 1" in msg
        assert "ÚLTIMAS 6H" in msg
        assert "N/D (sem câmbio)" in msg
        assert "sem apostas" in msg

        assert report_due(conn)
        record_report_attempt(conn)
        assert not report_due(conn)
        assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
        conn.close()
    # Integração offline: um sinal WS entra ANTES da rotação normal.
    # Nunca usa APIs reais nem cria uma aposta real/virtual.
    import io
    from contextlib import suppress
    with tempfile.TemporaryDirectory() as td:
        db = open_database(str(Path(td) / "priority.sqlite3"))
        now = time.time()
        observed = []
        pairs = [{
            "key": str(i), "poly_asset": f"poly-{i}",
            "poly": {"id": f"market-{i}"},
            "sx": {"hash": f"sx-{i}", "start": now + 3600,
                   "live_enabled": False, "sport": "tennis",
                   "teams": ("Team A", "Team B")},
            "sx_target": 1,
            "poly_label": "Team A", "sx_label": "Team B",
        } for i in range(3)]
        queue = ws_priority.PriorityCache(max_priority=1)
        queue.rebuild(pairs)
        queue.ingest({"poly-2": time.monotonic() - 0.01})
        old_poly_book, old_sx_book = bot.poly_book, bot.sx_book
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        old_level = LOG.level
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
        try:
            def fake_poly(asset):
                observed.append(asset)
                return [(0.55, 300)], time.monotonic()
            def fake_sx(market, target):
                return [(0.60, 300)], time.monotonic()
            bot.poly_book, bot.sx_book = fake_poly, fake_sx
            result = scan_pass(db, pairs, 0, fx=0, priority_cache=queue)
            assert result["checked"] == 3
            assert observed == ["poly-2", "poly-0", "poly-1"]
            assert "WS_LATENCY" in stream.getvalue()
            assert "evento_ate_validacao_ms_max=" in stream.getvalue()
            assert not queue.pending
            assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
        finally:
            bot.poly_book, bot.sx_book = old_poly_book, old_sx_book
            LOG.removeHandler(handler)
            LOG.setLevel(old_level)
            db.close()
    print("✅ Monitor V11: testes locais OK (sem APIs, sem apostas)")


def websocket_reaction_pause(elapsed_seconds, wake_received, min_gap=WS_REACT_GAP_SECONDS):
    """Só aplicar pausa curta após atualização WS; demais ciclos são periódicos."""
    if not wake_received:
        return 0.0
    return max(0.0, min_gap - elapsed_seconds)


def main(once=False):
    LOG.info("TRAFULHA | versao=%s | execucao_real=INEXISTENTE", APP_VERSION)
    # Modo de manutenção: permite montar /data para backup sem tentar
    # abrir SQLite cheia. Não apaga nem altera qualquer base de dados.
    if os.getenv("TRAFLUHA_STORAGE_MAINTENANCE", "0").strip().lower() in {"1", "true", "yes"}:
        LOG.warning(
            "STORAGE MAINTENANCE: scanner e Paper V12 pausados. "
            "Volume /data preservado para backup e recuperação; "
            "NENHUMA aposta real/virtual e nenhuma escrita SQLite."
        )
        if once:
            return
        while True:
            time.sleep(3600)
    fx = float(os.getenv("EUR_PER_USD", "0") or "0")
    if fx < 0 or fx > 10:
        raise SystemExit("EUR_PER_USD inválido. Usa euros por 1 USD, por exemplo cotação atual.")
    bot.FX = fx

    if fx == 0:
        LOG.warning(
            "EUR_PER_USD ausente: cálculo de stake de 100 EUR e alertas "
            "de oportunidades desativados; só se guarda o histórico."
        )

    # Bloquear arranque com volume cheio, antes de abrir ou alterar SQLite.
    storage_paths = [Path(DB).expanduser().resolve().parent]
    if PAPER_V12_ENABLED and fx > 0:
        storage_paths.append(Path(paper.PAPER_DB).expanduser().resolve().parent)
    mount_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if mount_path:
        persistent_root = Path(mount_path).resolve()
        if any(not path.is_relative_to(persistent_root) for path in storage_paths):
            raise SystemExit("As bases SQLite devem permanecer no volume persistente do Railway")
    if any(shutil.disk_usage(path if path.exists() else path.parent).free < STORAGE_FREE_FLOOR for path in storage_paths):
        raise SystemExit("STORAGE | menos de 32MiB livres; arranque bloqueado, bases preservadas")
    conn = open_database(DB)
    scheduler = ReactiveScheduler(
        max_hot_burst=WS_PRIORITY_MAX, normal_interval=INTERVAL / MAX_PAIRS,
        min_key_interval=WS_KEY_INTERVAL, ttl_seconds=WS_EVENT_TTL,
    )
    ws_feed = sx_hints.SXBookHints() if SX_WS_V12_ENABLED else None
    if ws_feed is not None and not ws_feed.start():
        LOG.warning("SX WS V12 solicitado mas sem SX_API_KEY: usar apenas REST.")
        ws_feed = None
    poly_feed = poly_hints.PolyBookHints() if POLY_WS_V12_ENABLED else None
    if poly_feed is not None:
        poly_feed.start()
    paper_conn = paper.open_database() if PAPER_V12_ENABLED and fx > 0 else None
    result_worker = None
    if PAPER_V12_ENABLED and paper_conn is None:
        LOG.warning("PAPER V12 pediu ativação, mas EUR_PER_USD inválido/ausente: desativado.")
    if paper_conn is not None:
        LOG.info("PAPER V12 ATIVO: ROI mínimo %.2f%% após custos simulados; "
                 "1000 EUR virtuais por plataforma, "
                 "máximo 100 EUR total por operação, sem apostas reais.",
                 100 * paper.MIN_NET_ROI)
        balances = paper.paper_summary(paper_conn)
        LOG.info(
            "PAPER_ESTADO | abertas=%d | liquidadas=%d | revisao=%d | "
            "saldo_poly=%.2f | saldo_sx=%.2f | exclusivamente_virtual",
            balances["open_trades"], balances["settled"],
            balances["requires_review"], balances["poly_free_eur"], balances["sx_free_eur"],
        )
        if not once:
            result_worker = PaperLifecycleWorker(
                paper.PAPER_DB,
                interval_seconds=int(os.getenv("PAPER_RESULTS_INTERVAL_SECONDS", "60")),
                batch_size=int(os.getenv("PAPER_RESULTS_BATCH_SIZE", "5")),
                results_enabled=PAPER_RESULTS_ENABLED,
            )
            result_worker.start()
    LOG.info(
        "REACTIVE V13.2 | uma direção por despacho; pré-análise integrada; "
        "REST fresco nas duas fontes e recheck de 250ms preservados"
    )
    LOG.info(
        "WS PRIORITY | quota=%d | rotação=%.2fs | mínimo/chave=%.2fs | "
        "máximo_pares/s=%.1f | timeout_livros=%.1fs | TTL=%.0fs | sem cache de odds",
        WS_PRIORITY_MAX, INTERVAL / MAX_PAIRS, WS_KEY_INTERVAL,
        1.0 / PAIR_START_GAP, bot.BOOK_TIMEOUT_SECONDS, WS_EVENT_TTL,
    )
    pairs = []
    catalog_ready = False
    catalog_worker = SnapshotRefresher(catalog_snapshot) if not once else None
    notice_worker = AsyncNoticeSender(telegram) if not once else None
    if catalog_worker is not None:
        catalog_worker.start()
    if notice_worker is not None:
        notice_worker.start()
    next_catalog = 0.0
    iteration = 0
    last_prune = 0.0
    last_health_check = 0.0
    last_summary_check = 0.0
    last_metrics = time.monotonic()
    last_batch_log = last_metrics
    last_pair_start = 0.0
    batch = []
    try:
        while True:
            started = time.monotonic()
            if started - last_health_check >= 1 or last_health_check == 0:
                if any(shutil.disk_usage(path).free < STORAGE_FREE_FLOOR for path in storage_paths):
                    raise SystemExit("STORAGE | reserva de 32MiB atingida; parar com erro explícito e preservar bases")
                if result_worker is not None and not result_worker.is_alive():
                    raise RuntimeError("Worker Paper parou; reiniciar serviço para recuperar")
                if catalog_worker is not None and not catalog_worker.thread.is_alive():
                    raise RuntimeError("Worker do catálogo parou")
                if notice_worker is not None and not notice_worker.thread.is_alive():
                    raise RuntimeError("Worker dos resumos parou")
                last_health_check = started
            if started - last_prune >= 60 or last_prune == 0:
                try:
                    removed = prune_observations(conn)
                    if removed:
                        LOG.info("STORAGE | %d leituras antigas eliminadas", removed)
                except sqlite3.Error as exc:
                    LOG.warning("STORAGE | falha na limpeza: %s", type(exc).__name__)
                last_prune = started
            if once:
                pairs = catalog()
                catalog_ready = True
            else:
                refreshed = catalog_worker.poll()
                if refreshed is not None:
                    ok, snapshot = refreshed
                    if ok:
                        pairs = accept_catalog(snapshot)
                        scheduler.rebuild(pairs)
                        catalog_ready = True
                    else:
                        LOG.warning("CATALOGO | atualização adiada (%s); manter snapshot completo", snapshot)
                        next_catalog = started + 30.0
                if refreshed is not None and refreshed[0]:
                    if ws_feed is not None:
                        ws_feed.update(p["sx"]["hash"] for p in pairs[:24])
                    if poly_feed is not None:
                        poly_feed.update(p["poly_asset"] for p in pairs[:24])
                if started >= next_catalog and catalog_worker.request():
                    next_catalog = started + CATALOG_INTERVAL

            if batch and (len(batch) >= MAX_PAIRS or started - last_batch_log >= 5):
                log_scan_results(batch)
                batch.clear()
                last_batch_log = started
            if started - last_metrics >= METRICS_INTERVAL:
                metrics = LATENCIES.snapshot()
                metrics["pending_events"] = len(scheduler.pending)
                metrics["queue_drops"] = dict(scheduler.metrics)
                metrics["coverage"] = scheduler.coverage()
                LOG.info("LATENCY_V13 | %s", json.dumps(metrics, separators=(",", ":")))
                last_metrics = started

            # Também emitir resumos se um catálogo completo tiver zero pares,
            # ou se a leitura estiver em recuo por limite da API.
            if not once and catalog_ready and started - last_summary_check >= 1:
                if report_due(conn):
                    send_summary(
                        conn, len(pairs), startup=(iteration == 0),
                        paper_conn=paper_conn, sender=notice_worker.submit,
                    )
                last_summary_check = started

            if once:
                result = scan_pass(conn, pairs, 0, fx, paper_conn=paper_conn)
            else:
                # Drenar a cada direção, incluindo sinais recebidos durante REST.
                sx_events = ws_feed.take_events() if ws_feed is not None else {}
                poly_events = poly_feed.take_events() if poly_feed is not None else {}
                scheduler.ingest(poly_events, sx_events)
                now_mono = time.monotonic()
                wait_until = max(last_pair_start + PAIR_START_GAP, book_backoff_until())
                if now_mono < wait_until:
                    time.sleep(min(0.05, wait_until - now_mono))
                    continue
                item = scheduler.choose(now=now_mono)
                if item is None:
                    time.sleep(max(0.001, scheduler.idle_seconds(max_wait=0.05)))
                    continue
                last_pair_start = time.monotonic()
                try:
                    result = scan_pass(
                        conn, [item[0]], 0, fx, paper_conn=paper_conn,
                        work_item=item, verbose=False,
                        notify_open=result_worker.notify if result_worker else None,
                        notice_sender=notice_worker.submit,
                    )
                finally:
                    scheduler.completed(item[0]["key"])
                batch.append(result)
            outage_notice = update_health(conn, result["checked"], result["priced"])
            if outage_notice:
                LOG.warning("Sem odds válidas durante o período de alerta.")
                if notice_worker is None:
                    telegram(outage_notice)
                else:
                    notice_worker.submit(outage_notice)
            iteration += 1

            if once:
                send_summary(conn, len(pairs), startup=True, paper_conn=paper_conn)

            if once:
                print("✅ Passagem única concluída; só leitura, sem apostas.")
                break
    except KeyboardInterrupt:
        LOG.info("Monitor interrompido.")
    finally:
        if batch:
            log_scan_results(batch)
        if catalog_worker is not None:
            catalog_worker.stop()
        if notice_worker is not None:
            notice_worker.stop()
        if result_worker is not None:
            result_worker.stop()
        if ws_feed is not None:
            ws_feed.stop()
        if poly_feed is not None:
            poly_feed.stop()
        conn.close()
        if paper_conn is not None:
            paper_conn.close()


def handle_shutdown(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        signal.signal(signal.SIGTERM, handle_shutdown)
        main(once=args.once)
