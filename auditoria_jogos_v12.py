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


def sx_complement(label, affirmative):
    """Confirma 'Not A' como oposto EXATO de A, incluindo Tie/Not Tie."""
    positive = bot.ascii_name(affirmative)
    negative = bot.ascii_name(label)
    if positive in {"draw", "tie", "empate", "x"}:
        return negative in {"not draw", "not tie", "not empate", "no draw"}
    return (
        negative.startswith(("not ", "no ", "nao "))
        and bot.same_team(negative.split(" ", 1)[1], affirmative)
    )


def sx_1x2_side(market, teams):
    """Uma linha só é 1X2 se tiver A/Not A, Tie/Not Tie ou B/Not B.

    Rejeita mercados 2-way, tipos de prolongamento, handicap, etc.
    Evita trocar uma odd de 'not A' pela odd de B.
    Retorna (nome da posição, índice do resultado positivo).
    """
    market_type = str(market.get("type") or "").strip().lower()
    if market_type not in {"", "52", "moneyline", "1x2", "1x2_moneyline"}:
        return None
    labels = market.get("outcomes") or ()
    if len(labels) != 2:
        return None
    candidates = []
    for index in (0, 1):
        positive = category(labels[index], teams)
        if positive and sx_complement(labels[1-index], labels[index]):
            candidates.append((positive, index))
    return candidates[0] if len(candidates) == 1 else None


def explicit_question_category(text, teams):
    """Classificar perguntas EXPLÍCITAS, nunca procurar uma equipa em 'A vs B'.

    'Will Man City vs Fulham end in a draw?' NÃO significa aposta no Fulham.
    Um título com o nome das duas equipas é metadado do evento, não um
    resultado individual.
    """
    text = bot.ascii_name(text)
    if not text:
        return None

    # Mercados diferentes de 1X2 não podem virar vitória/empate por texto.
    if any(flag in text for flag in (
        "draw no bet", "double chance", "first half", "second half",
        "half time", "halftime", "to qualify", "to advance",
        "extra time", "penalties",
    )):
        return None

    # A palavra 'draw' apenas identifica empate quando a PERGUNTA
    # pede explicitamente esse desfecho.
    if (re.search(r"\b(?:draw|tie|empate)\b", text)
            and re.search(
                r"\b(?:end|ends|finish|finishes|result|be|is|ends up)\b"
                r"(?:\s+\w+){0,5}\s+(?:draw|tie|empate)\b",
                text,
            )):
        return "draw"
    if re.fullmatch(r"(?:draw|tie|empate)", text):
        return "draw"

    # Só 'Will <equipa> win/beat ...?' ou '<equipa> to win?'.
    candidates = []
    m = re.match(r"^(?:will|does|can)\s+(.+?)\s+(?:win|beat)\b", text)
    if m:
        candidates.append(m.group(1))
    m = re.match(r"^(.+?)\s+to win\b", text)
    if m:
        candidates.append(m.group(1))
    for candidate in candidates:
        matched = [
            side for side, team in (("home", teams[0]), ("away", teams[1]))
            if bot.same_team(candidate, team)
        ]
        if len(matched) == 1:
            return matched[0]
    return None


def gamma_market_category(m, teams):
    """Exigir identificação inequívoca do desfecho; conflito => excluir.

    'groupItemTitle' é um rótulo de desfecho quando coincide exatamente
    com uma equipa ou 'Draw'. Se a pergunta indicar outro desfecho, não
    atribuimos o token a nenhum dos lados.
    """
    item = category(m.get("groupItemTitle"), teams)
    question = explicit_question_category(m.get("question"), teams)
    title = explicit_question_category(m.get("title"), teams)
    explicit = [value for value in (item, question, title) if value is not None]
    if not explicit:
        return None
    if len(set(explicit)) > 1:
        return None
    return explicit[0]


