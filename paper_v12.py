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
# Percentagem para papel: PAPER_V12_MIN_NET_ROI_PCT=0.5 significa 0,5%.
# A configuração não ativa ordens REAIS; só muda o limiar de registo virtual.
def read_min_roi():
    raw = os.getenv("PAPER_V12_MIN_NET_ROI_PCT", "1.5").strip().replace(",", ".")
    try:
        pct = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("PAPER_V12_MIN_NET_ROI_PCT deve ser uma percentagem, ex. 0.5") from exc
    if not (math.isfinite(pct) and 0.5 <= pct <= 15.0):
        raise ValueError("PAPER_V12_MIN_NET_ROI_PCT deve estar entre 0.5 e 15.0")
    return pct / 100.0


MIN_NET_ROI = read_min_roi()          # padrão 1,5%; piloto 0,5% no Railway
POLY_TAKER_FEE_RATE = 0.05             # parâmetro desportivo indicado no help center
ADVERSE_PRICE_BUFFER = 0.0125          # 1,25% por perna, reserva NÃO oficial
MAX_SNAPSHOT_GAP_SECONDS = 4.0
# Bloquear novas operações paper com margens invulgares, sujeitas a auditoria.
# Não altera nem liquida operações já registadas.
MAX_AUTO_PAPER_ROI = 0.10

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
    if (quote.roi < MIN_NET_ROI or quote.roi >= MAX_AUTO_PAPER_ROI
            or quote.conditional_profit_cents <= 0):
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
      CREATE TABLE IF NOT EXISTS paper_v12_notifications(
        trade_id INTEGER PRIMARY KEY,
        sent_epoch INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS paper_v12_open_notifications(
        trade_id INTEGER PRIMARY KEY,
        message TEXT NOT NULL,
        sent_epoch INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_epoch INTEGER
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


def book_shadow_trade(conn, pair, quote, stage, now_epoch=None,
                      opening_odds=None):
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
    if (quote.total_cents > MAX_TRADE_CENTS
            or quote.roi < MIN_NET_ROI
            or not math.isfinite(quote.roi)
            or quote.roi >= MAX_AUTO_PAPER_ROI):
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
        if opening_odds is not None:
            # A mensagem é persistida NA MESMA transação que os dois débitos
            # fictícios. Se o processo terminar antes de enviar ao Telegram,
            # outro ciclo pode enviá-la sem registar nova operação.
            odds_poly, odds_sx = opening_odds
            frozen_message = format_open_message(
                conn, trade_id, odds_poly=odds_poly, odds_sx=odds_sx,
                game_start=pair["sx"].get("start"),
            )
            conn.execute(
                "INSERT INTO paper_v12_open_notifications(trade_id, message) "
                "VALUES (?,?)", (trade_id, frozen_message),
            )
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



def eur(value_cents):
    """Apresentação portuguesa dos cêntimos de uma carteira virtual."""
    return f"{int(value_cents) / 100:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".") + " €"


def trade_details(conn, trade_id):
    """Linha completa da operação, sem alterar saldos ou estados."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM paper_v12_trades WHERE id=?", (int(trade_id),)
    ).fetchone()
    return row


def format_wallets(conn):
    """Mostra apenas saldo livre; capital aberto continua reservado."""
    balances = free_balances(conn)
    return (
        f"💰 Carteira Polymarket: {eur(balances['poly'])}\n"
        f"💰 Carteira SX Bet: {eur(balances['sx'])}"
    )


def format_open_message(conn, trade_id, odds_poly=None, odds_sx=None,
                        demo=False, game_start=None):
    """Mensagem enviada apenas para uma OPERAÇÃO VIRTUAL efetivamente registada."""
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "OPEN_CONDITIONAL":
        raise ValueError("Operação virtual aberta não encontrada")
    def odds(value):
        return f" | odd {value:.2f}".replace(".", ",") if value is not None else ""

    from datetime import datetime
    from zoneinfo import ZoneInfo
    game_time = "não disponível"
    if game_start:
        try:
            game_time = datetime.fromtimestamp(float(game_start), ZoneInfo("Europe/Lisbon")).strftime("%d/%m/%Y %H:%M (Portugal)")
        except (ValueError, TypeError, OSError, OverflowError):
            pass
    balances = free_balances(conn)
    message = [
        f"🧪 {'DEMONSTRAÇÃO FICTÍCIA' if demo else 'APOSTA VIRTUAL'} #{trade_id}",
        f"🏟️ {record['event']}",
        f"📅 Início previsto: {game_time}",
        f"🕒 Registo: {time.strftime('%d/%m/%Y %H:%M:%S UTC', time.gmtime(record['created_epoch']))}",
        f"🏷️ Modalidade: {record['sport']} | fase: {record['stage']}",
        "",
        "1️⃣ POLYMARKET",
        f"Equipa: {record['poly_side']}{odds(odds_poly)}",
        f"Valor retirado: {eur(record['poly_debit_cents'])}",
        "",
        "2️⃣ SX BET",
        f"Equipa: {record['sx_side']}{odds(odds_sx)}",
        f"Valor retirado: {eur(record['sx_debit_cents'])}",
        "",
        "ℹ️ Odds indicadas: melhor nível de compra REST, NÃO execução confirmada.",
        f"💶 Total aplicado: {eur(record['poly_debit_cents'] + record['sx_debit_cents'])}",
        "💰 SALDOS (antes → depois)",
        f"Carteira Polymarket: {eur(balances['poly'] + record['poly_debit_cents'])} → {eur(balances['poly'])}",
        f"Carteira SX Bet: {eur(balances['sx'] + record['sx_debit_cents'])} → {eur(balances['sx'])}",
        "",
        "⏳ Resultado: AGUARDA LIQUIDAÇÃO",
        f"📈 Lucro condicional previsto: {eur(record['conditional_profit_cents'])}",
        f"💵 Pagamento virtual condicional: {eur(record['conditional_payout_cents'])}",
        f"📊 ROI condicional: {100 * record['conditional_profit_cents'] / (record['poly_debit_cents'] + record['sx_debit_cents']):+.2f}%"
        + (" · 🧪 MARGEM BAIXA / EXPERIMENTAL" if
           record["conditional_profit_cents"] /
           (record["poly_debit_cents"] + record["sx_debit_cents"]) < 0.015
           else ""),
        "🔒 Apenas dinheiro fictício. Sem apostas reais.",
    ]
    return "\n".join(message)


def format_settlement_message(conn, trade_id, demo=False):
    """Mensagem APÓS liquidação manualmente verificada das duas plataformas."""
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "SETTLED":
        raise ValueError("Operação ainda não liquidada/verificada")
    winner_platform = record["settled_side"]
    if winner_platform not in ("poly", "sx"):
        raise ValueError("Liquidação sem vencedor reconhecido")
    winner = record["poly_side"] if winner_platform == "poly" else record["sx_side"]
    platform = "POLYMARKET" if winner_platform == "poly" else "SX BET"
    loser = "SX BET" if winner_platform == "poly" else "POLYMARKET"
    net = int(record["realized_profit_cents"])
    balances = free_balances(conn)
    poly_before = balances["poly"] - (
        record["conditional_payout_cents"] if winner_platform == "poly" else 0
    )
    sx_before = balances["sx"] - (
        record["conditional_payout_cents"] if winner_platform == "sx" else 0
    )
    icon = "✅" if net >= 0 else "🔴"
    lines = [
        f"🏁 {'RESULTADO DEMONSTRATIVO' if demo else 'RESULTADO VIRTUAL CONFIRMADO'} #{trade_id}",
        f"🏟️ {record['event']}",
        f"🏆 Venceu: {winner}",
        f"✅ Paga a carteira: {platform}",
        f"❌ A outra aposta ({loser}) perdeu",
        f"💸 Valor creditado: {eur(record['conditional_payout_cents'])}",
        f"{icon} Resultado líquido: {'+' if net > 0 else ''}{eur(net)}",
        "",
        "💰 SALDOS (antes → depois)",
        f"Carteira Polymarket: {eur(poly_before)} → {eur(balances['poly'])}",
        f"Carteira SX Bet: {eur(sx_before)} → {eur(balances['sx'])}",
        "🔒 Resultado simulado, não houve apostas reais.",
    ]
    return "\n".join(lines)


def send_telegram_notice(message):
    """Enviar Telegram sem expor token de API em erros ou logs."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print("ℹ️ Telegram não configurado: só foi gerado texto.")
        return False
    try:
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message,
                  "disable_web_page_preview": True},
            timeout=12,
        )
        return response.status_code == 200 and response.json().get("ok") is True
    except Exception:
        return False


