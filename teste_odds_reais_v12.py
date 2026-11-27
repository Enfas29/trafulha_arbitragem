"""Teste pontual com odds REAIS Polymarket + SX Bet, sem apostar.

Recolhe livros de ordens REST, encontra UM par de resultados opostos
do mesmo confronto e calcula uma aposta hipotética até 10 €.
NÃO debita carteiras V12, não cria ordens, não requer dinheiro real.
A amostra fica num SQLite separado para auditoria.

Railway Console: python teste_odds_reais_v12.py
Offline:         python teste_odds_reais_v12.py --self-test
"""
from __future__ import annotations

import argparse
import html
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import trafulha_arbitragem as bot
import paper_v12 as paper

TEST_DB = os.getenv("ODDS_TEST_DB", "/data/odds_test_v12.sqlite3")
MAX_SNAPSHOT_GAP = 6.0
TEST_TOTAL_EUR_CENTS = 1000   # limite único: 10 €, NÃO é uma ordem
MIN_TEST_EUR_CENTS = 500      # exigir no mínimo 5 € de liquidez estimada
MAX_CHECKS = 30


def select_test_quote(poly_levels, sx_levels, fx):
    """Uma cotação até 10 €, mesmo que o ROI seja NEGATIVO."""
    if not (poly_levels and sx_levels and 0 < fx < 10):
        return None
    if any(not (0 < price < 1 and size > 0)
           for book in (poly_levels, sx_levels) for price, size in book):
        return None
    capacity = min(
        sum(size for _, size in poly_levels),
        sum(size for _, size in sx_levels),
        200.0 / fx,
    )
    low, high = 0.0, capacity
    for _ in range(40):
        mid = (low + high) / 2
        quote = paper.quote_for_payout(poly_levels, sx_levels, mid, fx)
        if quote is not None and quote.total_cents <= TEST_TOTAL_EUR_CENTS:
            low = mid
        else:
            high = mid
    payout = int(low * 1_000_000) / 1_000_000
    quote = paper.quote_for_payout(poly_levels, sx_levels, payout, fx)
    if quote is None or not (MIN_TEST_EUR_CENTS <= quote.total_cents <= TEST_TOTAL_EUR_CENTS):
        return None
    poly_walk = paper.walk_book(poly_levels, payout)
    sx_walk = paper.walk_book(sx_levels, payout)
    if poly_walk is None or sx_walk is None:
        return None
    return quote, payout / poly_walk[0], payout / sx_walk[0]


def choose_pairs(poly_markets, sx_markets, now=None):
    """Priorizar jogos futuros; manter apenas pares com nomes/hora coerentes."""
    now = time.time() if now is None else now
    options = bot.pair_markets(poly_markets, sx_markets)
    allowed = []
    for pair in options:
        p, s = pair["poly"], pair["sx"]
        if p["sport"] != s["sport"]:
            continue
        if abs(float(p["start"]) - float(s["start"])) > 1800:
            continue
        if not (now - 4 * 3600 <= float(s["start"]) <= now + 7 * 86400):
            continue
        if float(s["start"]) <= now and not s.get("live_enabled"):
            continue
        allowed.append(pair)
    # Preferir pré-jogo para reduzir dúvidas de regras live.
    return sorted(allowed, key=lambda pair: (
        pair["sx"]["start"] <= now,
        abs(pair["sx"]["start"] - now),
        pair["key"],
    ))


