"""PAPER V12 — ledger de arbitragem simulada Polymarket + SX Bet.

SOMENTE paper trading. Não contém chaves de carteira nem endpoints de apostas.
O lucro é CONDICIONAL a ambos os mercados serem liquidados normalmente e a
ambas as pernas hipoteticamente serem preenchidas. Não é lucro garantido.

Usar com monitor_v11.py; teste: python paper_v12.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

INITIAL_CENTS = 100_000                 # 1.000 EUR em cada plataforma
MAX_TRADE_CENTS = 10_000                # 100 EUR TOTAL, incluindo custos
MAX_OPEN_EXPOSURE_CENTS = 50_000        # máximo 500 EUR expostos em conjunto
MIN_TRADE_CENTS = 500                  # mínimo de 5 EUR por operação
MIN_NET_ROI = 0.015                    # 1,5%, antes de considerar risco de liquidação
POLY_TAKER_FEE_RATE = 0.05             # parâmetro desportivo indicado no help center
ADVERSE_PRICE_BUFFER = 0.0125          # 1,25% por perna, reserva NÃO oficial
MAX_SNAPSHOT_GAP_SECONDS = 4.0
PAPER_DB = os.getenv("PAPER_V12_DB", "/data/paper_v12.sqlite3")
ALLOWED_SPORTS = {"tennis", "basketball", "american_football",
                  "baseball", "cricket", "esports", "combat"}
# Observação: elegibilidade em paper não prova equivalência de liquidação.
# Live pode ser analisado, mas só é simulado com adesão explícita.
ALLOW_LIVE_UNVERIFIED = os.getenv("PAPER_V12_LIVE_UNVERIFIED", "0") == "1"


def cents_up(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_CEILING))


def cents_down(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_FLOOR))


def walk_book(levels, payout):
    """Custo e distribuição por níveis para um mesmo payout em USD/USDC.

    Ambos os livros têm formato (preço/probabilidade, capacidade em payout).
    O livro SX já deve estar convertido do referencial maker para taker.
    """
    if payout <= 0 or not math.isfinite(payout):
        return None
    remaining, cost, poly_fee_units = payout, 0.0, 0.0
    for price, capacity in sorted(levels, key=lambda row: row[0]):
        price, capacity = float(price), float(capacity)
        if not (math.isfinite(price) and math.isfinite(capacity) and
                0 < price < 1 and capacity > 0):
            continue
        size = min(remaining, capacity)
        cost += size * price
        poly_fee_units += size * POLY_TAKER_FEE_RATE * price * (1 - price)
        remaining -= size
        if remaining <= 1e-7:
            return cost, poly_fee_units
    return None


@dataclass(frozen=True)
class ShadowQuote:
    payout_usdc: float
    fx_eur_per_usd: float
    poly_debit_cents: int
    sx_debit_cents: int
    poly_fee_cents: int
    total_cents: int
    payout_eur_cents: int
    conditional_profit_cents: int
    roi: float

    @property
    def total_eur(self):
        return self.total_cents / 100

    @property
    def profit_eur(self):
        return self.conditional_profit_cents / 100


def quote_for_payout(poly_levels, sx_levels, payout_usdc, fx):
    """Inclui fee de taker Poly, buffers, arredondamento a cêntimos."""
    if not (math.isfinite(fx) and 0 < fx < 10):
        return None
    poly = walk_book(poly_levels, payout_usdc)
    sx = walk_book(sx_levels, payout_usdc)
    if poly is None or sx is None:
        return None
    poly_stake, poly_fee = poly
    sx_stake, _ = sx
    # Reserva adversa independente em cada leg. Não simula a execução real.
    poly_debit = cents_up((poly_stake * (1 + ADVERSE_PRICE_BUFFER) + poly_fee) * fx)
    sx_debit = cents_up(sx_stake * (1 + ADVERSE_PRICE_BUFFER) * fx)
    fee_cents = cents_up(poly_fee * fx)
    total = poly_debit + sx_debit
    payout_cents = cents_down(payout_usdc * fx)
    if total <= 0:
        return None
    profit = payout_cents - total
    return ShadowQuote(
        payout_usdc, fx, poly_debit, sx_debit, fee_cents, total,
        payout_cents, profit, profit / total
    )


def select_shadow_quote(poly_levels, sx_levels, fx,
                        free_poly_cents, free_sx_cents):
    """Procura stake comum, respeitando profundidade, carteiras e 100 EUR."""
    if not (poly_levels and sx_levels and 0 < fx < 10):
        return None
    for book in (poly_levels, sx_levels):
        for price, capacity in book:
            if not (math.isfinite(price) and math.isfinite(capacity)
                    and 0 < price < 1 and capacity > 0):
                return None

    capacity = min(
        sum(cap for _, cap in poly_levels),
        sum(cap for _, cap in sx_levels),
        1_000.0 / fx,
    )
    if capacity <= 0:
        return None

    low, high = 0.0, capacity
    for _ in range(45):
        middle = (low + high) / 2
        quote = quote_for_payout(poly_levels, sx_levels, middle, fx)
        allowed = (
            quote is not None
            and quote.total_cents <= MAX_TRADE_CENTS
            and quote.poly_debit_cents <= free_poly_cents
            and quote.sx_debit_cents <= free_sx_cents
        )
        if allowed:
            low = middle
        else:
            high = middle

    # Reduz para 6 decimais e recalcula depois do arredondamento.
    payout = math.floor(low * 1_000_000) / 1_000_000
    quote = quote_for_payout(poly_levels, sx_levels, payout, fx)
    if quote is None:
        return None
    if not (MIN_TRADE_CENTS <= quote.total_cents <= MAX_TRADE_CENTS):
        return None
    if quote.poly_debit_cents > free_poly_cents or quote.sx_debit_cents > free_sx_cents:
        return None
    if quote.roi < MIN_NET_ROI or quote.conditional_profit_cents <= 0:
        return None
    return quote


def open_database(path=PAPER_DB):
    parent = Path(path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS paper_v12_wallets(
        platform TEXT PRIMARY KEY, free_cents INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_v12_trades(
        id INTEGER PRIMARY KEY,
        event_key TEXT UNIQUE NOT NULL,
        created_epoch INTEGER NOT NULL,
        sport TEXT NOT NULL,
        event TEXT NOT NULL,
        poly_market TEXT NOT NULL,
        sx_market TEXT NOT NULL,
        poly_side TEXT NOT NULL,
        sx_side TEXT NOT NULL,
        stage TEXT NOT NULL,
        payout_usdc REAL NOT NULL,
        fx_eur_per_usd REAL NOT NULL,
        poly_debit_cents INTEGER NOT NULL,
        sx_debit_cents INTEGER NOT NULL,
        poly_fee_cents INTEGER NOT NULL,
        conditional_payout_cents INTEGER NOT NULL,
        conditional_profit_cents INTEGER NOT NULL,
        status TEXT NOT NULL,
        settled_side TEXT,
        realized_profit_cents INTEGER,
        note TEXT NOT NULL
      );
    """)
    for name in ("poly", "sx"):
        conn.execute(
            "INSERT OR IGNORE INTO paper_v12_wallets(platform, free_cents) VALUES (?,?)",
            (name, INITIAL_CENTS),
        )
    conn.commit()
    return conn