def send_open_once(conn, trade_id, now_epoch=None, retry_seconds=60):
    """Enviar aviso de abertura da outbox. NÃO cria novas operações.

    Timeout HTTP após receção pelo Telegram pode gerar duplicação do aviso
    numa tentativa posterior. Nunca gera um segundo débito.
    """
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    row = conn.execute("""
        SELECT message, sent_epoch, last_attempt_epoch
        FROM paper_v12_open_notifications WHERE trade_id=?
    """, (int(trade_id),)).fetchone()
    if row is None:
        return False  # operação antiga ou teste sem snapshot de odds
    message, sent_epoch, last_attempt = row
    if sent_epoch is not None:
        return True
    if last_attempt is not None and now_epoch - int(last_attempt) < retry_seconds:
        return False
    # Marcar tentativa ANTES do pedido HTTP para evitar tempestades após crash.
    with conn:
        conn.execute("""
            UPDATE paper_v12_open_notifications
            SET attempts=attempts+1, last_attempt_epoch=?
            WHERE trade_id=? AND sent_epoch IS NULL
        """, (now_epoch, int(trade_id)))
    if not send_telegram_notice(message):
        return False
    with conn:
        conn.execute("""
            UPDATE paper_v12_open_notifications
            SET sent_epoch=?
            WHERE trade_id=? AND sent_epoch IS NULL
        """, (int(time.time()), int(trade_id)))
    return True


