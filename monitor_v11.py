"""V11 Monitor — acompanhamento contínuo, histórico SQLite e sinais de leitura.

Nunca executa apostas reais ou virtuais. Os candidatos são hipotéticos e
precisam de confirmação de regras de liquidação, taxas e execução.
Início: python monitor_v11.py
Teste local: python monitor_v11.py --self-test
Teste das APIs (uma passagem): python monitor_v11.py --once
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import trafulha_arbitragem as bot
import paper_v12 as paper
import sx_ws_v12 as sx_hints
import poly_ws_v12 as poly_hints
import ws_priority_v12 as ws_priority

LOG = logging.getLogger("monitor_v11")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

DB = os.getenv("MONITOR_DB", "monitor_v11.sqlite3")
INTERVAL = max(10, int(os.getenv("MONITOR_INTERVAL_SECONDS", "20")))
CATALOG_INTERVAL = max(180, int(os.getenv("MONITOR_CATALOG_SECONDS", "600")))
MAX_PAIRS = min(60, max(1, int(os.getenv("MONITOR_PAIRS_PER_PASS", "15"))))
# Reação a eventos WS: 2s entre ciclos para evitar tempestades de pedidos REST.
# NÃO é promessa de execução <10s, nem habilita apostas reais/virtuais LIVE.
WS_REACT_GAP_SECONDS = min(10.0, max(
    1.0, float(os.getenv("MONITOR_WS_REACT_GAP_SECONDS", "2.0"))
))
# Quantos eventos WS entram à frente dos restantes mercados em cada scan.
WS_PRIORITY_MAX = min(6, max(1, int(os.getenv("MONITOR_WS_PRIORITY_MAX", "4"))))
# Cache temporária SOMENTE de identificadores/timestamps, nunca de preços.
WS_EVENT_TTL = min(60.0, max(5.0,
    float(os.getenv("MONITOR_WS_EVENT_TTL_SECONDS", "20"))
))
ALERT_COOLDOWN = max(300, int(os.getenv("MONITOR_ALERT_COOLDOWN_SECONDS", "1800")))
SUMMARY_INTERVAL = max(3600, int(os.getenv("MONITOR_SUMMARY_SECONDS", "21600")))
OUTAGE_THRESHOLD = max(900, int(os.getenv("MONITOR_OUTAGE_SECONDS", "1800")))
OUTAGE_REPEAT = max(1800, int(os.getenv("MONITOR_OUTAGE_REPEAT_SECONDS", "3600")))
MAX_SKEW = 6.0
MIN_ROI = paper.MIN_NET_ROI    # Usar mesmo ROI do Paper V12, nunca o limiar legado V11
PAPER_V12_ENABLED = os.getenv("PAPER_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
SX_WS_V12_ENABLED = os.getenv("SX_WS_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
POLY_WS_V12_ENABLED = os.getenv("POLY_WS_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

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


def insert_observation(conn, pair, stage, poly_levels, sx_levels, skew, fx):
    quote = normalize_quote(poly_levels, sx_levels) if skew <= MAX_SKEW else None
    hypothetical = hypothetical_100_eur(poly_levels, sx_levels, fx) if quote else None
    p_best = poly_levels[0][0] if poly_levels else None
    s_best = sx_levels[0][0] if sx_levels else None
    event = " vs ".join(pair["sx"]["teams"])
    note = ("Livros lidos; equivalência de liquidação não validada."
            if quote else "Sem preço ou snapshots demasiado afastados.")

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


def maybe_alert(conn, pair, quote, hypothetical, now):
    if not hypothetical or not quote or quote[2] < MIN_ROI * 100:
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
    telegram(
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


def scan_pass(conn, pairs, offset: int, fx: float, paper_conn=None,
              priority_cache=None):
    scan_started = time.monotonic()
    if not pairs:
        return {"checked": 0, "priced": 0, "positive": 0, "signals": 0,
                "next_offset": 0, "best": None}
    if priority_cache is not None:
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
    now = time.time()

    for pair in window:
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
            poly_levels, t_poly = bot.poly_book(pair["poly_asset"])
            sx_levels, t_sx = bot.sx_book(sx["hash"], pair["sx_target"])
            book_completed = time.monotonic()
            book_max_ms = max(book_max_ms, 1000 * (book_completed - book_started))
            stage = "APOS_INICIO_NAO_CONFIRMADO" if start <= now else "PRE_JOGO"
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
                    conn, pair, quote, hypothetical, int(time.time())
                ):
                    signals += 1

                # A simulação é independente do relatório de odds. Nunca faz
                # chamadas de escrita à Polymarket ou SX Bet.
                if paper_conn is not None:
                    paper_stage = ("PRE_JOGO" if stage == "PRE_JOGO"
                                   else "LIVE_NAO_CONFIRMADO")
                    booked = paper.try_paper_entry(
                        paper_conn, pair, poly_levels, sx_levels, fx,
                        paper_stage, abs(t_poly - t_sx)
                    )
                    if booked:
                        shadow_id, shadow_quote = booked
                        LOG.info(
                            "PAPER V12 #%d | %s | %.2f EUR | "
                            "lucro condicional +%.2f EUR | ROI %.2f%%",
                            shadow_id, " vs ".join(sx["teams"]),
                            shadow_quote.total_eur, shadow_quote.profit_eur,
                            100 * shadow_quote.roi,
                        )
                        telegram(paper.format_open_message(
                            paper_conn, shadow_id,
                            odds_poly=1.0 / min(p for p, _ in poly_levels),
                            odds_sx=1.0 / min(p for p, _ in sx_levels),
                        ))
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
            ws_skipped,
        )
    LOG.info(
        "SCAN | direções=%d | preços válidos=%d | margens positivas=%d "
        "| sinais=%d | melhor buffer=%s | duracao_ms=%d "
        "| maior_leitura_ms=%d | sinais_WS_verificados=%d",
        checked, priced, positive, signals,
        f"{best:+.2f}%" if best is not None else "N/D",
        round(1000 * (time.monotonic() - scan_started)),
        round(book_max_ms), hot_checked,
    )
    return {"checked": checked, "priced": priced, "positive": positive,
            "signals": signals, "next_offset": new_offset, "best": best}


def summary(conn, since_hours=6, now_epoch=None):
    """Resumo de uma janela móvel, sem duplicar as duas direções por confronto."""
    current_epoch = time.time() if now_epoch is None else now_epoch
    cutoff = datetime.fromtimestamp(
        current_epoch - since_hours * 3600, tz=timezone.utc
    ).isoformat(timespec="seconds")
    n, priced, pos, best, unique_events, potential_signals = conn.execute("""
        SELECT COUNT(*),
               COUNT(buffered_roi_pct),
               COALESCE(SUM(CASE WHEN buffered_roi_pct > 0 THEN 1 ELSE 0 END), 0),
               MAX(buffered_roi_pct),
               COUNT(DISTINCT poly_market || '|' || sx_market),
               COUNT(DISTINCT CASE
                 WHEN hypothetical_profit_eur > 0 AND hypothetical_total_eur > 0
                 THEN poly_market || '|' || sx_market END)
        FROM observations WHERE observed_at >= ?
    """, (cutoff,)).fetchone()
    return {
        "records": int(n),
        "priced": int(priced),
        "positive": int(pos),
        "unique": int(unique_events),
        "best": best,
        "potential_signals": int(potential_signals),
    }


def format_summary(stats, catalog_count, window_hours=6, fx_available=False):
    """Formato curto do Telegram, sem confundir sinais com apostas feitas."""
    best = f"{stats['best']:+.2f}%" if stats["best"] is not None else "N/D"
    potentials = str(stats["potential_signals"]) if fx_available else "N/D (sem câmbio)"
    return "\n".join([
        f"📊 MONITOR V11 · ÚLTIMAS {window_hours:g}H",
        f"🏟️ Confrontos analisados: {stats['unique']}",
        f"📚 Odds válidas: {stats['priced']}/{stats['records']}",
        f"📈 Melhor margem estimada: {best}",
        f"🔎 Sinais potenciais: {potentials}",
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


def send_summary(conn, catalog_count, startup=False, now_epoch=None, paper_conn=None):
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
            f"{totals['settled']} liquidadas"
            f"\n💰 Polymarket: {paper.eur(int(round(totals['poly_free_eur'] * 100)))} livres"
            f"\n💰 SX Bet: {paper.eur(int(round(totals['sx_free_eur'] * 100)))} livres"
            f"\n🔒 Em operações abertas: {paper.eur(int(round(totals['exposure_eur'] * 100)))}"
            f"\n📈 Resultado realizado: {paper.eur(int(round(totals['realized_eur'] * 100)))}"
            "\n⚠️ Saldos virtuais"
        )
    telegram(message)
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


def catalog():
    poly = bot.discover_poly()
    sx = bot.discover_sx()
    pairs = select_pairs(bot.pair_markets(poly, sx))
    stats = dict(Counter(p["sx"]["sport"] for p in pairs))
    LOG.info(
        "CATALOGO | Poly=%d SX=%d direções=%d | desportos=%s",
        len(poly), len(sx), len(pairs), stats,
    )
    return pairs


def self_test():
    import tempfile

    # Fila com prioridade sem bloquear rotação REST e sem preços em cache.
    ws_priority.self_test()

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
    fx = float(os.getenv("EUR_PER_USD", "0") or "0")
    if fx < 0 or fx > 10:
        raise SystemExit("EUR_PER_USD inválido. Usa euros por 1 USD, por exemplo cotação atual.")
    bot.FX = fx

    if fx == 0:
        LOG.warning(
            "EUR_PER_USD ausente: cálculo de stake de 100 EUR e alertas "
            "de oportunidades desativados; só se guarda o histórico."
        )

    conn = open_database(DB)
    priority_cache = ws_priority.PriorityCache(
        max_priority=WS_PRIORITY_MAX, ttl_seconds=WS_EVENT_TTL
    )
    ws_feed = sx_hints.SXBookHints() if SX_WS_V12_ENABLED else None
    if ws_feed is not None and not ws_feed.start():
        LOG.warning("SX WS V12 solicitado mas sem SX_API_KEY: usar apenas REST.")
        ws_feed = None
    poly_feed = poly_hints.PolyBookHints() if POLY_WS_V12_ENABLED else None
    if poly_feed is not None:
        poly_feed.start()
    paper_conn = paper.open_database() if PAPER_V12_ENABLED and fx > 0 else None
    if PAPER_V12_ENABLED and paper_conn is None:
        LOG.warning("PAPER V12 pediu ativação, mas EUR_PER_USD inválido/ausente: desativado.")
    if paper_conn is not None:
        LOG.info("PAPER V12 ATIVO: ROI mínimo %.2f%% após custos simulados; "
                 "1000 EUR virtuais por plataforma, "
                 "máximo 100 EUR total por operação, sem apostas reais.",
                 100 * paper.MIN_NET_ROI)
    LOG.info(
        "WS PRIORITY: janela %.1fs; direções/passo=%d; "
        "prioridade máx=%d; cache eventos=%.0fs (SEM cache de odds); "
        "REST valida ambos os livros. Sem apostas reais.",
        WS_REACT_GAP_SECONDS, MAX_PAIRS,
        WS_PRIORITY_MAX, WS_EVENT_TTL,
    )
    pairs, cursor = [], 0
    last_catalog = 0.0
    iteration = 0
    try:
        while True:
            started = time.monotonic()
            if last_catalog == 0.0 or started - last_catalog >= CATALOG_INTERVAL:
                try:
                    pairs = catalog()
                    cursor = cursor % len(pairs) if pairs else 0
                    priority_cache.rebuild(pairs)
                    if ws_feed is not None:
                        ws_feed.update(p["sx"]["hash"] for p in pairs[:24])
                    if poly_feed is not None:
                        poly_feed.update(p["poly_asset"] for p in pairs[:24])
                    last_catalog = time.monotonic()
                except Exception as exc:
                    LOG.error("Erro a atualizar catálogo: %s", type(exc).__name__)
                    last_catalog = time.monotonic()
                    if once:
                        raise

            sx_events = ws_feed.take_events() if ws_feed is not None else {}
            poly_events = poly_feed.take_events() if poly_feed is not None else {}
            priority_cache.ingest(poly_events, sx_events)
            result = scan_pass(
                conn, pairs, cursor, fx, paper_conn=paper_conn,
                priority_cache=priority_cache
            )
            cursor = result["next_offset"]
            outage_notice = update_health(conn, result["checked"], result["priced"])
            if outage_notice:
                LOG.warning("Sem odds válidas durante o período de alerta.")
                telegram(outage_notice)
            iteration += 1

            if once or report_due(conn):
                send_summary(conn, len(pairs), startup=(iteration == 1),
                             paper_conn=paper_conn)

            # Preservar o histórico do último mês; não apagar resultados recentes.
            if iteration % 100 == 0:
                cutoff = datetime.fromtimestamp(
                    time.time() - 30 * 86400, tz=timezone.utc
                ).isoformat(timespec="seconds")
                with conn:
                    conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))

            if once:
                print("✅ Passagem única concluída; só leitura, sem apostas.")
                break
            wait_seconds = max(1.0, INTERVAL - (time.monotonic() - started))
            if ws_feed is None and poly_feed is None:
                time.sleep(wait_seconds)
            else:
                # Polling de eventos de ambas as fontes; REST continua obrigatório.
                deadline = time.monotonic() + wait_seconds
                wake_received = False
                while time.monotonic() < deadline:
                    if ((ws_feed is not None and ws_feed.wake.is_set())
                            or (poly_feed is not None and poly_feed.wake.is_set())):
                        wake_received = True
                        break
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        break
                    # Antes dormia 1s e forçava 8s mínimos mesmo após WS.
                    # Agora: reação em ~100ms (mais a duração das APIs REST),
                    # com um mínimo de 2s entre scans acionados pelo WS.
                    time.sleep(min(0.1, remaining))
                elapsed = time.monotonic() - started
                pause = websocket_reaction_pause(elapsed, wake_received)
                if pause > 0:
                    time.sleep(pause)
    except KeyboardInterrupt:
        LOG.info("Monitor interrompido.")
    finally:
        if ws_feed is not None:
            ws_feed.stop()
        if poly_feed is not None:
            poly_feed.stop()
        conn.close()
        if paper_conn is not None:
            paper_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        main(once=args.once)
