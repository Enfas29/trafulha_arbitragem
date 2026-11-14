import os
import re
import json
import time
import threading
import logging
import hashlib
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# ============================================================
# 1. CONFIGURACOES
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

BANKROLL_EUR = float(os.getenv("BANKROLL_EUR", "100"))
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "100"))
EUR_USD_RATE = float(os.getenv("EUR_USD_RATE", "1.08"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "1.0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "14400"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "2000"))
POLY_FEE_BUFFER_PCT = float(os.getenv("POLY_FEE_BUFFER_PCT", "1.0"))

BOOKMAKER_FILTER = [
    x.strip().lower()
    for x in os.getenv("BOOKMAKER_FILTER", "").split(",")
    if x.strip()
]

CUSTOM_SPORT_KEYS = [
    x.strip()
    for x in os.getenv("SPORT_KEYS", "").split(",")
    if x.strip()
]

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "10"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "300"))

ALERT_COOLDOWN = 3600
AUTO_BET = False

ODDS_BASE = "https://api.the-odds-api.com/v4"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"


# ============================================================
# 2. LOGGING E ESTADO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ArbitrageBot")

session = requests.Session()
session.headers.update({"User-Agent": "TrafulhaArbitrage/4.1"})

state_lock = threading.Lock()
scanner_status = {
    "running": True,
    "last_scan": None,
    "last_error": None,
    "events_checked": 0,
    "poly_games_checked": 0,
    "matched_games": 0,
    "best_margin_pct": None,
    "opportunities": 0,
    "sports": [],
    "mode": "HYBRID_POLY_BOOKMAKERS"
}

alert_history = {}


# ============================================================
# 3. UTILITARIOS HTTP E NORMALIZACAO DE NOMES
# ============================================================

def limpar_erro_url(texto):
    return re.sub(r"apiKey=[^&\s]+", "apiKey=***", str(texto))


def http_get(url, params=None, timeout=20):
    try:
        response = session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise requests.RequestException(limpar_erro_url(e)) from None


def converter_lista(valor):
    if isinstance(valor, str):
        try:
            return json.loads(valor)
        except json.JSONDecodeError:
            return []
    if isinstance(valor, list):
        return valor
    return []


def remover_acentos(texto):
    nfkd = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


STOPWORDS_EQUIPAS = {
    "fc", "cf", "sl", "sc", "ac", "as", "cd", "ud", "rc", "rcd", "afc",
    "club", "clube", "sporting", "de", "do", "da", "dos", "das", "the",
    "united", "city", "real", "atletico", "athletic", "saint", "st"
}

ALIASES_EQUIPAS = {
    "sporting lisbon": "sporting cp",
    "sporting clube de portugal": "sporting cp",
    "sl benfica": "benfica",
    "fc porto": "porto",
    "sc braga": "braga",
    "vitoria guimaraes": "vitoria sc",
    "paris saint germain": "psg",
    "paris saint-germain": "psg",
    "manchester united": "man united",
    "manchester city": "man city",
    "tottenham hotspur": "tottenham",
    "wolverhampton wanderers": "wolves",
    "inter milan": "inter",
    "internazionale": "inter",
    "ac milan": "milan",
    "atletico madrid": "atletico",
    "bayern munich": "bayern",
    "borussia dortmund": "dortmund",
    "los angeles lakers": "lakers",
    "la lakers": "lakers",
    "los angeles clippers": "clippers",
    "la clippers": "clippers",
    "golden state warriors": "warriors",
    "boston celtics": "celtics"
}


def normalizar_texto(nome):
    txt = remover_acentos(str(nome)).lower().strip()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    txt = " ".join(txt.split())
    return ALIASES_EQUIPAS.get(txt, txt)


def tokens_relevantes(nome):
    norm = normalizar_texto(nome)
    tokens = [t for t in norm.split() if t not in STOPWORDS_EQUIPAS and len(t) > 1]
    return set(tokens) if tokens else set(norm.split())