def retry_pending_open_notices(conn, limit=3):
    """Chamado periodicamente pelo monitor principal; exclui trades legados."""
    pending = conn.execute("""
        SELECT n.trade_id FROM paper_v12_open_notifications n
        JOIN paper_v12_trades t ON t.id=n.trade_id
        WHERE n.sent_epoch IS NULL
          AND t.status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED')
          AND (n.last_attempt_epoch IS NULL
               OR n.last_attempt_epoch <= ?)
        ORDER BY n.trade_id LIMIT ?
    """, (int(time.time()) - 60, limit)).fetchall()
    delivered = 0
    for (ident,) in pending:
        if send_open_once(conn, ident):
            delivered += 1
    return delivered


def send_settlement_once(conn, trade_id):
    """Uma notificação por liquidação; retry manual em caso de falha de rede."""
    record = trade_details(conn, trade_id)
    if record is None or record["status"] != "SETTLED":
        raise ValueError("Só é possível notificar uma liquidação confirmada")
    if conn.execute(
        "SELECT 1 FROM paper_v12_notifications WHERE trade_id=?",
        (trade_id,),
    ).fetchone():
        return True  # já enviado — nunca repetir automaticamente
    message = format_settlement_message(conn, trade_id)
    if not send_telegram_notice(message):
        return False
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_v12_notifications(trade_id, sent_epoch) "
            "VALUES(?,?)",
            (trade_id, int(time.time())),
        )
    return True


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
    # Best asks na 2.ª leitura REST: não confundir com odd média garantida
    # de execução do stake. A mensagem explicita essa limitação.
    best_poly = min(price for price, _ in poly_levels)
    best_sx = min(price for price, _ in sx_levels)
    trade_id = book_shadow_trade(
        conn, pair, quote, stage,
        opening_odds=(1.0 / best_poly, 1.0 / best_sx),
    )
    return (trade_id, quote) if trade_id is not None else None