def unique_pick(picks, conflicts, label, asset, market, outcome,
                gamma_price=None, market_id=None):
    """Nunca escolher arbitrariamente entre dois tokens para o mesmo 1/X/2."""
    if label in conflicts:
        return
    item = {
        "asset": str(asset), "market": str(market),
        "outcome": str(outcome), "gamma_price": gamma_price,
        "market_id": str(market_id or ""),
    }
    previous = picks.get(label)
    if previous is not None and previous["asset"] != item["asset"]:
        picks.pop(label, None)
        conflicts.add(label)
        return
    picks[label] = item


def optional_gamma_probability(m, index):
    values = parse_list(m.get("outcomePrices"))
    try:
        p = float(values[index])
        return p if 0 < p < 1 and math.isfinite(p) else None
    except (ValueError, IndexError, TypeError):
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
            ambiguous = set()
            for m in event.get("markets") or []:
                if m.get("closed") or m.get("active") is False:
                    continue
                if not bot.is_poly_moneyline(m):
                    continue
                outcomes = parse_list(m.get("outcomes"))
                assets = parse_list(
                    m.get("positionIds") if m.get("version") == "v2"
                    else m.get("clobTokenIds")
                )
                if len(outcomes) != len(assets):
                    continue
                title = str(m.get("question") or m.get("title") or name)
                # Caso A: um único mercado 1X2 com três resultados explícitos.
                if len(outcomes) >= 3:
                    for idx, outcome in enumerate(outcomes):
                        label = category(outcome, teams)
                        if label:
                            unique_pick(
                                picks, ambiguous, label, assets[idx],
                                title, outcome,
                                optional_gamma_probability(m, idx),
                                m.get("id"),
                            )
                    continue
                # Caso B: três mercados binários separados, um para
                # equipa da casa, outro para empate e outro para visitante.
                label = gamma_market_category(m, teams)
                if label is None or len(outcomes) != 2:
                    continue
                if {bot.ascii_name(x) for x in outcomes} != {"yes", "no"}:
                    continue
                idx = next(
                    (i for i, x in enumerate(outcomes)
                     if bot.ascii_name(x) == "yes"), None
                )
                if idx is not None:
                    unique_pick(
                        picks, ambiguous, label, assets[idx], title,
                        outcomes[idx], optional_gamma_probability(m, idx),
                        m.get("id"),
                    )
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
            check = sx_1x2_side(
                {"outcomes": outcomes, "type": market.get("type")},
                teams,
            )
            if check is None:
                # Não podemos identificar a odd do resultado positivo
                # com garantias suficientes para comparar com 1X2.
                continue
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


def poly_site_price_estimate(bid, ask, last):
    """Regra publicada da Polymarket: midpoint ou último negócio.

    A UI do navegador pode estar noutro instante; isto é uma ESTIMATIVA
    baseada no MESMO snapshot CLOB do preço executável.
    """
    if (bid is not None and ask is not None
            and 0 < bid < 1 and 0 < ask < 1
            and 0 <= ask - bid <= 0.10):
        return (bid + ask) / 2, "MÉDIA (BID+ASK)/2"
    if last is not None and 0 < last < 1:
        return last, "ÚLTIMO NEGÓCIO"
    return None, "SEM PREÇO ESTIMÁVEL"