def equipas_coincidem(nome_a, nome_b):
    norm_a = normalizar_texto(nome_a)
    norm_b = normalizar_texto(nome_b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    if len(norm_a) >= 4 and len(norm_b) >= 4:
        if norm_a in norm_b or norm_b in norm_a:
            return True
    tok_a = tokens_relevantes(norm_a)
    tok_b = tokens_relevantes(norm_b)
    if not tok_a or not tok_b:
        return False
    intersecao = tok_a.intersection(tok_b)
    return len(intersecao) >= min(len(tok_a), len(tok_b))


def parse_iso_datetime(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    except ValueError:
        return None


def formatar_data_pt(dt_str):
    dt = parse_iso_datetime(dt_str)
    if not dt:
        return "Data por confirmar"
    return dt.strftime("%d/%m/%Y as %H:%M UTC")


# ============================================================
# 4. TELEGRAM
# ============================================================

def enviar_alerta_telegram(mensagem, alert_key=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado (TELEGRAM_TOKEN ou CHAT_ID em falta).")
        return False

    now = time.time()
    if alert_key:
        previous = alert_history.get(alert_key, 0)
        if now - previous < ALERT_COOLDOWN:
            return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "disable_web_page_preview": True
    }

    try:
        response = session.post(url, json=payload, timeout=15)
        if not response.ok:
            logger.error("Falha Telegram (%s): %s", response.status_code, response.text)
            return False
        if alert_key:
            alert_history[alert_key] = now
        logger.info("Alerta enviado para Telegram com sucesso!")
        return True
    except requests.RequestException as e:
        logger.error("Erro de rede Telegram: %s", e)
        return False


def gerar_chave_alerta(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ============================================================
# 5. THE ODDS API
# ============================================================

def descobrir_desportos():
    if not ODDS_API_KEY:
        return []
    if CUSTOM_SPORT_KEYS:
        return CUSTOM_SPORT_KEYS
    try:
        sports = http_get(f"{ODDS_BASE}/sports", params={"apiKey": ODDS_API_KEY})
        return [s["key"] for s in sports if s.get("active")][:6]
    except requests.RequestException as e:
        logger.error("Erro ao descobrir competicoes: %s", e)
        return []


def obter_odds_desporto(sport_key):
    if not ODDS_API_KEY:
        return []
    try:
        dados = http_get(
            f"{ODDS_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal"
            },
            timeout=25
        )
        logger.info("%s: %s eventos", sport_key, len(dados))
        return dados
    except requests.RequestException as e:
        logger.error("Erro em %s: %s", sport_key, e)
        return []


def extrair_melhores_odds_tradicionais(evento, casas_vistas):
    sport = evento.get("sport_key", "")
    home_raw = evento.get("home_team", "")
    away_raw = evento.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    is_soccer = sport.startswith("soccer_")
    melhores = {}

    for bookmaker in evento.get("bookmakers", []):
        bk_key = bookmaker.get("key", "").lower()
        bk_name = bookmaker.get("title", bk_key)
        casas_vistas.add(bk_key)

        if BOOKMAKER_FILTER and bk_key not in BOOKMAKER_FILTER:
            continue

        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):
                out_name = outcome.get("name", "")
                try:
                    odd = float(outcome.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if odd <= 1.0:
                    continue

                slot = None
                if normalizar_texto(out_name) == "draw":
                    slot = "draw"
                elif equipas_coincidem(out_name, home_raw):
                    slot = "home"
                elif equipas_coincidem(out_name, away_raw):
                    slot = "away"

                if not slot:
                    continue

                if slot not in melhores or odd > melhores[slot]["odd"]:
                    melhores[slot] = {
                        "odd": odd,
                        "bookmaker": bk_name,
                        "label": out_name
                    }

    if "home" not in melhores or "away" not in melhores:
        return None
    if is_soccer and "draw" not in melhores:
        return None

    return melhores


# ============================================================
# 6. POLYMARKET - RECOLHA DE JOGOS DESPORTIVOS
# ============================================================

SPORT_KEYWORDS = [
    " vs ", " vs. ", "nba", "euroleague", "premier league",
    "champions league", "la liga", "serie a", "bundesliga",
    "ligue 1", "primeira liga", "benfica", "porto", "sporting",
    "ufc", "mma", "atp", "wta", "tennis", "basketball", "soccer", "football"
]


def parece_evento_desportivo(texto):
    t = str(texto).lower()
    return any(k in t for k in SPORT_KEYWORDS)


def obter_melhor_ask_clob(token_id):
    try:
        book = http_get(
            f"{POLY_CLOB}/book",
            params={"token_id": token_id},
            timeout=12
        )
        asks = book.get("asks", [])
        validas = []
        for ask in asks:
            try:
                price = float(ask["price"])
                size = float(ask["size"])
            except (ValueError, TypeError, KeyError):
                continue
            if 0.01 < price < 0.99 and size > 0:
                validas.append({"price": price, "size": size})
        if not validas:
            return None
        return min(validas, key=lambda x: x["price"])
    except requests.RequestException:
        return None


def obter_jogos_polymarket():
    jogos_poly = []
    vistos = set()

    for page in range(MAX_POLY_PAGES):
        try:
            eventos = http_get(
                f"{POLY_GAMMA}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": 100,
                    "offset": page * 100
                },
                timeout=20
            )
        except requests.RequestException as e:
            logger.error("Erro ao ler eventos Polymarket: %s", e)
            break

        if not eventos:
            break

        for ev in eventos:
            ev_id = str(ev.get("id", ""))
            if ev_id in vistos:
                continue
            vistos.add(ev_id)

            titulo = ev.get("title", "")
            descricao = ev.get("description", "")
            if not parece_evento_desportivo(f"{titulo} {descricao}"):
                continue

            try:
                ev_volume = float(ev.get("volume", 0) or 0)
            except (ValueError, TypeError):
                ev_volume = 0

            mercados_ev = ev.get("markets", [])
            if not mercados_ev:
                continue

            submercados = []
            for m in mercados_ev:
                if m.get("closed") is True or m.get("active") is False:
                    continue

                try:
                    m_vol = float(m.get("volume", 0) or 0)
                except (ValueError, TypeError):
                    m_vol = 0

                volume_efetivo = max(ev_volume, m_vol)
                if volume_efetivo < MIN_POLY_VOLUME:
                    continue

                outcomes = converter_lista(m.get("outcomes"))
                token_ids = converter_lista(m.get("clobTokenIds"))
                prices_raw = converter_lista(m.get("outcomePrices"))

                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue

                prices = []
                for p in prices_raw:
                    try:
                        prices.append(float(p))
                    except (ValueError, TypeError):
                        prices.append(0.0)

                submercados.append({
                    "id": str(m.get("id", "")),
                    "question": m.get("question", titulo),
                    "group_title": m.get("groupItemTitle", ""),
                    "outcomes": outcomes,
                    "token_ids": token_ids,
                    "indicative_prices": prices if len(prices) == 2 else [0.0, 0.0],
                    "volume": volume_efetivo
                })

            if submercados:
                jogos_poly.append({
                    "event_id": ev_id,
                    "title": titulo,
                    "end_date": ev.get("endDate") or ev.get("startDate"),
                    "volume": ev_volume,
                    "markets": submercados
                })

            if len(jogos_poly) >= MAX_POLY_MARKETS:
                return jogos_poly

    return jogos_poly


# ============================================================
# 7. MOTOR DE CRUZAMENTO: POLYMARKET x CASAS DE APOSTAS
# ============================================================

def extrair_opcoes_poly_para_jogo(jogo_poly, home_team, away_team, is_soccer):
    opcoes = {}

    for m in jogo_poly["markets"]:
        q = m["question"]
        g = m["group_title"]
        outs = m["outcomes"]
        tids = m["token_ids"]
        iprices = m["indicative_prices"]
        vol = m["volume"]

        outs_norm = [normalizar_texto(o) for o in outs]

        if "yes" not in outs_norm and "no" not in outs_norm:
            for idx, out_name in enumerate(outs):
                if equipas_coincidem(out_name, home_team):
                    opcoes["home"] = {
                        "token_id": tids[idx],
                        "indicative_price": iprices[idx],
                        "volume": vol,
                        "label": f"{home_team} Vence"
                    }
                elif equipas_coincidem(out_name, away_team):
                    opcoes["away"] = {
                        "token_id": tids[idx],
                        "indicative_price": iprices[idx],
                        "volume": vol,
                        "label": f"{away_team} Vence"
                    }

        elif "yes" in outs_norm:
            yes_idx = outs_norm.index("yes")
            alvo_texto = g if g else q

            if any(w in alvo_texto.lower() for w in ["spread", "over", "under", "total", "goals", "points", "handicap", "o/u"]):
                continue

            if is_soccer and ("draw" in normalizar_texto(alvo_texto) or "tie" in normalizar_texto(alvo_texto)):
                opcoes["draw"] = {
                    "token_id": tids[yes_idx],
                    "indicative_price": iprices[yes_idx],
                    "volume": vol,
                    "label": "Empate"
                }
            elif equipas_coincidem(alvo_texto, home_team) and not equipas_coincidem(alvo_texto, away_team):
                opcoes["home"] = {
                    "token_id": tids[yes_idx],
                    "indicative_price": iprices[yes_idx],
                    "volume": vol,
                    "label": f"{home_team} Vence"
                }
            elif equipas_coincidem(alvo_texto, away_team) and not equipas_coincidem(alvo_texto, home_team):
                opcoes["away"] = {
                    "token_id": tids[yes_idx],
                    "indicative_price": iprices[yes_idx],
                    "volume": vol,
                    "label": f"{away_team} Vence"
                }

    return opcoes


def calcular_odd_liquida_poly(ask_price):
    fator_taxa = 1.0 + (POLY_FEE_BUFFER_PCT / 100.0)
    preco_com_taxa = ask_price * fator_taxa
    if preco_com_taxa >= 1.0:
        return None
    return 1.0 / preco_com_taxa


def cruzar_evento_hibrido(evento, jogos_poly, stats):
    home = evento.get("home_team", "")
    away = evento.get("away_team", "")
    sport = evento.get("sport_key", "")
    commence_time = evento.get("commence_time", "")
    is_soccer = sport.startswith("soccer_")

    melhores_trad = extrair_melhores_odds_tradicionais(evento, stats["casas_vistas"])
    if not melhores_trad:
        return False

    # Limite maximo de soma de probabilidades ajustado ao MIN_PROFIT_PCT configurado
    fator_min = max(0.5, 1.0 + (MIN_PROFIT_PCT / 100.0))
    max_soma_prob = 1.0 / fator_min
    limite_prefiltro = max_soma_prob + 0.03

    dt_evento = parse_iso_datetime(commence_time)
    alertas_enviados = 0

    for jogo_poly in jogos_poly:
        titulo_poly = jogo_poly["title"]
        if not (equipas_coincidem(home, titulo_poly) and equipas_coincidem(away, titulo_poly)):
            continue

        dt_poly = parse_iso_datetime(jogo_poly.get("end_date"))
        if dt_evento and dt_poly:
            diff_horas = abs((dt_poly - dt_evento).total_seconds()) / 3600.0
            if diff_horas > 120:
                continue

        opcoes_poly = extrair_opcoes_poly_para_jogo(jogo_poly, home, away, is_soccer)
        if not opcoes_poly:
            continue

        stats["jogos_casados"] += 1

        # CENARIO A: DESPORTOS 2-WAY (NBA, EUROLIGA, MMA, TENIS)
        if not is_soccer:
            pares = [
                ("home", "away", f"{home} Vence", f"{away} Vence"),
                ("away", "home", f"{away} Vence", f"{home} Vence")
            ]

            for lado_poly, lado_trad, label_poly, label_trad in pares:
                if lado_poly not in opcoes_poly or lado_trad not in melhores_trad:
                    continue

                opt_p = opcoes_poly[lado_poly]
                opt_t = melhores_trad[lado_trad]

                if opt_p["indicative_price"] > 0:
                    odd_ind = calcular_odd_liquida_poly(opt_p["indicative_price"])
                    if odd_ind and (1.0 / odd_ind + 1.0 / opt_t["odd"]) > limite_prefiltro:
                        continue

                ask = obter_melhor_ask_clob(opt_p["token_id"])
                if not ask:
                    continue

                odd_poly = calcular_odd_liquida_poly(ask["price"])
                if not odd_poly:
                    continue

                odd_trad = opt_t["odd"]
                soma_prob = (1.0 / odd_poly) + (1.0 / odd_trad)
                lucro_pct = ((1.0 / soma_prob) - 1.0) * 100.0

                if stats["melhor_margem"] is None or lucro_pct > stats["melhor_margem"]:
                    stats["melhor_margem"] = lucro_pct

                if lucro_pct < MIN_PROFIT_PCT:
                    continue

                stake_poly_eur = BANKROLL_EUR * (1.0 / odd_poly) / soma_prob
                stake_trad_eur = BANKROLL_EUR * (1.0 / odd_trad) / soma_prob
                stake_poly_usd = stake_poly_eur * EUR_USD_RATE

                liquidez_usd = ask["size"] * ask["price"]
                if liquidez_usd < 10.0:
                    continue

                retorno_eur = BANKROLL_EUR / soma_prob
                lucro_eur = retorno_eur - BANKROLL_EUR

                mensagem = (
                    "Oportunidade de Arbitragem Detetada!\n\n"
                    f"Jogo: {home} vs {away}\n"
                    f"Data e Hora: {formatar_data_pt(commence_time)}\n"
                    f"Volume Polymarket: ${opt_p['volume']:,.2f} (Valido > ${MIN_POLY_VOLUME:,.0f})\n\n"
                    f"Lado 1 (Polymarket):\n"
                    f"- Aposta: {label_poly}\n"
                    f"- Odd Liquida: {odd_poly:.2f} (Preco: ${ask['price']:.3f})\n"
                    f"- Valor a colocar: {stake_poly_eur:.2f} EUR (~${stake_poly_usd:.2f} USD)\n\n"
                    f"Lado 2 ({opt_t['bookmaker']}):\n"
                    f"- Aposta: {label_trad}\n"
                    f"- Odd: {odd_trad:.2f}\n"
                    f"- Valor a colocar: {stake_trad_eur:.2f} EUR\n\n"
                    f"Investimento Total: {BANKROLL_EUR:.2f} EUR\n"
                    f"Retorno Garantido: ~{retorno_eur:.2f} EUR\n"
                    f"Lucro Liquido: {lucro_eur:+.2f} EUR (~{lucro_pct:.2f}%)"
                )

                key = gerar_chave_alerta(f"hyb2_{evento.get('id')}_{lado_poly}_{round(odd_poly,2)}_{round(odd_trad,2)}")
                if enviar_alerta_telegram(mensagem, key):
                    alertas_enviados += 1

        # CENARIO B: FUTEBOL (POLYMARKET x HIPOTESE DUPLA)
        else:
            combinacoes_futebol = [
                ("home", "draw", "away", f"{home} Vence", f"{away} ou Empate (Hipotese Dupla)"),
                ("away", "draw", "home", f"{away} Vence", f"{home} ou Empate (Hipotese Dupla)"),
                ("draw", "home", "away", "Empate", f"{home} ou {away} (Hipotese Dupla 12)")
            ]

            for lado_poly, t1, t2, label_poly, label_dupla in combinacoes_futebol:
                if lado_poly not in opcoes_poly:
                    continue
                if t1 not in melhores_trad or t2 not in melhores_trad:
                    continue

                opt_p = opcoes_poly[lado_poly]
                odd_t1 = melhores_trad[t1]["odd"]
                odd_t2 = melhores_trad[t2]["odd"]
                bk_t1 = melhores_trad[t1]["bookmaker"]
                bk_t2 = melhores_trad[t2]["bookmaker"]

                odd_dupla = 1.0 / ((1.0 / odd_t1) + (1.0 / odd_t2))

                if opt_p["indicative_price"] > 0:
                    odd_ind = calcular_odd_liquida_poly(opt_p["indicative_price"])
                    if odd_ind and (1.0 / odd_ind + 1.0 / odd_dupla) > limite_prefiltro:
                        continue

                ask = obter_melhor_ask_clob(opt_p["token_id"])
                if not ask:
                    continue

                odd_poly = calcular_odd_liquida_poly(ask["price"])
                if not odd_poly:
                    continue

                soma_prob = (1.0 / odd_poly) + (1.0 / odd_t1) + (1.0 / odd_t2)
                lucro_pct = ((1.0 / soma_prob) - 1.0) * 100.0

                if stats["melhor_margem"] is None or lucro_pct > stats["melhor_margem"]:
                    stats["melhor_margem"] = lucro_pct

                if lucro_pct < MIN_PROFIT_PCT:
                    continue

                liquidez_usd = ask["size"] * ask["price"]
                if liquidez_usd < 10.0:
                    continue

                stake_poly_eur = BANKROLL_EUR * (1.0 / odd_poly) / soma_prob
                stake_t1_eur = BANKROLL_EUR * (1.0 / odd_t1) / soma_prob
                stake_t2_eur = BANKROLL_EUR * (1.0 / odd_t2) / soma_prob
                stake_poly_usd = stake_poly_eur * EUR_USD_RATE

                retorno_eur = BANKROLL_EUR / soma_prob
                lucro_eur = retorno_eur - BANKROLL_EUR

                casas_lado2 = bk_t1 if bk_t1 == bk_t2 else f"{bk_t1} / {bk_t2}"

                mensagem = (
                    "Oportunidade de Arbitragem Detetada!\n\n"
                    f"Jogo: {home} vs {away}\n"
                    f"Data e Hora: {formatar_data_pt(commence_time)}\n"
                    f"Volume Polymarket: ${opt_p['volume']:,.2f} (Valido > ${MIN_POLY_VOLUME:,.0f})\n\n"
                    f"Lado 1 (Polymarket):\n"
                    f"- Aposta: {label_poly}\n"
                    f"- Odd Liquida: {odd_poly:.2f} (Preco: ${ask['price']:.3f})\n"
                    f"- Valor a colocar: {stake_poly_eur:.2f} EUR (~${stake_poly_usd:.2f} USD)\n\n"
                    f"Lado 2 ({casas_lado2}):\n"
                    f"- Aposta: {label_dupla}\n"
                    f"- Odd Combinada: {odd_dupla:.2f}\n"
                    f"  * {melhores_trad[t1]['label']} ({bk_t1}): {stake_t1_eur:.2f} EUR @ {odd_t1:.2f}\n"
                    f"  * {melhores_trad[t2]['label']} ({bk_t2}): {stake_t2_eur:.2f} EUR @ {odd_t2:.2f}\n\n"
                    f"Investimento Total: {BANKROLL_EUR:.2f} EUR\n"
                    f"Retorno Garantido: ~{retorno_eur:.2f} EUR\n"
                    f"Lucro Liquido: {lucro_eur:+.2f} EUR (~{lucro_pct:.2f}%)"
                )

                key = gerar_chave_alerta(f"hyb3_{evento.get('id')}_{lado_poly}_{round(odd_poly,2)}_{round(odd_dupla,2)}")
                if enviar_alerta_telegram(mensagem, key):
                    alertas_enviados += 1

    return alertas_enviados > 0


# ============================================================
# 8. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():
    logger.info("A iniciar scanner Hibrido V4.1 (MIN_PROFIT_PCT = %s%%).", MIN_PROFIT_PCT)

    total_eventos = 0
    total_alertas = 0
    stats = {
        "casas_vistas": set(),
        "jogos_casados": 0,
        "melhor_margem": None
    }

    jogos_poly = obter_jogos_polymarket()
    logger.info("Polymarket: %s eventos desportivos carregados.", len(jogos_poly))

    sports = descobrir_desportos()
    with state_lock:
        scanner_status["sports"] = sports

    for sport in sports:
        eventos = obter_odds_desporto(sport)
        total_eventos += len(eventos)

        for evento in eventos:
            evento["sport_key"] = sport
            try:
                if cruzar_evento_hibrido(evento, jogos_poly, stats):
                    total_alertas += 1
            except Exception:
                logger.exception("Erro ao analisar evento.")

    melhor_str = f"{stats['melhor_margem']:.2f}%" if stats["melhor_margem"] is not None else "N/A"
    logger.info(
        "Diagnostico | Casas API: %s | Jogos casados: %s | Melhor margem: %s",
        sorted(list(stats["casas_vistas"]))[:10],
        stats["jogos_casados"],
        melhor_str
    )

    with state_lock:
        scanner_status["last_scan"] = datetime.now(timezone.utc).isoformat()
        scanner_status["events_checked"] = total_eventos
        scanner_status["poly_games_checked"] = len(jogos_poly)
        scanner_status["matched_games"] = stats["jogos_casados"]
        scanner_status["best_margin_pct"] = stats["melhor_margem"]
        scanner_status["opportunities"] += total_alertas
        scanner_status["last_error"] = None

    logger.info(
        "Ciclo terminado | Eventos API: %s | Jogos Polymarket: %s | Alertas: %s",
        total_eventos,
        len(jogos_poly),
        total_alertas
    )


def correr_robo_continuo():
    while True:
        try:
            ciclo_scanner()
        except Exception as e:
            logger.exception("Erro geral do scanner.")
            with state_lock:
                scanner_status["last_error"] = limpar_erro_url(e)
        time.sleep(SCAN_INTERVAL)


# ============================================================
# 9. SERVIDOR WEB RENDER
# ============================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            with state_lock:
                data = dict(scanner_status)
            data["auto_bet"] = AUTO_BET
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = (
                "ROBO HIBRIDO V4.1 ONLINE\n"
                "Cruzamento: Polymarket x Casas Europeias\n"
                "Mode: SCAN ONLY\n"
                "Health: /health\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_web():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    logger.info("Servidor ativo na porta %s", port)
    server.serve_forever()


if __name__ == "__main__":
    if not ODDS_API_KEY:
        logger.warning("ODDS_API_KEY nao configurada.")
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")

    scanner_thread = threading.Thread(target=correr_robo_continuo, daemon=True)
    scanner_thread.start()
    iniciar_servidor_web()
