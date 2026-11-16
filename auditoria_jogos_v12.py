"""Auditoria pontual de odds REAIS, por vários jogos, sem apostas.

Railway:
  python auditoria_jogos_v12.py --sport soccer --count 3
  python auditoria_jogos_v12.py --sport tennis --count 3
  python auditoria_jogos_v12.py --sport basketball --count 3
  python auditoria_jogos_v12.py --self-test

O futebol é 1X2: NÃO calcular arbitragem de duas pernas que ignore empate.
Não altera /data/paper_v12.sqlite3 ou saldos; guarda apenas auditoria separada.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import trafulha_arbitragem as bot
import teste_odds_reais_v12 as single

AUDIT_DB = os.getenv("ODDS_AUDIT_DB", "/data/odds_auditoria_v12.sqlite3")
SPORTS = ("soccer", "tennis", "basketball", "american_football", "cricket",
          "baseball", "esports", "combat")
MAX_SOCCER_SERIES = 38
MAX_SX_PAGES = 18
HORIZON = 7 * 86400


def display_odd(price):
    return single.format_odd(1.0 / price)


def parse_list(obj):
    return bot.values(obj)


def in_window(when, now):
    return bool(when and now - 3600 * 3 <= when <= now + HORIZON)


def match_teams(t1, t2, left, right):
    return ((bot.same_team(t1, left) and bot.same_team(t2, right))
            or (bot.same_team(t1, right) and bot.same_team(t2, left)))


def category(label, teams):
    """Classificar apenas resultados EXATOS. Nunca equiparar 'Not X' a Y."""
    raw = bot.ascii_name(label)
    if not raw:
        return None
    if raw in ("draw", "tie", "empate", "x"):
        return "draw"
    if raw.startswith(("not ", "nao ", "no ")) or "not to" in raw:
        return None
    if bot.same_team(label, teams[0]):
        return "home"
    if bot.same_team(label, teams[1]):
        return "away"
    return None


def gamma_market_category(m, teams):
    # Primeiro nome individual explícito; em segundo lugar, título do mercado.
    for field in ("groupItemTitle", "title", "question"):
        value = str(m.get(field) or "").strip()
        cat = category(value, teams)
        if cat:
            return cat
        normalized = bot.ascii_name(value)
        if normalized and re.search(r"\b(draw|tie|empate)\b", normalized):
            if re.search(r"\b(will|result|end|finish|match|jogo)\b", normalized):
                return "draw"
        hits = [
            bool(bot.comparable(team) and
                 bot.comparable(team) in bot.comparable(value))
            for team in teams
        ]
        if hits == [True, False]:
            return "home"
        if hits == [False, True]:
            return "away"
    return None


def poly_soccer_events(now=None):
    now = time.time() if now is None else now
    sports = bot.get_json(bot.POLY_GAMMA + "/sports")
    records = sports if isinstance(sports, list) else []
    series = []
    for row in records:
        if not isinstance(row, dict):
            continue
        if bot.sport_family(row.get("sport") or row.get("name")) != "soccer":
            continue
        for sid in re.findall(r"\d+", str(row.get("series") or "")):
            if sid not in series:
                series.append(sid)
    print(f"⚽ Polymarket: {len(series)} séries de futebol identificadas.", flush=True)
    events = []
    seen = set()
    for sid in series[:MAX_SOCCER_SERIES]:
        try:
            raw = bot.get_json(bot.POLY_GAMMA + "/events", {
                "series_id": sid, "active": "true", "closed": "false",
                "limit": 125, "offset": 0,
            }, timeout=14)
        except Exception as exc:
            print(f"⚠️ Gamma série {sid}: {type(exc).__name__}", flush=True)
            continue
        for event in raw if isinstance(raw, list) else []:
            if not isinstance(event, dict):
                continue
            name = str(event.get("title") or "")
            teams = bot.parse_title(name)
            if not teams:
                continue
            start = bot.unix_time(event.get("startTime") or event.get("gameStartTime"))
            if not in_window(start, now):
                continue
            key = str(event.get("id") or event.get("slug") or name)
            if key in seen:
                continue
            seen.add(key)
            picks = {}
            for m in event.get("markets") or []:
                if m.get("closed") or m.get("active") is False:
                    continue
                if not bot.is_poly_moneyline(m):
                    continue
                label = gamma_market_category(m, teams)
                if label is None:
                    continue
                outcomes = parse_list(m.get("outcomes"))
                assets = parse_list(
                    m.get("positionIds") if m.get("version") == "v2"
                    else m.get("clobTokenIds")
                )
                if len(outcomes) != len(assets):
                    continue
                # 3 tokens explicit; ou 3 mercados independentes SIM/NÃO.
                idx = None
                if len(outcomes) == 2 and {
                    bot.ascii_name(x) for x in outcomes
                } == {"yes", "no"}:
                    idx = next(
                        (i for i, x in enumerate(outcomes)
                         if bot.ascii_name(x) == "yes"), None
                    )
                elif len(outcomes) >= 3:
                    idx = next((i for i, x in enumerate(outcomes)
                                if category(x, teams) == label), None)
                if idx is None:
                    continue
                if label not in picks:
                    picks[label] = {
                        "asset": str(assets[idx]),
                        "market": str(m.get("question") or m.get("title") or name),
                    }
            if picks:
                events.append({
                    "id": key, "teams": teams, "start": start,
                    "slug": str(event.get("slug") or ""),
                    "picks": picks, "name": name,
                })
    print(f"⚽ Polymarket: {len(events)} jogos com resultados identificados.",
          flush=True)
    return events


def sx_soccer_ids():
    raw = bot.get_json(bot.SX_API + "/sports")
    items = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(items, dict):
        items = items.get("sports", [])
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("name") or item.get("sportLabel") or ""
        if bot.sport_family(label) != "soccer":
            continue
        sport_id = item.get("sportId") or item.get("id")
        if sport_id is not None:
            result.append(str(sport_id))
    return sorted(set(result))


def sx_soccer_events(now=None):
    now = time.time() if now is None else now
    try:
        ids = sx_soccer_ids()
    except Exception:
        ids = []
    # A leitura de mercados é pública; filtrar pelo desporto quando possível.
    results = []
    page_key = None
    seen = set()
    for _ in range(MAX_SX_PAGES):
        params = {"pageSize": 100, "onlyMainLine": "true"}
        if ids:
            params["sportIds"] = ",".join(ids)
        if page_key:
            params["paginationKey"] = page_key
        try:
            payload = bot.get_json(bot.SX_API + "/markets/active", params, timeout=14)
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
        except Exception as exc:
            print(f"⚠️ SX mercados futebol: {type(exc).__name__}", flush=True)
            break
        batch = data.get("markets", []) if isinstance(data, dict) else []
        if not batch:
            break
        for market in batch:
            if not isinstance(market, dict):
                continue
            sport_label = bot.sx_sport_family(market)
            if sport_label != "soccer" and (
                not ids or str(market.get("sportId")) not in ids
            ):
                continue
            if str(market.get("status") or "").upper() != "ACTIVE":
                continue
            teams = (market.get("teamOneName"), market.get("teamTwoName"))
            if not all(teams):
                continue
            start = bot.unix_time(market.get("gameTime"))
            if not in_window(start, now):
                continue
            h = str(market.get("marketHash") or "")
            if not h or h in seen:
                continue
            seen.add(h)
            outcomes = (
                str(market.get("outcomeOneName") or ""),
                str(market.get("outcomeTwoName") or ""),
            )
            results.append({
                "hash": h, "teams": teams, "start": start,
                "league": str(market.get("leagueLabel") or ""),
                "type": market.get("type"),
                "outcomes": outcomes,
            })
        page_key = data.get("nextKey") if isinstance(data, dict) else None
        if not page_key:
            break
    print(f"⚽ SX Bet: {len(results)} mercados de futebol com hora e equipas.",
          flush=True)
    return results


def soccer_matches(poly_events, sx_markets, now=None):
    """Só comparações de MESMO evento com ambos nomes e horários próximos."""
    now = time.time() if now is None else now
    unique = {}
    for poly in poly_events:
        if not in_window(poly["start"], now):
            continue
        matches = [
            sx for sx in sx_markets
            if match_teams(*sx["teams"], *poly["teams"])
            and abs(sx["start"] - poly["start"]) <= 1800
        ]
        if not matches:
            continue
        key = poly["id"]
        unique[key] = {"poly": poly, "sx": matches}
    return sorted(unique.values(), key=lambda d: (
        d["poly"]["start"] <= now,
        abs(d["poly"]["start"] - now),
        d["poly"]["id"]
    ))


def poly_soccer_prices(poly):
    prices = {}
    for side, item in poly["picks"].items():
        try:
            levels, _ = bot.poly_book(item["asset"])
            if levels:
                prices[side] = 1.0 / min(p for p, capacity in levels)
        except Exception:
            continue
    return prices


def sx_soccer_prices(sx_markets, teams):
    prices = {}
    # No máximo seis consultas por jogo; não confundir NOT TEAM com TEAM.
    looked = 0
    for market in sx_markets:
        for index, label in enumerate(market["outcomes"]):
            side = category(label, teams)
            if side is None or side in prices:
                continue
            try:
                levels, _ = bot.sx_book(market["hash"], index)
                looked += 1
                if levels:
                    prices[side] = 1.0 / min(p for p, capacity in levels)
            except Exception:
                pass
            if looked >= 6:
                return prices
    return prices


def soccer_message(pair, poly_prices, sx_prices, index, count):
    poly, group = pair["poly"], pair["sx"]
    teams = poly["teams"]
    start = datetime.fromtimestamp(poly["start"], timezone.utc)
    stamp = datetime.now(timezone.utc)
    def format_source(price):
        return single.format_odd(price) if price is not None else "— sem cotação"
    def tab(prices):
        return "\n".join([
            f"🏠 {teams[0]}: {format_source(prices.get('home'))}",
            f"🤝 Empate: {format_source(prices.get('draw'))}",
            f"🚌 {teams[1]}: {format_source(prices.get('away'))}",
        ])
    sx_start = group[0]["start"]
    warning = (
        "\n⚠️ Horários Poly/SX diferentes — conferir evento"
        if abs(sx_start - poly["start"]) > 60 else ""
    )
    return "\n".join([
        f"⚽ AUDITORIA FUTEBOL {index}/{count} · SÓ LEITURA",
        f"🏟️ {teams[0]} vs {teams[1]}",
        f"⏰ Início: {start:%d/%m %H:%M} UTC" + warning,
        "",
        "1️⃣ POLYMARKET · odd de compra no livro",
        tab(poly_prices),
        "",
        "2️⃣ SX BET · odd de compra no livro",
        tab(sx_prices),
        "",
        f"🕒 Consulta: {stamp:%d/%m %H:%M:%S} UTC",
        "⚠️ 1X2: vitória/empate/derrota; mercados e liquidação por validar.",
        "⚠️ 'Não vence' NÃO significa 'vence o adversário'.",
        "🔒 Não é arbitragem confirmada. Sem apostas ou débitos.",
    ])


def db_open(path=AUDIT_DB):
    Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audited_events (
            key TEXT PRIMARY KEY, sport TEXT NOT NULL,
            sent_epoch INTEGER NOT NULL, source TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def was_sent(conn, key):
    return conn.execute(
        "SELECT 1 FROM audited_events WHERE key=?", (key,)
    ).fetchone() is not None


def mark_sent(conn, key, sport):
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO audited_events(key,sport,sent_epoch,source) "
            "VALUES (?,?,?,?)", (key, sport, int(time.time()), "READ_ONLY")
        )


def send(text):
    if not single.send_telegram(text):
        print("⚠️ Telegram não confirmou a mensagem; consulta na consola.")
        return False
    return True


def run_soccer(count, conn):
    poly = poly_soccer_events()
    sx = sx_soccer_events()
    matches = soccer_matches(poly, sx)
    print(f"⚽ Jogos identificados nas DUAS plataformas: {len(matches)}")
    sent, examined = 0, 0
    for match in matches:
        key = "soccer:" + match["poly"]["id"]
        if was_sent(conn, key):
            continue
        examined += 1
        pp = poly_soccer_prices(match["poly"])
        sp = sx_soccer_prices(match["sx"], match["poly"]["teams"])
        # Só enviar COMPARAÇÕES, não listas de uma só plataforma.
        if not pp or not sp:
            continue
        msg = soccer_message(match, pp, sp, sent+1, count)
        print("\n" + msg + "\n")
        if send(msg):
            mark_sent(conn, key, "soccer")
            sent += 1
        if sent >= count:
            break
    print(f"✅ Futebol: {sent} jogos enviados, {examined} jogos examinados.")
    if sent == 0:
        print("⚠️ Não houve jogos de futebol com odds comparáveis nos dois "
              "sites nesta consulta. Nada foi inventado.")
    return 0 if sent else 2


def run_standard(sport, count, conn):
    poly = bot.discover_poly()
    sx = bot.discover_sx()
    pairs = single.choose_pairs(poly, sx)
    sent, seen = 0, set()
    for pair in pairs:
        if pair["sx"]["sport"] != sport:
            continue
        key = "standard:" + str(pair["poly"]["id"]) + ":" + str(pair["sx"]["hash"])
        if key in seen or was_sent(conn, key):
            continue
        seen.add(key)
        try:
            pl, pt = bot.poly_book(pair["poly_asset"])
            sl, st = bot.sx_book(pair["sx"]["hash"], pair["sx_target"])
            snap = single.make_snapshot(pair, pl, sl, pt, st, bot.FX)
        except Exception:
            continue
        if snap is None:
            continue
        balances = single.read_wallet_balances()
        body = single.format_message(snap, balances=balances)
        title = f"🧪 AUDITORIA {sport.upper()} {sent+1}/{count}\n"
        print("\n" + title + body + "\n")
        if send(title + body):
            mark_sent(conn, key, sport)
            sent += 1
        if sent >= count:
            break
    print(f"✅ {sport}: {sent} jogos diferentes enviados ao Telegram.")
    if sent == 0:
        print("⚠️ Nenhum jogo novo com duas odds válidas nesta amostra.")
    return 0 if sent else 2


def tests():
    import tempfile
    now = time.time()
    p = {"id": "soc-1", "start": now + 3600,
         "teams": ("Team Alpha", "Team Beta"),
         "picks": {"home": {"asset": "1"}, "draw": {"asset": "2"}}}
    sx = [{"hash": "sx-1", "start": now + 3600,
           "teams": ("Team Alpha", "Team Beta"),
           "outcomes": ("Team Alpha", "Not Team Alpha")}]
    assert len(soccer_matches([p], sx, now)) == 1
    assert not soccer_matches([p], [{**sx[0], "start": now + 7200}], now)
    assert not soccer_matches([p], [{**sx[0], "teams": ("Other", "Another")}], now)
    assert category("Team Alpha", p["teams"]) == "home"
    assert category("Team Beta", p["teams"]) == "away"
    assert category("Draw", p["teams"]) == "draw"
    assert category("Not Team Alpha", p["teams"]) is None
    assert category("Not Draw", p["teams"]) is None
    assert gamma_market_category(
        {"groupItemTitle": "Draw"}, p["teams"]
    ) == "draw"
    msg = soccer_message(
        {"poly": p, "sx": sx},
        {"home": 2.1, "draw": 3.4}, {"home": 2.2},
        1, 3
    )
    assert "2,10" in msg and "2,20" in msg
    assert "Empate: 3,40" in msg
    assert "NÃO significa" in msg
    assert "Sem apostas ou débitos" in msg
    with tempfile.TemporaryDirectory() as tmp:
        conn = db_open(str(Path(tmp) / "audit.sqlite3"))
        assert not was_sent(conn, "soccer:soc-1")
        mark_sent(conn, "soccer:soc-1", "soccer")
        assert was_sent(conn, "soccer:soc-1")
        assert conn.execute("SELECT COUNT(*) FROM audited_events").fetchone()[0] == 1
        conn.close()
    print("✅ AUDITORIA V12: 1X2, duplicados, horários, mensagens e DB OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=SPORTS, default="soccer")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        tests()
        return 0
    if not 1 <= args.count <= 5:
        ap.error("--count tem de ser de 1 a 5")
    if args.sport != "soccer" and not (0 < bot.FX < 10):
        ap.error("EUR_PER_USD inválido: necessário para stakes hipotéticos")
    conn = db_open()
    try:
        if args.sport == "soccer":
            return run_soccer(args.count, conn)
        return run_standard(args.sport, args.count, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