def free_balances(conn):
    return dict(conn.execute(
        "SELECT platform, free_cents FROM paper_v12_wallets"
    ).fetchall())


def event_key(pair):
    poly_id = str(pair["poly"]["id"])
    sx_hash = str(pair["sx"]["hash"])
    return hashlib.sha256(f"{poly_id}|{sx_hash}".encode()).hexdigest()


def open_exposure(conn):
    row = conn.execute("""
        SELECT COALESCE(SUM(poly_debit_cents + sx_debit_cents),0)
        FROM paper_v12_trades WHERE status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED')
    """).fetchone()
    return int(row[0])


def book_shadow_trade(conn, pair, quote, stage, now_epoch=None):
    """Duas pernas virtuais numa única transação SQLite. Nunca faz apostas.

    Uma única simulação por par de mercados, mesmo que a direção mude.
    Não marca como REAL_EXECUTED nem como garantia de arbitragem.
    """
    if stage not in {"PRE_JOGO", "LIVE_NAO_CONFIRMADO"}:
        return None
    if stage == "LIVE_NAO_CONFIRMADO" and not ALLOW_LIVE_UNVERIFIED:
        return None
    if pair["sx"]["sport"] not in ALLOWED_SPORTS:
        return None
    if str(pair["poly"].get("type") or "").lower() != "moneyline":
        return None
    # Uma diferença grande de horário é indício de mercado diferente.
    if abs(float(pair["poly"].get("start", 0)) -
           float(pair["sx"].get("start", 0))) > 1800:
        return None
    if pair["poly"].get("sport") != pair["sx"].get("sport"):
        return None
    if str(pair["poly_label"]).strip().lower() == str(pair["sx_label"]).strip().lower():
        return None
    if quote.total_cents > MAX_TRADE_CENTS or quote.roi < MIN_NET_ROI:
        return None

    eid = event_key(pair)
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM paper_v12_trades WHERE event_key=?", (eid,)).fetchone():
            conn.rollback()
            return None
        free = free_balances(conn)
        if free["poly"] < quote.poly_debit_cents or free["sx"] < quote.sx_debit_cents:
            conn.rollback()
            return None
        if open_exposure(conn) + quote.total_cents > MAX_OPEN_EXPOSURE_CENTS:
            conn.rollback()
            return None

        conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents-? WHERE platform='poly'",
                     (quote.poly_debit_cents,))
        conn.execute("UPDATE paper_v12_wallets SET free_cents=free_cents-? WHERE platform='sx'",
                     (quote.sx_debit_cents,))
        result = conn.execute("""
          INSERT INTO paper_v12_trades(
            event_key, created_epoch, sport, event, poly_market, sx_market,
            poly_side, sx_side, stage, payout_usdc, fx_eur_per_usd,
            poly_debit_cents, sx_debit_cents, poly_fee_cents,
            conditional_payout_cents, conditional_profit_cents, status, note
          ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN_CONDITIONAL',?)
        """, (
            eid, now_epoch, pair["sx"]["sport"],
            " vs ".join(pair["sx"]["teams"]), str(pair["poly"]["id"]),
            str(pair["sx"]["hash"]), pair["poly_label"], pair["sx_label"],
            stage, quote.payout_usdc, quote.fx_eur_per_usd,
            quote.poly_debit_cents, quote.sx_debit_cents,
            quote.poly_fee_cents, quote.payout_eur_cents,
            quote.conditional_profit_cents,
            "Só simulação; fills presumidos. Lucro apenas se ambos os "
            "mercados resolverem normalmente; exceções exigem revisão.",
        ))
        trade_id = result.lastrowid
        conn.commit()
        return trade_id
    except Exception:
        conn.rollback()
        raise


