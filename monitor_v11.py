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

LOG = logging.getLogger("monitor_v11")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

DB = os.getenv("MONITOR_DB", "monitor_v11.sqlite3")
INTERVAL = max(10, int(os.getenv("MONITOR_INTERVAL_SECONDS", "20")))
CATALOG_INTERVAL = max(180, int(os.getenv("MONITOR_CATALOG_SECONDS", "600")))
MAX_PAIRS = min(60, max(1, int(os.getenv("MONITOR_PAIRS_PER_PASS", "15"))))
ALERT_COOLDOWN = max(300, int(os.getenv("MONITOR_ALERT_COOLDOWN_SECONDS", "1800")))
SUMMARY_INTERVAL = max(3600, int(os.getenv("MONITOR_SUMMARY_SECONDS", "21600")))
MAX_SKEW = 6.0
MIN_ROI = bot.MIN_NET_ROI
PAPER_V12_ENABLED = os.getenv("PAPER_V12_ENABLED", "0").strip().lower() in {"1", "true", "yes"}

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
    buffered_cost = pc * (1.0 + bot.POLY_FEE_BUFFER) + sc * (1.0 + bot.SX_FEE_BUFFER)
    buffered = 100.0 * (capacity - buffered_cost) / buffered_cost
    return capacity, gross, buffered


def hypothetical_100_eur(poly_levels, sx_levels, fx: float):
    """Tamanho máximo de 100 € total usando o modelo prudente existente.

    Uma cotação positiva NÃO constitui autorização para abrir posições.
    """
    if fx <= 0:
        return None
    bot.FX = fx
    return bot.price_quote(
        poly_levels, sx_levels,
        bot.INITIAL_EUR / fx, bot.INITIAL_EUR / fx,
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
            (hypothetical.poly_usd + hypothetical.sx_usd
             + hypothetical.fees_usd) * fx if hypothetical else None,
            hypothetical.profit_usd * fx if hypothetical else None,
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
    total = (hypothetical.poly_usd + hypothetical.sx_usd
             + hypothetical.fees_usd) * bot.FX
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


def scan_pass(conn, pairs, offset: int, fx: float, paper_conn=None):
    if not pairs:
        return {"checked": 0, "priced": 0, "positive": 0, "signals": 0,
                "next_offset": 0, "best": None}
    total = len(pairs)
    window = [pairs[(offset + n) % total] for n in range(min(MAX_PAIRS, total))]
    new_offset = (offset + len(window)) % total
    checked = priced = positive = signals = 0
    best = None
    now = time.time()

    for pair in window:
        sx = pair["sx"]
        start = sx["start"]
        if not (now - bot.MAX_LIVE_AGE_H * 3600 <= start
                <= now + bot.MAX_FUTURE_H * 3600):
            continue
        # Início ≠ jogo oficialmente ao vivo.
        if start <= now and not sx["live_enabled"]:
            continue
        checked += 1
        try:
            poly_levels, t_poly = bot.poly_book(pair["poly_asset"])
            sx_levels, t_sx = bot.sx_book(sx["hash"], pair["sx_target"])
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
                if maybe_alert(conn, pair, quote, hypothetical, int(time.time())):
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
                        telegram(
                            f"🧪 PAPER V12 #{shadow_id} · NÃO REAL\n"
                            f"{sx['sport']} | {sx['teams'][0]} vs {sx['teams'][1]}\n"
                            f"Poly: {pair['poly_label']} {shadow_quote.poly_debit_cents/100:.2f}€\n"
                            f"SX: {pair['sx_label']} {shadow_quote.sx_debit_cents/100:.2f}€\n"
                            f"Total: {shadow_quote.total_eur:.2f}€\n"
                            f"Lucro condicional: +{shadow_quote.profit_eur:.2f}€\n"
                            "⚠️ Fills presumidos · liquidação por validar"
                        )
        except Exception as exc:
            LOG.warning(
                "Book falhou em %s (%s)",
                pair["key"][:35], type(exc).__name__,
            )
    LOG.info(
        "SCAN | direções=%d | preços válidos=%d | margens positivas=%d "
        "| sinais=%d | melhor buffer=%s",
        checked, priced, positive, signals,
        f"{best:+.2f}%" if best is not None else "N/D",
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
            f"\n🧾 Paper: {totals['open_trades']} abertas · "
            f"{totals['settled']} liquidadas"
            f"\n💰 Livre: Poly {totals['poly_free_eur']:.2f}€ | "
            f"SX {totals['sx_free_eur']:.2f}€"
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
    print("✅ Monitor V11: testes locais OK (sem APIs, sem apostas)")


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
    paper_conn = paper.open_database() if PAPER_V12_ENABLED and fx > 0 else None
    if PAPER_V12_ENABLED and paper_conn is None:
        LOG.warning("PAPER V12 pediu ativação, mas EUR_PER_USD inválido/ausente: desativado.")
    if paper_conn is not None:
        LOG.info("PAPER V12 ATIVO: 1000 EUR virtuais por plataforma, "
                 "máximo 100 EUR total por operação, sem apostas reais.")
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
                    last_catalog = time.monotonic()
                except Exception as exc:
                    LOG.error("Erro a atualizar catálogo: %s", type(exc).__name__)
                    last_catalog = time.monotonic()
                    if once:
                        raise

            result = scan_pass(conn, pairs, cursor, fx, paper_conn=paper_conn)
            cursor = result["next_offset"]
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
            time.sleep(max(1.0, INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        LOG.info("Monitor interrompido.")
    finally:
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