def self_test():
    import tempfile

    # Um livro sintético com ROI entre 0,5 e 1,5% tem de passar SOMENTE
    # no piloto, continuando a aplicar taxa, buffer e limite de 100 EUR.
    global MIN_NET_ROI
    saved_min = MIN_NET_ROI
    try:
        MIN_NET_ROI = 0.005
        pilot = select_shadow_quote(
            [(0.45, 300)], [(0.516, 300)], 0.91,
            100_000, 100_000,
        )
        assert pilot is not None and 0.005 <= pilot.roi < 0.015, pilot
        assert pilot.total_cents <= MAX_TRADE_CENTS
        MIN_NET_ROI = 0.015
        assert select_shadow_quote(
            [(0.45, 300)], [(0.516, 300)], 0.91,
            100_000, 100_000,
        ) is None
    finally:
        MIN_NET_ROI = saved_min

    # +47% deve ser rejeitado mesmo com liquidez aparente.
    assert select_shadow_quote(
        [(0.51, 300)], [(0.14625, 300)], 0.91, 100_000, 100_000
    ) is None

    q = select_shadow_quote([(0.43, 300)], [(0.50, 300)], 0.91, 100_000, 100_000)
    assert q and q.total_cents <= 10_000 and q.profit_eur > 0
    assert q.total_eur >= 5 and q.roi >= MIN_NET_ROI
    assert select_shadow_quote([(0.55, 300)], [(0.55, 300)], 0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 0.1)], [(0.50, 300)], 0.91, 100_000, 100_000) is None
    assert quote_for_payout([(0.43, 10)], [(0.50, 10)], 10, 0.91).poly_fee_cents > 0
    # Falha da segunda perna, perda de liquidez e spread adverso não podem
    # criar duas apostas fictícias com lucro.
    assert select_shadow_quote([(0.43, 300)], [], 0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.60, 300)],
                               0.91, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.50, 300)],
                               0, 100_000, 100_000) is None
    assert select_shadow_quote([(0.43, 300)], [(0.50, 300)],
                               0.91, 0, 100_000) is None

    sample = {
        "poly": {"id": "poly-1", "type": "moneyline",
                 "sport": "basketball", "start": 1800000000},
        "sx": {"hash": "sx-1", "sport": "basketball",
               "teams": ("Lakers", "Celtics"), "start": 1800000000},
        "poly_label": "Lakers", "sx_label": "Celtics",
    }
    with tempfile.TemporaryDirectory() as temp:
        db = open_database(str(Path(temp) / "paper.sqlite3"))
        ident = book_shadow_trade(
            db, sample, q, "PRE_JOGO", now_epoch=1000,
            opening_odds=(2.33, 2.0),
        )
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
        opening = format_open_message(db, ident, odds_poly=2.33, odds_sx=2.0)
        assert "Polymarket" in opening and "SX Bet" in opening
        assert "Valor retirado" in opening and "AGUARDA LIQUIDAÇÃO" in opening
        assert "Carteira Polymarket" in opening and "Carteira SX Bet" in opening
        stored = db.execute(
            "SELECT message, sent_epoch, attempts FROM paper_v12_open_notifications "
            "WHERE trade_id=?", (ident,)
        ).fetchone()
        assert stored and "Lakers vs Celtics" in stored[0]
        assert "odd 2,33" in stored[0] and stored[1] is None and stored[2] == 0
        original_send = send_telegram_notice
        attempts = []
        try:
            def mocked_send(msg):
                attempts.append(msg)
                return len(attempts) >= 2
            globals()["send_telegram_notice"] = mocked_send
            assert not send_open_once(db, ident, now_epoch=1001, retry_seconds=0)
            assert db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0] == 1
            assert send_open_once(db, ident, now_epoch=1002, retry_seconds=0)
            assert send_open_once(db, ident, now_epoch=1003, retry_seconds=0)
            assert len(attempts) == 2
            assert db.execute(
                "SELECT sent_epoch IS NOT NULL FROM paper_v12_open_notifications "
                "WHERE trade_id=?", (ident,)
            ).fetchone()[0] == 1
            assert retry_pending_open_notices(db) == 0
        finally:
            globals()["send_telegram_notice"] = original_send

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
        settling = format_settlement_message(db, ident)
        assert "Venceu: Lakers" in settling and "POLYMARKET" in settling
        assert "Resultado líquido" in settling
        assert "Carteira SX Bet" in settling
        assert db.execute("SELECT COUNT(*) FROM paper_v12_trades").fetchone()[0] == 1

        # Outro jogo: desta vez vence a SX Bet; apenas a SX recebe o prémio.
        opposite = {
            **sample,
            "poly": {**sample["poly"], "id": "poly-2"},
            "sx": {**sample["sx"], "hash": "sx-2"},
        }
        ident2 = book_shadow_trade(db, opposite, q, "PRE_JOGO")
        assert ident2 is not None
        before = free_balances(db)
        settle_normal(db, ident2, "sx", verified=True)
        after = free_balances(db)
        assert after["poly"] == before["poly"]
        assert after["sx"] == before["sx"] + q.payout_eur_cents
        text_result = format_settlement_message(db, ident2)
        assert "Venceu: Celtics" in text_result
        assert "Paga a carteira: SX BET" in text_result
        assert "SALDOS (antes → depois)" in text_result
        db.close()
    print("✅ PAPER V12 | matemática, carteiras, limite 100€, duplicados e liquidação OK")