def paper_summary(conn):
    free = free_balances(conn)
    open_count, settled, reviews = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN status='OPEN_CONDITIONAL' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN status='REVIEW_REQUIRED' THEN 1 ELSE 0 END),0)
        FROM paper_v12_trades
    """).fetchone()
    realized = conn.execute("""
        SELECT COALESCE(SUM(realized_profit_cents),0)
        FROM paper_v12_trades WHERE status='SETTLED'
    """).fetchone()[0]
    return {
        "poly_free_eur": free["poly"] / 100,
        "sx_free_eur": free["sx"] / 100,
        "open_trades": open_count,
        "settled": settled,
        "requires_review": reviews,
        "exposure_eur": open_exposure(conn) / 100,
        "realized_eur": realized / 100,
    }


def settle_normal(conn, trade_id, winning_platform, verified=False):
    """Liquidação manual após resultado E regras verificados nas duas fontes."""
    if not verified:
        raise ValueError("É obrigatório confirmar as regras e o resultado em ambas as plataformas")
    if winning_platform not in ("poly", "sx"):
        raise ValueError("Vencedor deve ser 'poly' ou 'sx'. Exceções exigem revisão.")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
          SELECT status, conditional_payout_cents, poly_debit_cents, sx_debit_cents
          FROM paper_v12_trades WHERE id=?
        """, (trade_id,)).fetchone()
        if not row or row[0] != "OPEN_CONDITIONAL":
            raise ValueError("Operação inexistente, bloqueada para revisão ou já liquidada")
        _, payout, poly_debit, sx_debit = row
        net = payout - poly_debit - sx_debit
        conn.execute(
            "UPDATE paper_v12_wallets SET free_cents=free_cents+? WHERE platform=?",
            (payout, winning_platform),
        )
        conn.execute("""
          UPDATE paper_v12_trades
          SET status='SETTLED', settled_side=?, realized_profit_cents=?
          WHERE id=?
        """, (winning_platform, net, trade_id))
        conn.commit()
        return net / 100
    except Exception:
        conn.rollback()
        raise


def flag_exception(conn, trade_id, reason):
    """Suspender a liquidação quando há desistência, anulação, adiamento, etc."""
    reason = str(reason).strip()
    if len(reason) < 5:
        raise ValueError("Motivo de revisão obrigatório")
    with conn:
        cursor = conn.execute("""
          UPDATE paper_v12_trades
          SET status='REVIEW_REQUIRED', note=?
          WHERE id=? AND status='OPEN_CONDITIONAL'
        """, (reason[:500], trade_id))
    if cursor.rowcount != 1:
        raise ValueError("Operação não aberta ou inexistente")


