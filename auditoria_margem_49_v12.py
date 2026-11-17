"""Auditoria somente leitura da margem e operações fictícias do V12.

Cruza observações do monitor com o ledger paper. Não importa o monitor,
não acede às APIs, não realiza, liquida ou simula novas apostas.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def open_ro(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        print(f"⚠️ Base de dados não encontrada: {p}")
        return None
    # Modo ro: não criar ficheiros nem modificar WAL/journal do monitor.
    try:
        conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        print(f"⚠️ Não foi possível abrir {p.name}: {type(exc).__name__}")
        return None


def rows(conn, query, params=()):
    if conn is None:
        return []
    try:
        return conn.execute(query, params).fetchall()
    except sqlite3.Error as exc:
        print(f"⚠️ Consulta indisponível: {type(exc).__name__}")
        return []


def amount(cents):
    return f"{int(cents or 0) / 100:,.2f} €"


def audit(monitor_conn, paper_conn, limit=5):
    print("🔍 AUDITORIA 49% — APENAS LEITURA")
    trades = rows(paper_conn, """
        SELECT id, created_epoch, sport, event, poly_market, sx_market,
               poly_side, sx_side, stage, payout_usdc, fx_eur_per_usd,
               poly_debit_cents, sx_debit_cents, conditional_payout_cents,
               conditional_profit_cents, status, note
        FROM paper_v12_trades ORDER BY created_epoch DESC LIMIT ?
    """, (limit,))
    print(f"\n🧪 Operações virtuais recentes: {len(trades)}")
    for t in trades:
        total = t["poly_debit_cents"] + t["sx_debit_cents"]
        roi = (100 * t["conditional_profit_cents"] / total) if total else 0
        stamp = datetime.fromtimestamp(
            t["created_epoch"], timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(
            f"\n#{t['id']} | {stamp} | {t['status']} | {t['stage']}"
            f"\nJOGO: {t['event']} ({t['sport']})"
            f"\nPOLY: {t['poly_side']} | {amount(t['poly_debit_cents'])}"
            f" | mercado={t['poly_market']}"
            f"\nSX:   {t['sx_side']} | {amount(t['sx_debit_cents'])}"
            f" | mercado={t['sx_market']}"
            f"\nTOTAL: {amount(total)} | pagamento condicional: "
            f"{amount(t['conditional_payout_cents'])}"
            f"\nROI REGISTADO: {roi:+.2f}% | lucro condicional: "
            f"{amount(t['conditional_profit_cents'])}"
            "\n⚠️ Não prova que ambos os mercados liquidam de forma equivalente."
        )
        matching = rows(monitor_conn, """
            SELECT observed_at, event, status, poly_outcome, sx_outcome,
                   best_poly_price, best_sx_price, gross_roi_pct,
                   buffered_roi_pct, snapshot_gap_seconds,
                   hypothetical_total_eur, hypothetical_profit_eur
            FROM observations
            WHERE poly_market=? AND sx_market=?
            ORDER BY ABS(strftime('%s', observed_at) - ?) ASC, id ASC
            LIMIT 1
        """, (t["poly_market"], t["sx_market"], t["created_epoch"]))
        if matching:
            o = matching[0]
            print(
                f"📒 Observação próxima: {o['observed_at']} | {o['status']}"
                f"\nLados observados: Poly={o['poly_outcome']}"
                f" | SX={o['sx_outcome']}"
                f"\nPreços no livro (fração de 1): "
                f"Poly={o['best_poly_price']} | SX={o['best_sx_price']}"
                f"\nROI do monitor: {o['buffered_roi_pct']:+.2f}%"
                if o["buffered_roi_pct"] is not None else
                "📒 Observação disponível, mas sem margem válida."
            )
        else:
            print("📒 Sem observação correspondente no histórico do monitor.")

    top = rows(monitor_conn, """
        SELECT observed_at, event, sport, status,
               poly_market, sx_market, poly_outcome, sx_outcome,
               best_poly_price, best_sx_price, common_payout_usd,
               gross_roi_pct, buffered_roi_pct, snapshot_gap_seconds,
               hypothetical_total_eur, hypothetical_profit_eur, note
        FROM observations
        WHERE buffered_roi_pct IS NOT NULL
        ORDER BY buffered_roi_pct DESC, id DESC LIMIT ?
    """, (limit,))
    print(f"\n📈 Maiores margens HISTÓRICAS registadas: {len(top)}")
    for i, o in enumerate(top, 1):
        margin = float(o["buffered_roi_pct"])
        print(
            f"\n{i}. {margin:+.2f}% | {o['observed_at']}"
            f"\nJOGO: {o['event']} | desporto={o['sport']}"
            f" | fase={o['status']}"
            f"\nPoly: {o['poly_outcome']} | preço={o['best_poly_price']}"
            f" | mercado={o['poly_market']}"
            f"\nSX:   {o['sx_outcome']} | preço={o['best_sx_price']}"
            f" | mercado={o['sx_market']}"
            f"\nSkew snapshots: {o['snapshot_gap_seconds']}s"
            f" | ROI bruto={o['gross_roi_pct']}%"
            f"\nStake hipotética={o['hypothetical_total_eur']} EUR"
            f" | lucro hipotético={o['hypothetical_profit_eur']} EUR"
            f"\nNota: {o['note']}"
        )
    if not trades and not top:
        print("\n⚠️ Sem dados disponíveis. Verifica MONITOR_DB e PAPER_V12_DB.")
    print("\n🔒 Sem modificações às bases, carteiras ou apostas.")


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        m = sqlite3.connect(Path(td) / "m.sqlite3")
        p = sqlite3.connect(Path(td) / "p.sqlite3")
        m.execute("""CREATE TABLE observations(
            id INTEGER PRIMARY KEY, observed_at TEXT, event TEXT, sport TEXT,
            status TEXT, poly_market TEXT, sx_market TEXT, poly_outcome TEXT,
            sx_outcome TEXT, best_poly_price REAL, best_sx_price REAL,
            common_payout_usd REAL, gross_roi_pct REAL, buffered_roi_pct REAL,
            snapshot_gap_seconds REAL, hypothetical_total_eur REAL,
            hypothetical_profit_eur REAL, note TEXT
        )""")
        p.execute("""CREATE TABLE paper_v12_trades(
            id INTEGER, created_epoch INTEGER, sport TEXT, event TEXT,
            poly_market TEXT, sx_market TEXT, poly_side TEXT, sx_side TEXT,
            stage TEXT, payout_usdc REAL, fx_eur_per_usd REAL,
            poly_debit_cents INTEGER, sx_debit_cents INTEGER,
            conditional_payout_cents INTEGER,
            conditional_profit_cents INTEGER, status TEXT, note TEXT
        )""")
        m.execute("""INSERT INTO observations VALUES(
           1,'2026-11-17T12:00:00+00:00','A vs B','tennis','PRE_JOGO',
           'pm','sm','A','B',0.3,0.4,10,50,49.18,0.2,100,49.18,'amostra')""")
        p.execute("""INSERT INTO paper_v12_trades VALUES(
           1,1794916800,'tennis','A vs B','pm','sm','A','B','PRE_JOGO',
           10,0.91,7812,2188,10500,500,'OPEN_CONDITIONAL','amostra')""")
        m.commit(); p.commit(); m.close(); p.close()
        ro_m = open_ro(Path(td) / "m.sqlite3")
        ro_p = open_ro(Path(td) / "p.sqlite3")
        assert len(rows(ro_m, "SELECT * FROM observations")) == 1
        assert len(rows(ro_p, "SELECT * FROM paper_v12_trades")) == 1
        audit(ro_m, ro_p, 3)
        ro_m.close(); ro_p.close()
    print("✅ Auditoria 49%: teste de leitura e associação dos mercados OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    m = open_ro(os.getenv("MONITOR_DB", "monitor_v11.sqlite3"))
    p = open_ro(os.getenv("PAPER_V12_DB", "/data/paper_v12.sqlite3"))
    try:
        audit(m, p, min(20, max(1, args.limit)))
    finally:
        if m:
            m.close()
        if p:
            p.close()


if __name__ == "__main__":
    main()