def poly_soccer_prices(poly, diagnostics=None):
    """Best ask é o preço de compra, não necessariamente o preço do site."""
    prices = {}
    for side, item in poly["picks"].items():
        try:
            if diagnostics is None:
                levels, _ = bot.poly_book(item["asset"])
                if levels:
                    prices[side] = 1.0 / min(p for p, capacity in levels)
                continue

            # No diagnóstico, consultar o livro inteiro UMA vez para cada
            # resultado e observar também melhor BID e preço Gamma.
            data = bot.get_json(
                bot.POLY_CLOB + "/book", {"token_id": item["asset"]},
                timeout=7,
            )
            if not isinstance(data, dict):
                continue
            asks = sorted(
                [float(x["price"]) for x in data.get("asks", [])
                 if 0 < float(x.get("price", 0)) < 1
                 and float(x.get("size", 0)) > 0]
            )
            bids = sorted(
                [float(x["price"]) for x in data.get("bids", [])
                 if 0 < float(x.get("price", 0)) < 1
                 and float(x.get("size", 0)) > 0]
            )
            ask = asks[0] if asks else None
            bid = bids[-1] if bids else None
            last = data.get("lastTradePrice")
            try:
                last = float(last)
                if not (0 < last < 1 and math.isfinite(last)):
                    last = None
            except (TypeError, ValueError):
                last = None
            # A Polymarket mostra o MID (BID/ASK) quando spread <= 10¢;
            # caso contrário pode usar o último negócio.
            indicative, display_source = poly_site_price_estimate(
                bid, ask, last
            )
            if ask:
                prices[side] = 1.0 / ask
            diagnostics[side] = {
                "token": item["asset"],
                "market_id": item.get("market_id", ""),
                "market": item["market"],
                "outcome": item.get("outcome", ""),
                "gamma": item.get("gamma_price"),
                "ask": ask,
                "bid": bid,
                "last": last,
                "site_indicative": indicative,
                "site_source": display_source,
                "book_timestamp": data.get("timestamp"),
                "read_utc": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            }
        except Exception as exc:
            if diagnostics is not None:
                diagnostics[side] = {
                    "error": type(exc).__name__,
                    "token": item["asset"],
                    "market": item["market"],
                }
    return prices


def sx_public_best_odds(market_hash, affirmative_index):
    """Endpoint opcional de melhores odds SX, independente da leitura do livro.

    Segundo a documentação SX, bestBettingOdds usa probabilidade x 10^12
    e índices de resultado 1/2. Se o formato não corresponder, omitir.
    """
    try:
        payload = bot.get_json(bot.SX_API + "/orders/odds/best", {
            "marketHash": market_hash
        }, timeout=7)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        back = data.get("bestBettingOdds") if isinstance(data, dict) else None
        if not isinstance(back, dict):
            return None
        number = back.get(str(affirmative_index+1))
        if number is None:
            number = back.get(affirmative_index+1)
        implied = float(number) / 1e12
        if not (0 < implied < 1 and math.isfinite(implied)):
            return None
        return 1 / implied
    except Exception:
        return None


def sx_soccer_prices(sx_markets, teams, diagnostics=None):
    prices = {}
    # Só mercados 1X2 explicitamente emparelhados com o seu 'Not X'.
    # Contratos duplicados ou 2-way não substituem as odds do 1X2.
    looked = 0
    for market in sx_markets:
        answer = sx_1x2_side(market, teams)
        if answer is None:
            continue
        side, index = answer
        if side in prices:
            continue
        try:
            levels, _ = bot.sx_book(market["hash"], index)
            looked += 1
            if levels:
                best_price = min(p for p, capacity in levels)
                prices[side] = 1.0 / best_price
                if diagnostics is not None:
                    diagnostics[side] = {
                        "hash": market["hash"],
                        "type": market.get("type"),
                        "outcomes": tuple(market["outcomes"]),
                        "positive_index": index,
                        "best_price": best_price,
                        "sx_public_best": sx_public_best_odds(market["hash"], index),
                        "read_utc": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                    }
        except Exception as exc:
            if diagnostics is not None:
                diagnostics[side] = {"error": type(exc).__name__,
                                     "hash": market["hash"]}
        if looked >= 6:
            return prices
    return prices