def make_snapshot(pair, poly_levels, sx_levels, poly_time, sx_time, fx):
    skew = abs(poly_time - sx_time)
    if skew > MAX_SNAPSHOT_GAP:
        return None
    sample = select_test_quote(poly_levels, sx_levels, fx)
    if sample is None:
        return None
    quote, poly_effective_odd, sx_effective_odd = sample
    p = min(poly_levels, key=lambda level: level[0])[0]
    s = min(sx_levels, key=lambda level: level[0])[0]
    if p <= 0 or s <= 0:
        return None
    return {
        "pair": pair,
        "quote": quote,
        "best_poly_odd": 1 / p,
        "best_sx_odd": 1 / s,
        "poly_effective_odd": poly_effective_odd,
        "sx_effective_odd": sx_effective_odd,
        "poly_price": p,
        "sx_price": s,
        "skew_sec": skew,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def format_eur(value):
    """Formato português: 8,89 € em vez de 8.89€."""
    return f"{float(value):,.2f}".translate(str.maketrans(",.", ".,")) + " €"


def format_odd(value):
    """Odds decimais, tal como no site: duas casas."""
    return f"{float(value):.2f}".replace(".", ",")


def format_message(snapshot, html_mode=False, balances=None):
    """Uma mensagem clara: plataforma, equipa, odd e montante simulado."""
    pair = snapshot["pair"]
    quote = snapshot["quote"]
    sx, poly = pair["sx"], pair["poly"]
    observed = datetime.fromisoformat(
        snapshot["observed_at"]
    ).astimezone(timezone.utc).strftime("%d/%m %H:%M:%S UTC")

    def show(value):
        return html.escape(str(value), quote=False) if html_mode else str(value)

    def strong(value):
        value = show(value)
        return f"<b>{value}</b>" if html_mode else value

    poly_start = datetime.fromtimestamp(
        float(poly["start"]), timezone.utc
    ).strftime("%H:%M")
    sx_start = datetime.fromtimestamp(
        float(sx["start"]), timezone.utc
    ).strftime("%H:%M")
    difference = abs(float(poly["start"]) - float(sx["start"]))
    start = "⏰ Início: " + sx_start + " UTC"
    if difference >= 60:
        start = (
            f"⏰ Início: Poly {poly_start} | SX {sx_start} UTC"
            "\n⚠️ Horários diferentes — confirmar o mesmo jogo"
        )

    verdict = (
        strong("⛔ MARGEM NEGATIVA — NÃO APOSTAR")
        if quote.conditional_profit_cents <= 0 else
        strong("🔎 Margem positiva estimada — NÃO EXECUTADO")
    )
    lines = [
        f"🧪 {strong('TESTE DE ODDS REAIS · V12')}",
        f"🎾 {strong(' vs '.join(sx['teams']))}",
        start,
        "",
        f"1️⃣ {strong('POLYMARKET')}",
        f"Equipa: {strong(pair['poly_label'])}",
        f"Odd na API: {strong(format_odd(snapshot['best_poly_odd']))}"
        f" ({round(snapshot['poly_price'] * 100):d}¢)",
        f"Valor virtual: {strong(format_eur(quote.poly_debit_cents / 100))}",
        "",
        f"2️⃣ {strong('SX BET')}",
        f"Equipa: {strong(pair['sx_label'])}",
        f"Odd na API: {strong(format_odd(snapshot['best_sx_odd']))}",
        f"Valor virtual: {strong(format_eur(quote.sx_debit_cents / 100))}",
        "",
        f"💶 Total do teste: {strong(format_eur(quote.total_eur))}",
        "",
        "💰 CARTEIRAS VIRTUAIS (SEM DÉBITOS NESTE TESTE)",
        (f"Polymarket: {strong(format_eur(balances['poly'] / 100))}"
         if balances else "Polymarket: saldo não disponível"),
        (f"SX Bet: {strong(format_eur(balances['sx'] / 100))}"
         if balances else "SX Bet: saldo não disponível"),
        f"📊 Resultado estimado: {strong(format_eur(quote.profit_eur))} "
        f"({quote.roi * 100:+.2f}%)",
        verdict,
        "",
        f"🕒 Leitura: {observed}",
        "ℹ️ Odd lida no livro REST; pode diferir da página noutro instante.",
        "🔒 Sem apostas nem débitos virtuais.",
    ]
    return "\n".join(lines)



def record_snapshot(snapshot, path=TEST_DB):
    """Auditoria independente: nenhuma operação na carteira virtual principal."""
    parent = Path(path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_odds_snapshots (
                id INTEGER PRIMARY KEY,
                observed_at TEXT NOT NULL,
                sport TEXT NOT NULL,
                event TEXT NOT NULL,
                poly_market TEXT NOT NULL,
                sx_market TEXT NOT NULL,
                poly_outcome TEXT NOT NULL,
                sx_outcome TEXT NOT NULL,
                best_poly_odd REAL NOT NULL,
                best_sx_odd REAL NOT NULL,
                test_poly_eur REAL NOT NULL,
                test_sx_eur REAL NOT NULL,
                conditional_roi_pct REAL NOT NULL,
                snapshot_gap_seconds REAL NOT NULL,
                status TEXT NOT NULL
            )
        """)
        pair, quote = snapshot["pair"], snapshot["quote"]
        with conn:
            cursor = conn.execute("""
                INSERT INTO test_odds_snapshots (
                    observed_at, sport, event, poly_market, sx_market,
                    poly_outcome, sx_outcome, best_poly_odd, best_sx_odd,
                    test_poly_eur, test_sx_eur, conditional_roi_pct,
                    snapshot_gap_seconds, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                snapshot["observed_at"], pair["sx"]["sport"],
                " vs ".join(pair["sx"]["teams"]),
                str(pair["poly"]["id"]), str(pair["sx"]["hash"]),
                pair["poly_label"], pair["sx_label"],
                snapshot["best_poly_odd"], snapshot["best_sx_odd"],
                quote.poly_debit_cents / 100, quote.sx_debit_cents / 100,
                quote.roi * 100, snapshot["skew_sec"], "TEST_ONLY_NOT_EXECUTED",
            ))
        return cursor.lastrowid
    finally:
        conn.close()


def read_wallet_balances():
    """Consulta APENAS de leitura da carteira V12; não cria nem altera saldos."""
    path = Path(paper.PAPER_DB)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        try:
            values = paper.free_balances(conn)
            return values if "poly" in values and "sx" in values else None
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


def telegram_parts(message, max_chars=3600):
    """Dividir diagnósticos longos respeitando linhas e limite do Telegram."""
    text = str(message)
    if not text.strip():
        return []
    result = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                result.append(current.rstrip("\n"))
                current = ""
            result.append(line[:max_chars])
            line = line[max_chars:]
        if len(current) + len(line) > max_chars and current:
            result.append(current.rstrip("\n"))
            current = ""
        current += line
    if current.strip():
        result.append(current.rstrip("\n"))
    return result


def send_telegram(message, html_mode=False):
    """Enviar mensagem sem parse_mode=null (rejeitado por Telegram).

    O teste sem HTML usa só texto plano. Não imprimir token de Bot API,
    nem cabeçalhos ou payloads sensíveis nos logs.
    """
    if not (bot.TG_TOKEN and bot.TG_CHAT):
        print("⚠️ Telegram não configurado: relatório apenas na consola.")
        return False
    parts = telegram_parts(message)
    if not parts:
        print("⚠️ Telegram: mensagem vazia, não enviada.")
        return False
    try:
        import requests
        for part in parts:
            payload = {
                "chat_id": bot.TG_CHAT,
                "text": part,
                "disable_web_page_preview": True,
            }
            if html_mode:
                payload["parse_mode"] = "HTML"
            response = requests.post(
                f"https://api.telegram.org/bot{bot.TG_TOKEN}/sendMessage",
                json=payload, timeout=12
            )
            if response.status_code != 200:
                print(f"⚠️ Telegram devolveu HTTP {response.status_code}.")
                return False
            if response.json().get("ok") is not True:
                print("⚠️ Telegram não confirmou a entrega do relatório.")
                return False
        return True
    except Exception:
        print("⚠️ Não foi possível confirmar o envio para o Telegram.")
        return False



def run_once():
    fx = float(os.getenv("EUR_PER_USD", "0") or "0")
    if not (0 < fx < 10):
        raise SystemExit("EUR_PER_USD não está configurado corretamente no Railway.")
    print("🔎 A obter mercados reais (só leitura)...", flush=True)
    poly = bot.discover_poly()
    sx = bot.discover_sx()
    pairs = choose_pairs(poly, sx)
    print(f"📚 {len(pairs)} direções elegíveis; até {MAX_CHECKS} tentativas.", flush=True)

    for idx, pair in enumerate(pairs[:MAX_CHECKS], 1):
        try:
            poly_levels, tp = bot.poly_book(pair["poly_asset"])
            sx_levels, ts = bot.sx_book(pair["sx"]["hash"], pair["sx_target"])
            snapshot = make_snapshot(pair, poly_levels, sx_levels, tp, ts, fx)
            if snapshot is None:
                continue
        except Exception as exc:
            # Apenas o tipo de erro: nunca imprimir credenciais das APIs.
            print(f"Livro {idx}: indisponível ({type(exc).__name__}).", flush=True)
            continue
        record_id = record_snapshot(snapshot)
        balances = read_wallet_balances()
        msg = format_message(snapshot, balances=balances)
        print("\n"+msg)
        print(f"\n✅ Amostra #{record_id} guardada numa base separada.")
        print("✅ Telegram enviado." if send_telegram(format_message(snapshot, html_mode=True, balances=balances), html_mode=True)
              else "ℹ️ Consulta a mensagem na consola.")
        return 0
    print("⚠️ Sem livros comparáveis e com liquidez suficiente nesta amostra.")
    print("Não enviei odds inventadas nem fiz apostas.")
    return 2


def self_test():
    import tempfile
    now = time.time()
    pair = {
        "key": "poly:sx:0",
        "poly": {"id": "p123", "sport": "basketball",
                 "start": now+3600, "type": "moneyline"},
        "sx": {"hash": "sx456", "sport": "basketball",
               "start": now+3600, "teams": ("Team A", "Team B"),
               "live_enabled": False},
        "poly_label": "Team A", "sx_label": "Team B",
    }
    book_poly = [(0.47, 200.0)]
    book_sx = [(0.56, 200.0)]
    quote = select_test_quote(book_poly, book_sx, 0.91)
    assert quote is not None
    q, odd_poly, odd_sx = quote
    assert 5 <= q.total_eur <= 10
    assert q.roi < 0, "Uma margem negativa deve ser aceite no teste"
    assert abs(odd_poly - 1/0.47) < 1e-6
    assert abs(odd_sx - 1/0.56) < 1e-6
    assert select_test_quote([], book_sx, 0.91) is None
    assert select_test_quote([(0.47, 0.01)], book_sx, 0.91) is None
    snap = make_snapshot(pair, book_poly, book_sx, 5.0, 5.5, 0.91)
    assert snap is not None
    assert make_snapshot(pair, book_poly, book_sx, 5.0, 12.0, 0.91) is None
    msg = format_message(snap)
    assert "TESTE DE ODDS REAIS" in msg
    assert "Team A" in msg and "Team B" in msg
    assert "Sem apostas nem débitos virtuais" in msg
    assert "POLYMARKET" in msg and "SX BET" in msg
    assert "Equipa: Team A" in msg and "Equipa: Team B" in msg
    assert "Valor virtual:" in msg
    with_balances = format_message(
        snap, balances={"poly": 100_000, "sx": 100_000}
    )
    assert "Polymarket: 1.000,00 €" in with_balances
    assert "SX Bet: 1.000,00 €" in with_balances
    assert "SEM DÉBITOS NESTE TESTE" in with_balances
    assert "MARGEM NEGATIVA" in msg
    assert "Odd na API: 2,13" in msg
    assert "Odd na API: 1,79" in msg
    rich = format_message(snap, html_mode=True)
    assert "<b>POLYMARKET</b>" in rich
    assert "<b>Team A</b>" in rich and "<b>Team B</b>" in rich
    assert telegram_parts("") == []
    assert telegram_parts("a\nb\nc", max_chars=2) == ["a", "b", "c"]
    assert all(len(chunk) <= 3600
               for chunk in telegram_parts("linha\n" * 1400))
    # Confirmar que o Telegram recebe texto sem parse_mode null.
    import requests
    old_post = requests.post
    sent = []
    class FakeResponse:
        status_code = 200
        def json(self):
            return {"ok": True}
    try:
        def fake_post(url, json, timeout):
            sent.append(json)
            return FakeResponse()
        requests.post = fake_post
        old_token, old_chat = bot.TG_TOKEN, bot.TG_CHAT
        try:
            bot.TG_TOKEN, bot.TG_CHAT = "TEST_TOKEN", "TEST_CHAT"
            assert send_telegram("AUDITORIA TESTE", html_mode=False)
            assert "parse_mode" not in sent[-1]
            assert send_telegram("<b>TESTE</b>", html_mode=True)
            assert sent[-1]["parse_mode"] == "HTML"
        finally:
            bot.TG_TOKEN, bot.TG_CHAT = old_token, old_chat
    finally:
        requests.post = old_post

    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "test.sqlite3")
        assert record_snapshot(snap, path) == 1
        conn = sqlite3.connect(path)
        record = conn.execute(
            "SELECT status,conditional_roi_pct FROM test_odds_snapshots"
        ).fetchone()
        assert record[0] == "TEST_ONLY_NOT_EXECUTED"
        assert record[1] < 0
        conn.close()
    print("✅ Teste offline: odds, margem negativa, livro, SQLite e Telegram OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        raise SystemExit(run_once())