def try_paper_entry(conn, pair, poly_levels, sx_levels, fx, stage, skew):
    """Um candidato de paper: NÃO chama nenhuma API de escrita."""
    if skew > MAX_SNAPSHOT_GAP_SECONDS:
        return None
    if pair["sx"]["sport"] not in ALLOWED_SPORTS:
        return None
    if stage not in ("PRE_JOGO", "LIVE_NAO_CONFIRMADO"):
        return None
    if stage == "LIVE_NAO_CONFIRMADO" and not ALLOW_LIVE_UNVERIFIED:
        return None
    if fx <= 0 or fx >= 10:
        return None
    eid = event_key(pair)
    if conn.execute("SELECT 1 FROM paper_v12_trades WHERE event_key=?", (eid,)).fetchone():
        return None
    free = free_balances(conn)
    quote = select_shadow_quote(poly_levels, sx_levels, fx, free["poly"], free["sx"])
    if not quote:
        return None
    trade_id = book_shadow_trade(conn, pair, quote, stage)
    return (trade_id, quote) if trade_id is not None else None


def self_test():
    import tempfile

    q = select_shadow_quote([(0.43, 300)], [(0.50, 300)], 0.91, 100_000, 100_000)
    assert q and q.total_cents <= 10_000 and q.profit_eur > 0
    assert q.total_eur >= 5 and q.roi >= MIN_NET_ROI
    assert select_shadow_quote([(0.55, 300)], [(0.55, 300)], 0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 0.1)], [(0.50, 300)], 0.91, 100_000, 100_000) is None
    assert quote_for_payout([(0.43, 10)], [(0.50, 10)], 10, 0.91).poly_fee_cents > 0

    sample = {
        "poly": {"id": "poly-1", "type": "moneyline",
                 "sport": "basketball", "start": 1800000000},
        "sx": {"hash": "sx-1", "sport": "basketball",
               "teams": ("Lakers", "Celtics"), "start": 1800000000},
        "poly_label": "Lakers", "sx_label": "Celtics",
    }
    with tempfile.TemporaryDirectory() as temp:
        db = open_database(str(Path(temp) / "paper.sqlite3"))
        ident = book_shadow_trade(db, sample, q, "PRE_JOGO", now_epoch=1000)
        assert ident == 1
        assert book_shadow_trade(db, sample, q, "PRE_JOGO") is None
        # Duplicação, modalidade diferente, horário e tipo de mercado
        # nunca podem ser admitidos.
        bad = {**sample, "poly": {**sample["poly"], "type": "spread"}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        bad = {**sample, "poly": {**sample["poly"], "sport": "tennis"}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        bad = {**sample, "poly": {**sample["poly"], "start": 1800004000}}
        assert book_shadow_trade(db, bad, q, "PRE_JOGO") is None
        balances = free_balances(db)
        assert balances["poly"] == INITIAL_CENTS - q.poly_debit_cents
        assert balances["sx"] == INITIAL_CENTS - q.sx_debit_cents
        assert paper_summary(db)["open_trades"] == 1
        try:
            settle_normal(db, ident, "poly")
        except ValueError:
            pass
        else:
            raise AssertionError("Liquidação sem confirmação devia falhar")
        realized = settle_normal(db, ident, "poly", verified=True)
        assert round(realized * 100) == q.conditional_profit_cents
        assert paper_summary(db)["open_trades"] == 0
        assert paper_summary(db)["settled"] == 1
        assert paper_summary(db)["realized_eur"] == realized
        assert db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0] == 1
        db.close()
    print("✅ PAPER V12 | matemática, carteiras, limite 100€, duplicados e liquidação OK")


def cli():
    parser = argparse.ArgumentParser(description="Shadow Paper V12 (sem apostas reais)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--settle-id", type=int)
    parser.add_argument("--winner", choices=["poly", "sx"])
    parser.add_argument("--verified-both-rules", action="store_true")
    parser.add_argument("--flag-exception", type=int)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    conn = open_database()
    try:
        if args.settle_id is not None:
            gain = settle_normal(
                conn, args.settle_id, args.winner,
                verified=args.verified_both_rules
            )
            print(f"Liquidação condicional #{args.settle_id}: {gain:+.2f} €")
        elif args.flag_exception is not None:
            flag_exception(conn, args.flag_exception, args.reason or "")
            print(f"Operação #{args.flag_exception} bloqueada para revisão")
        else:
            print(paper_summary(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