def diagnostics_message(poly_details, sx_details):
    """Detalhe técnico para verificar divergências sem misturar preço de site."""
    names = {"home": "1 · Casa", "draw": "X · Empate", "away": "2 · Fora"}
    lines = ["", "🔬 DIAGNÓSTICO DE ODDS (mesmo confronto)"]
    for side in ("home", "draw", "away"):
        info = poly_details.get(side) or {}
        if not info:
            continue
        lines.append(f"🔵 Poly {names[side]}:")
        if info.get("error"):
            lines.append(f"  Livro indisponível: {info['error']}")
            continue
        lines.append(
            "  BUY (ask): " +
            (f"{info['ask']*100:.2f}¢ → odd {single.format_odd(1/info['ask'])}"
             if info.get("ask") else "indisponível")
        )
        lines.append(
            "  BID (venda): " +
            (f"{info['bid']*100:.2f}¢" if info.get("bid") else "indisponível")
        )
        gamma = info.get("gamma")
        lines.append(
            "  Gamma (indicativo): " +
            (f"{gamma*100:.2f}¢" if gamma else "indisponível")
        )
        estimated = info.get("site_indicative")
        lines.append(
            "  Exibição site estimada: " +
            (f"{estimated*100:.2f}¢ → odd "
             f"{single.format_odd(1/estimated)} "
             f"({info.get('site_source', '')})" if estimated
             else "sem dados suficientes")
        )
        if info.get("last"):
            lines.append(f"  Último negócio: {info['last']*100:.2f}¢")
        if info.get("book_timestamp"):
            lines.append(f"  Timestamp CLOB: {info['book_timestamp']}")
        lines.append(f"  Token: {info.get('token', 'indisponível')}")
        lines.append(f"  Mercado: {info.get('market', 'indisponível')}")
        lines.append(f"  Lido: {info.get('read_utc', 'indisponível')}")
    for side in ("home", "draw", "away"):
        info = sx_details.get(side) or {}
        if not info:
            continue
        lines.append(f"🟣 SX {names[side]}:")
        if info.get("error"):
            lines.append(f"  Livro indisponível: {info['error']}")
            continue
        lines.append(
            "  BUY (taker) pelo livro: " +
            (single.format_odd(1/info["best_price"])
             if info.get("best_price") else "indisponível")
        )
        best = info.get("sx_public_best")
        lines.append(
            "  API SX melhor odd: " +
            (single.format_odd(best) if best else "indisponível")
        )
        lines.append(
            f"  Tipo: {info.get('type')} | "
            f"Outcome {info.get('positive_index')}"
        )
        lines.append(f"  Opostos: {info.get('outcomes')}")
        lines.append(f"  Hash: {info.get('hash')}")
        lines.append(f"  Lido: {info.get('read_utc', 'indisponível')}")
    lines.append("ℹ️ Gamma, preço do site e BUY/ASK podem divergir.")
    lines.append("🔒 Auditoria apenas. Zero apostas.")
    return "\n".join(lines)