def cli():
    parser = argparse.ArgumentParser(description="Shadow Paper V12 (sem apostas reais)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--settle-id", type=int)
    parser.add_argument("--winner", choices=["poly", "sx"])
    parser.add_argument("--verified-both-rules", action="store_true")
    parser.add_argument("--notify-open-id", type=int,
                        help="Tentar reenviar aviso de abertura pendente (sem duplicar aposta)")
    parser.add_argument("--pending-open-notices", action="store_true",
                        help="Ver avisos de abertura ainda não confirmados")
    parser.add_argument("--notify-settlement-id", type=int,
                        help="Reenviar notificação pendente (sem duplicações)")
    parser.add_argument("--list-open", action="store_true",
                        help="Listar operações simuladas abertas")
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
            print(format_settlement_message(conn, args.settle_id))
            if not send_settlement_once(conn, args.settle_id):
                print("⚠️ Telegram não confirmou a mensagem. "
                      "Pode repetir com --notify-settlement-id.")
        elif args.notify_open_id is not None:
            ok = send_open_once(conn, args.notify_open_id, retry_seconds=0)
            print("✅ Aviso enviado ou já confirmado." if ok else
                  "⚠️ Aviso não confirmado. Confirma id e Telegram nos logs.")
        elif args.pending_open_notices:
            pending = conn.execute("""
                SELECT trade_id, attempts, last_attempt_epoch
                FROM paper_v12_open_notifications
                WHERE sent_epoch IS NULL ORDER BY trade_id
            """).fetchall()
            for trade_id, attempts, last in pending:
                print(f"#{trade_id} | tentativas={attempts} | último={last}")
            if not pending:
                print("✅ Sem avisos de abertura pendentes (não inclui trades antigos).")
        elif args.notify_settlement_id is not None:
            if not send_settlement_once(conn, args.notify_settlement_id):
                print("⚠️ Telegram indisponível. O resultado foi preservado no SQLite.")
            else:
                print("✅ Liquidação já notificada ou agora enviada.")
        elif args.list_open:
            for row in conn.execute(
                "SELECT id, event, poly_side, sx_side, status "
                "FROM paper_v12_trades "
                "WHERE status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED') "
                "ORDER BY id"
            ):
                print(f"#{row[0]} | {row[1]} | Poly={row[2]} | SX={row[3]} | {row[4]}")
        elif args.flag_exception is not None:
            flag_exception(conn, args.flag_exception, args.reason or "")
            print(f"Operação #{args.flag_exception} bloqueada para revisão")
        else:
            print(paper_summary(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