def soccer_message(pair, poly_prices, sx_prices, index, count):
    poly, group = pair["poly"], pair["sx"]
    teams = poly["teams"]
    start = datetime.fromtimestamp(poly["start"], timezone.utc)
    stamp = datetime.now(timezone.utc)
    def format_source(price, available):
        if price is not None:
            return single.format_odd(price)
        if not available:
            return "⚠️ mercado não identificado"
        return "⚠️ sem preço de compra disponível"
    poly_available = set(poly["picks"])
    sx_available = {
        answer[0]
        for market in group
        if (answer := sx_1x2_side(market, teams)) is not None
    }
    sx_available.discard(None)
    def tab(prices, available):
        return "\n".join([
            f"🏠 {teams[0]}: {format_source(prices.get('home'), 'home' in available)}",
            f"🤝 Empate: {format_source(prices.get('draw'), 'draw' in available)}",
            f"🚌 {teams[1]}: {format_source(prices.get('away'), 'away' in available)}",
        ])
    sx_start = group[0]["start"]
    warning = (
        "\n⚠️ Horários Poly/SX diferentes — conferir evento"
        if abs(sx_start - poly["start"]) > 60 else ""
    )
    return "\n".join([
        f"⚽ AUDITORIA FUTEBOL {index}/{count} · SÓ LEITURA",
        f"🏟️ JOGO: {teams[0]} vs {teams[1]}",
        f"⏰ Início: {start:%d/%m %H:%M} UTC" + warning,
        f"🆔 Poly evento: {poly['id']}" + (
            f" | referência: {poly['slug']}" if poly.get("slug") else ""
        ),
        f"🆔 SX mercado: {group[0]['hash'][:12]}...",
        "",
        "1️⃣ POLYMARKET · odd de compra no livro",
        tab(poly_prices, poly_available),
        "",
        "2️⃣ SX BET · odd de compra no livro",
        tab(sx_prices, sx_available),
        "",
        f"🕒 Consulta: {stamp:%d/%m %H:%M:%S} UTC",
        "⚠️ CONFERE O MESMO ADVERSÁRIO NOS DOIS SITES.",
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


def run_soccer(count, conn, requested_match=None, recheck=False,
               explain=False):
    poly = poly_soccer_events()
    sx = sx_soccer_events()
    matches = soccer_matches(poly, sx)
    if requested_match:
        requested = bot.parse_title(requested_match)
        if requested is None:
            print("⚠️ Usa --match com formato 'Equipa A vs Equipa B'.")
            return 2
        matches = [
            pair for pair in matches
            if match_teams(*pair["poly"]["teams"], *requested)
        ]
    print(f"⚽ Jogos identificados nas DUAS plataformas: {len(matches)}")
    sent, examined = 0, 0
    for match in matches:
        key = "soccer:" + match["poly"]["id"]
        if was_sent(conn, key) and not recheck:
            continue
        examined += 1
        poly_details, sx_details = {}, {}
        pp = poly_soccer_prices(
            match["poly"], diagnostics=poly_details if explain else None
        )
        sp = sx_soccer_prices(
            match["sx"], match["poly"]["teams"],
            diagnostics=sx_details if explain else None
        )
        # Só enviar COMPARAÇÕES, não listas de uma só plataforma.
        if not pp or not sp:
            continue
        msg = soccer_message(match, pp, sp, sent+1, count)
        if explain:
            msg += diagnostics_message(poly_details, sx_details)
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


def previous_single_test_events():
    """Excluir também o confronto do teste isolado anterior, se existir."""
    path = Path(single.TEST_DB)
    if not path.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT poly_market,sx_market FROM test_odds_snapshots"
            ).fetchall()
            return {(str(p), str(s)) for p, s in rows}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return set()


def run_standard(sport, count, conn):
    previously_tested = previous_single_test_events()
    poly = bot.discover_poly()
    sx = bot.discover_sx()
    pairs = single.choose_pairs(poly, sx)
    sent, seen = 0, set()
    for pair in pairs:
        if pair["sx"]["sport"] != sport:
            continue
        key = "standard:" + str(pair["poly"]["id"]) + ":" + str(pair["sx"]["hash"])
        if (str(pair["poly"]["id"]), str(pair["sx"]["hash"])) in previously_tested:
            continue
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
    assert match_teams("Man City", "Fulham", "Manchester City", "Fulham")
    assert not match_teams("Man City", "Napoli", "Manchester City", "Fulham")
    assert category("Team Alpha", p["teams"]) == "home"
    assert category("Team Beta", p["teams"]) == "away"
    assert category("Draw", p["teams"]) == "draw"
    assert category("Not Team Alpha", p["teams"]) is None
    assert sx_1x2_side({
        "outcomes": ("Team Alpha", "Not Team Alpha"), "type": 52
    }, p["teams"]) == ("home", 0)
    assert sx_1x2_side({
        "outcomes": ("Not Team Alpha", "Team Alpha"), "type": 52
    }, p["teams"]) == ("home", 1)
    assert sx_1x2_side({
        "outcomes": ("Tie", "Not Tie"), "type": 52
    }, p["teams"]) == ("draw", 0)
    assert sx_1x2_side({
        "outcomes": ("Team Beta", "Not Team Beta"), "type": 52
    }, p["teams"]) == ("away", 0)
    assert sx_1x2_side({
        "outcomes": ("Team Alpha", "Team Beta"), "type": 52
    }, p["teams"]) is None
    assert sx_1x2_side({
        "outcomes": ("Team Alpha", "Not Team Alpha"), "type": 226
    }, p["teams"]) is None
    assert sx_1x2_side({
        "outcomes": ("Not Tie", "Team Beta"), "type": 52
    }, p["teams"]) is None
    assert category("Not Draw", p["teams"]) is None
    assert gamma_market_category(
        {"groupItemTitle": "Draw"}, p["teams"]
    ) == "draw"
    # Regressão com caso real: o preço do EMPATE não pode ser rotulado Fulham.
    football = ("Manchester City", "Fulham")
    assert gamma_market_category({
        "question": "Will Man City vs Fulham end in a draw?"
    }, football) == "draw"
    assert gamma_market_category({
        "groupItemTitle": "Fulham",
        "question": "Will Man City vs Fulham end in a draw?"
    }, football) is None
    assert gamma_market_category({
        "question": "Manchester City vs Fulham"
    }, football) is None
    assert gamma_market_category({
        "groupItemTitle": "Draw",
        "question": "Man City vs Fulham"
    }, football) == "draw"
    assert gamma_market_category({
        "groupItemTitle": "Fulham",
        "question": "Will Fulham win against Manchester City?"
    }, football) == "away"
    assert gamma_market_category({
        "groupItemTitle": "Manchester City",
        "question": "Will Manchester City win against Fulham?"
    }, football) == "home"
    assert gamma_market_category({
        "question": "Will Man City vs Fulham end in a draw?"
    }, football) != "away"

    assert poly_site_price_estimate(0.50, 0.54, 0.53)[0] == 0.52
    assert poly_site_price_estimate(0.25, 0.50, 0.44)[0] == 0.44
    assert poly_site_price_estimate(None, 0.50, None)[0] is None
    assert poly_site_price_estimate(0.50, 0.54, 0.53)[1].startswith("MÉDIA")
    # Chelsea/Leeds: o mercado 2-way hipotético com odd 1.44 NÃO pode
    # substituir a linha 1X2 Chelsea/Not Chelsea com odd 1.93.
    teams = ("Chelsea FC", "Leeds United FC")
    allowed = {
        "hash": "1x2", "type": 52,
        "outcomes": ("Chelsea", "Not Chelsea")
    }
    forbidden = {
        "hash": "two-way", "type": 226,
        "outcomes": ("Chelsea", "Leeds United")
    }
    assert sx_1x2_side(allowed, teams) == ("home", 0)
    assert sx_1x2_side(forbidden, teams) is None
    picks, collisions = {}, set()
    unique_pick(picks, collisions, "draw", "yes-draw", "market1", "Yes", 0.2)
    unique_pick(picks, collisions, "draw", "other-draw", "market2", "Yes", 0.3)
    assert "draw" in collisions and "draw" not in picks
    unique_pick(picks, collisions, "draw", "new-draw", "market3", "Yes", 0.3)
    assert "draw" not in picks
    assert category("Not Team Beta", p["teams"]) is None
    assert category("Team Alpha", p["teams"]) == "home"
    assert category("Team Beta", p["teams"]) == "away"
    msg = soccer_message(
        {"poly": p, "sx": sx},
        {"home": 2.1, "draw": 3.4}, {"home": 2.2},
        1, 3
    )
    assert "2,10" in msg and "2,20" in msg
    assert "Empate: 3,40" in msg
    assert "mercado não identificado" in msg
    assert "CONFERE O MESMO ADVERSÁRIO" in msg
    assert "Poly evento: soc-1" in msg
    assert "SX mercado: sx-1..." in msg
    forensic = diagnostics_message(
        {"home": {"token": "poly-token", "market": "Chelsea",
                  "gamma": 0.52, "ask": 0.54, "bid": 0.50,
                  "read_utc": "12:00:00 UTC"}},
        {"home": {"hash": "sx-hash", "type": 52,
                  "outcomes": ("Chelsea", "Not Chelsea"),
                  "positive_index": 0, "best_price": 0.52,
                  "read_utc": "12:00:01 UTC"}},
    )
    assert "BUY (ask): 54.00¢" in forensic
    assert "Gamma (indicativo): 52.00¢" in forensic
    assert "Token: poly-token" in forensic
    # O preço indicativo pode divergir do ASK.
    simulated = {
        "token": "abc", "market": "Chelsea", "gamma": 0.52,
        "bid": 0.50, "ask": 0.54, "last": 0.53,
        "site_indicative": 0.52, "site_source": "MÉDIA (BID+ASK)/2",
    }
    explained = diagnostics_message({"home": simulated}, {})
    assert "Exibição site estimada: 52.00¢" in explained
    assert "BUY (ask): 54.00¢" in explained
    assert "Token: poly-token" in forensic
    assert "BUY (taker) pelo livro: 1,92" in forensic
    assert "API SX melhor odd: indisponível" in forensic
    assert "NÃO significa" in msg
    assert "Sem apostas ou débitos" in msg
    assert isinstance(previous_single_test_events(), set)
    # Teste de ponta a ponta OFFLINE com respostas Gamma/SX sintéticas.
    # Nunca assume que dados simulados são odds do dia.
    old_get_json = bot.get_json
    old_poly_book = bot.poly_book
    old_sx_book = bot.sx_book
    try:
        def fake_get_json(url, params=None, timeout=12):
            if url.endswith("/sports") and "gamma" in url:
                return [{"sport": "soccer", "series": "123"}]
            if url.endswith("/events"):
                return [{
                    "id": "game-1", "title": "Team Alpha vs Team Beta",
                    "startTime": now + 3600,
                    "markets": [
                        {"sportsMarketType": "moneyline",
                         "outcomes": '["Yes","No"]',
                         "clobTokenIds": '["poly-home","other-home"]',
                         "groupItemTitle": "Team Alpha"},
                        {"sportsMarketType": "moneyline",
                         "outcomes": '["Yes","No"]',
                         "clobTokenIds": '["poly-draw","other-draw"]',
                         "groupItemTitle": "Draw"},
                        {"sportsMarketType": "moneyline",
                         "outcomes": '["Yes","No"]',
                         "clobTokenIds": '["poly-away","other-away"]',
                         "groupItemTitle": "Team Beta"},
                    ],
                }]
            if url.endswith("/sports") and "sx.bet" in url:
                return {"data": {"sports": [{"sportId": 5, "label": "Soccer"}]}}
            if url.endswith("/markets/active"):
                return {"data": {"markets": [{
                    "status": "ACTIVE",
                    "sportId": 5, "sportLabel": "Soccer",
                    "marketHash": "sx-fixture",
                    "gameTime": now + 3600,
                    "teamOneName": "Team Alpha",
                    "teamTwoName": "Team Beta",
                    "outcomeOneName": "Team Alpha",
                    "outcomeTwoName": "Not Team Alpha",
                }], "nextKey": None}}
            raise AssertionError("Unexpected HTTP URL " + url)

        bot.get_json = fake_get_json
        bot.poly_book = lambda asset: (
            [(0.41 if asset == "poly-home" else 0.30, 100)],
            time.monotonic(),
        )
        bot.sx_book = lambda market_hash, index: (
            [(0.42, 100)], time.monotonic(),
        )
        sample_poly = poly_soccer_events(now)
        sample_sx = sx_soccer_events(now)
        assert len(sample_poly) == 1 and len(sample_sx) == 1
        assert set(sample_poly[0]["picks"]) == {"home", "draw", "away"}
        sample_matches = soccer_matches(sample_poly, sample_sx, now)
        assert len(sample_matches) == 1
        pp = poly_soccer_prices(sample_poly[0])
        sp = sx_soccer_prices(sample_sx, sample_poly[0]["teams"])
        assert set(pp) == {"home", "draw", "away"}
        assert set(sp) == {"home"}
        assert sp["home"] > 2
        assert "Empate: " in soccer_message(sample_matches[0], pp, sp, 1, 3)
    finally:
        bot.get_json = old_get_json
        bot.poly_book = old_poly_book
        bot.sx_book = old_sx_book

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
    ap.add_argument("--match", type=str, default=None,
                    help="Futebol: filtrar confronto 'Man City vs Fulham'")
    ap.add_argument("--recheck", action="store_true",
                    help="Repetir um jogo já verificado sem alterar saldos")
    ap.add_argument("--explain-prices", action="store_true",
                    help="Anexar ASK, BID, Gamma, tokens e timestamps")
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
            return run_soccer(
                args.count, conn, requested_match=args.match,
                recheck=args.recheck, explain=args.explain_prices
            )
        if args.match:
            ap.error("--match só se aplica a --sport soccer")
        return run_standard(args.sport, args.count, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
