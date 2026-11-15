
import os
import re
import json
import time
import threading
import logging
import unicodedata
import difflib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import websocket


# ============================================================
# PAPER TRADING V10.0 - ULTRA SMART MATCH
# MODO EXCLUSIVO DE SIMULACAO
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
SX_API_KEY = os.getenv("SX_API_KEY", "").strip()
POLY_API_KEY = os.getenv("POLY_API_KEY", "").strip()

PAPER_INITIAL_BANKROLL_POLY = float(
    os.getenv("PAPER_INITIAL_BANKROLL_POLY", "1000")
)
PAPER_INITIAL_BANKROLL_SX = float(
    os.getenv("PAPER_INITIAL_BANKROLL_SX", "1000")
)
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "-3.0"))
SCAN_INTERVAL = max(5, int(os.getenv("SCAN_INTERVAL", "15")))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "100"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "15"))

POLY_FEE_BUFFER_PCT = float(os.getenv("POLY_FEE_BUFFER_PCT", "1.0"))
SX_FEE_BUFFER_PCT = float(os.getenv("SX_FEE_BUFFER_PCT", "0.0"))

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "15"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "400"))
MAX_MATCH_LOGS = int(os.getenv("MAX_MATCH_LOGS", "30"))

AUTO_BET = False

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
POLY_CLOB_WS = (
    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)
SX_BASE = "https://api.sx.bet"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("PaperTradingV10")

session = requests.Session()

session.headers.update({
    "User-Agent": "PaperTradingV10/1.0",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
})

if SX_API_KEY:
    session.headers["x-sx-api-key"] = SX_API_KEY

if POLY_API_KEY:
    session.headers["POLY_API_KEY"] = POLY_API_KEY


# ============================================================
# ESTADO GLOBAL
# ============================================================

state_lock = threading.RLock()
wallet_lock = threading.Lock()
orderbook_lock = threading.Lock()
ws_lock = threading.Lock()

live_orderbooks = {}
subscribed_tokens = set()
ws_app = None

paper_wallet = {
    "poly": {
        "initial": PAPER_INITIAL_BANKROLL_POLY,
        "available": PAPER_INITIAL_BANKROLL_POLY,
        "invested": 0.0,
        "profit": 0.0
    },
    "sx": {
        "initial": PAPER_INITIAL_BANKROLL_SX,
        "available": PAPER_INITIAL_BANKROLL_SX,
        "invested": 0.0,
        "profit": 0.0
    },
    "bets_placed": 0,
    "history": [],
    "apostas_realizadas_hashes": set()
}

scanner_status = {
    "running": True,
    "last_scan": None,
    "last_error": None,
    "poly_events_found": 0,
    "poly_markets_found": 0,
    "sx_markets_found": 0,
    "matched_games": 0,
    "unmatched_sx_markets": 0,
    "alerts_sent": 0,
    "ws_connected": False,
    "mode": "PAPER_TRADING_V10.0"
}


# ============================================================
# 1. MATCHING INTELIGENTE DE EQUIPAS
# ============================================================

def remover_acentos(texto):
    nfkd = unicodedata.normalize("NFKD", str(texto))

    return "".join(
        c for c in nfkd
        if not unicodedata.combining(c)
    )


def normalizar_texto(nome):
    txt = remover_acentos(nome).lower().strip()
    txt = txt.replace("&", " and ")
    txt = re.sub(r"[^a-z0-9]+", " ", txt)

    return " ".join(txt.split())


ALIASES_EQUIPAS = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "man u": "manchester united",
    "spurs": "tottenham hotspur",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "atleti": "atletico madrid",
    "atletico": "atletico madrid",
    "bayern": "bayern munich",
    "sporting": "sporting cp",
    "porto": "fc porto",
    "benfica": "sl benfica",
    "barca": "barcelona",
    "juve": "juventus",
    "ajax": "ajax amsterdam",
    "roma": "as roma",
    "napoli": "ssc napoli",
    "wolves": "wolverhampton wanderers",
    "newcastle": "newcastle united",
    "west ham": "west ham united",
    "athletic": "athletic bilbao"
}


GENERIC_TEAM_WORDS = {
    "fc", "cf", "sc", "ac", "afc",
    "club", "clube", "football", "futebol",
    "the", "team", "basketball",
    "women", "woman", "womens",
    "u18", "u19", "u20", "u21", "u23",
    "reserves"
}


def normalizar_equipa(nome):
    txt = normalizar_texto(nome)

    if not txt:
        return ""

    txt = ALIASES_EQUIPAS.get(txt, txt)

    words = [
        w for w in txt.split()
        if w not in GENERIC_TEAM_WORDS
    ]

    return " ".join(words)


def equipas_coincidem(nome_a, nome_b):

    a = normalizar_equipa(nome_a)
    b = normalizar_equipa(nome_b)

    if not a or not b:
        return False

    if a == b:
        return True

    wa = set(a.split())
    wb = set(b.split())

    comuns = wa & wb

    if comuns:

        for word in comuns:

            if len(word) >= 5:

                if len(wa) == 1 or len(wb) == 1:
                    return True

        if len(comuns) >= 2:
            return True

    ratio = difflib.SequenceMatcher(
        None, a, b
    ).ratio()

    if min(len(a), len(b)) >= 8 and ratio >= 0.88:
        return True

    return False


def separar_equipas_titulo(titulo):

    txt = str(titulo or "").strip()

    patterns = [
        r"\s+vs\.?\s+",
        r"\s+v\.?\s+",
        r"\s+@\s+",
        r"\s+at\s+"
    ]

    for pattern in patterns:

        parts = re.split(
            pattern,
            txt,
            maxsplit=1,
            flags=re.IGNORECASE
        )

        if len(parts) == 2:

            left = parts[0].strip()
            right = parts[1].strip()

            if left and right:
                return left, right

    return None


def jogo_coincide_com_titulo(
    team1,
    team2,
    titulo_poly
):

    parts = separar_equipas_titulo(titulo_poly)

    if not parts:
        return False

    a, b = parts

    direct = (
        equipas_coincidem(team1, a)
        and equipas_coincidem(team2, b)
    )

    reverse = (
        equipas_coincidem(team1, b)
        and equipas_coincidem(team2, a)
    )

    return direct or reverse


# ============================================================
# 2. HTTP E TELEGRAM
# ============================================================

def http_get(url, params=None, timeout=20):

    response = session.get(
        url,
        params=params,
        timeout=timeout
    )

    response.raise_for_status()

    return response.json()


def converter_lista(value):

    if isinstance(value, str):

        try:
            decoded = json.loads(value)

            return decoded if isinstance(decoded, list) else []

        except (json.JSONDecodeError, TypeError):
            return []

    return value if isinstance(value, list) else []


def enviar_alerta_telegram(mensagem):

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.error(
            "Telegram não configurado: falta TOKEN ou CHAT_ID."
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = session.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": mensagem,
                "disable_web_page_preview": True
            },
            timeout=15
        )

        if not response.ok:

            logger.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:500]
            )

            return False

        payload = response.json()

        if not payload.get("ok"):

            logger.error(
                "Telegram recusou a mensagem: %s",
                payload
            )

            return False

        logger.info("Alerta enviado ao Telegram.")

        return True

    except requests.RequestException:

        logger.exception(
            "Falha de ligação ao Telegram."
        )

        return False


def testar_telegram():

    if not TELEGRAM_TOKEN or not CHAT_ID:

        logger.warning(
            "Telegram não testado: configura TOKEN e CHAT_ID."
        )

        return

    enviar_alerta_telegram(
        "🟢 PAPER TRADING V10.0 ONLINE\n"
        "Scanner iniciado em modo de simulação.\n"
        "Nenhuma aposta real será realizada."
    )


# ============================================================
# 3. WEBSOCKET POLYMARKET
# ============================================================

def atualizar_orderbook_cache(token_id, asks, bids=None):

    valid_asks = []

    for level in asks or []:

        try:

            price = float(level.get("price"))
            size = float(level.get("size"))

            if 0 < price < 1 and size > 0:

                valid_asks.append({
                    "price": price,
                    "size": size
                })

        except (TypeError, ValueError, AttributeError):
            continue

    if not valid_asks:
        return

    best = min(
        valid_asks,
        key=lambda x: x["price"]
    )

    liquidity = sum(
        x["price"] * x["size"]
        for x in valid_asks
    )

    with orderbook_lock:

        live_orderbooks[str(token_id)] = {
            "price": best["price"],
            "size": best["size"],
            "liquidity_usd": liquidity,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "websocket"
        }


def processar_mensagem_ws(ws, message):

    try:

        payload = json.loads(message)

        items = (
            payload
            if isinstance(payload, list)
            else [payload]
        )

        for item in items:

            if not isinstance(item, dict):
                continue

            event_type = (
                item.get("event_type")
                or item.get("type")
            )

            if (
                event_type == "book"
                or (
                    "asks" in item
                    and "asset_id" in item
                )
            ):

                token_id = (
                    item.get("asset_id")
                    or item.get("market")
                )

                if token_id:

                    atualizar_orderbook_cache(
                        token_id,
                        item.get("asks", []),
                        item.get("bids", [])
                    )

    except Exception:

        logger.exception(
            "Erro ao interpretar mensagem WebSocket."
        )


def ao_abrir_ws(ws):

    logger.info(
        "WebSocket Polymarket ligado."
    )

    with state_lock:
        scanner_status["ws_connected"] = True

    with ws_lock:
        tokens = list(subscribed_tokens)

    if tokens:

        try:

            ws.send(json.dumps({
                "type": "market",
                "assets_ids": tokens,
                "custom_feature_enabled": True
            }))

            logger.info(
                "WebSocket subscrito a %d tokens.",
                len(tokens)
            )

        except Exception:

            logger.exception(
                "Falha ao subscrever tokens."
            )


def ao_fechar_ws(
    ws,
    close_status_code,
    close_msg
):

    logger.warning(
        "WebSocket fechado. Código=%s Motivo=%s",
        close_status_code,
        close_msg
    )

    with state_lock:
        scanner_status["ws_connected"] = False


def ao_erro_ws(ws, error):

    logger.error(
        "WebSocket Polymarket: %s",
        error
    )


def subscrever_tokens_poly(token_ids):

    novos = []

    with ws_lock:

        for token in token_ids:

            token = str(token)

            if token and token not in subscribed_tokens:

                subscribed_tokens.add(token)
                novos.append(token)

    if not novos:
        return

    current_ws = ws_app

    if current_ws and scanner_status.get("ws_connected"):

        try:

            current_ws.send(json.dumps({
                "assets_ids": novos,
                "operation": "subscribe"
            }))

            logger.info(
                "Subscrição incremental: %d tokens.",
                len(novos)
            )

        except Exception:

            logger.exception(
                "Não foi possível subscrever novos tokens."
            )


def correr_websocket_poly():

    global ws_app

    while True:

        try:

            app = websocket.WebSocketApp(
                POLY_CLOB_WS,
                on_open=ao_abrir_ws,
                on_message=processar_mensagem_ws,
                on_close=ao_fechar_ws,
                on_error=ao_erro_ws
            )

            ws_app = app

            app.run_forever(
                ping_interval=20,
                ping_timeout=10
            )

        except Exception:

            logger.exception(
                "Erro no ciclo WebSocket."
            )

        with state_lock:
            scanner_status["ws_connected"] = False

        time.sleep(5)

# ============================================================
# POLYMARKET — RECOLHA DE EVENTOS E MERCADOS
# ============================================================

def parece_evento_desportivo(texto):
    value = f" {str(texto).lower()} "
    return any(keyword in value for keyword in SPORT_KEYWORDS)


def obter_jogos_polymarket():
    jogos = []
    vistos = set()
    total_markets = 0

    for page in range(MAX_POLY_PAGES):
        try:
            events = http_get(
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
        except Exception:
            logger.exception("Falha a obter eventos Gamma (página %d).", page)
            break

        if not isinstance(events, list) or not events:
            break

        for event in events:
            event_id = str(event.get("id", ""))
            if event_id in vistos:
                continue
            vistos.add(event_id)

            title = event.get("title", "")
            description = event.get("description", "")

            if not parece_evento_desportivo(f"{title} {description}"):
                continue

            submarkets = []

            for market in event.get("markets", []) or []:
                if market.get("closed") is True or market.get("active") is False:
                    continue

                outcomes = converter_lista(market.get("outcomes"))
                token_ids = converter_lista(market.get("clobTokenIds"))

                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue

                submarkets.append({
                    "id": str(market.get("id", "")),
                    "question": market.get("question") or title,
                    "outcomes": outcomes,
                    "token_ids": [str(x) for x in token_ids],
                    "volume": float(
                        market.get("volumeNum") or market.get("volume") or 0
                    )
                })

                total_markets += 1

            if submarkets:
                jogos.append({
                    "event_id": event_id,
                    "title": title,
                    "markets": submarkets
                })

            if total_markets >= MAX_POLY_MARKETS:
                with state_lock:
                    scanner_status["poly_events_found"] = len(jogos)
                    scanner_status["poly_markets_found"] = total_markets
                return jogos

    with state_lock:
        scanner_status["poly_events_found"] = len(jogos)
        scanner_status["poly_markets_found"] = total_markets

    logger.info(
        "Polymarket: %d eventos desportivos, %d mercados binários.",
        len(jogos), total_markets
    )

    return jogos


# ============================================================
# SX BET — RECOLHA DE MERCADOS
# ============================================================

def obter_mercados_sx():
    markets = []
    next_key = None

    for _ in range(8):
        params = {"onlyMainLine": "true"}

        if next_key:
            params["paginationKey"] = next_key

        try:
            response = http_get(
                f"{SX_BASE}/markets/active",
                params=params,
                timeout=20
            )
        except Exception:
            logger.exception("Falha a obter mercados SX Bet.")
            break

        data = response.get("data", {}) if isinstance(response, dict) else {}
        listing = data.get("markets", []) if isinstance(data, dict) else []

        if not listing:
            break

        for market in listing:
            if market.get("status") != "ACTIVE":
                continue

            if market.get("type") not in (1, 52, 226):
                continue

            team1 = market.get("teamOneName", "")
            team2 = market.get("teamTwoName", "")
            outcome1 = market.get("outcomeOneName", "")
            outcome2 = market.get("outcomeTwoName", "")
            market_hash = market.get("marketHash", "")

            if not all([team1, team2, outcome1, outcome2, market_hash]):
                continue

            markets.append({
                "market_hash": market_hash,
                "team_one": team1,
                "team_two": team2,
                "outcome_one": outcome1,
                "outcome_two": outcome2,
                "sport": market.get("sportLabel", ""),
                "league": market.get("leagueLabel", "")
            })

        next_key = data.get("nextKey")

        if not next_key:
            break

    with state_lock:
        scanner_status["sx_markets_found"] = len(markets)

    logger.info("SX Bet: %d mercados ativos elegíveis.", len(markets))
    return markets


# ============================================================
# SX BET — CONVERSÃO DOS NÍVEIS DE ODDS
# ============================================================

def extrair_niveis_lado_sx(raw_side):
    items = [raw_side] if isinstance(raw_side, dict) else (
        raw_side if isinstance(raw_side, list) else []
    )

    levels = []
    fee_factor = 1.0 - (SX_FEE_BUFFER_PCT / 100.0)

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_odds = item.get("percentageOdds")
        raw_size = item.get("size")

        if raw_size is None:
            raw_size = item.get("totalBetSize")

        if raw_odds is None or raw_size is None:
            continue

        try:
            maker_prob = float(raw_odds) / 1e20
            maker_stake = float(raw_size) / 1e6
        except (TypeError, ValueError):
            continue

        if not (0.01 < maker_prob < 0.99) or maker_stake <= 0:
            continue

        taker_prob = 1.0 - maker_prob
        taker_odd = 1.0 + (
            (1.0 / taker_prob) - 1.0
        ) * fee_factor

        taker_liquidity = maker_stake * (taker_prob / maker_prob)

        if taker_liquidity > 0:
            levels.append({
                "odd": taker_odd,
                "liquidity_usdc": taker_liquidity
            })

    levels.sort(key=lambda item: item["odd"], reverse=True)
    return levels


def registar_livro_sx(market_hash, block, best_by_market):
    if not market_hash or not isinstance(block, dict):
        return

    side_one = extrair_niveis_lado_sx(block.get("outcomeTwo"))
    side_two = extrair_niveis_lado_sx(block.get("outcomeOne"))

    sides = best_by_market.setdefault(market_hash, {})

    if side_one:
        best_odd = side_one[0]["odd"]

        sides["outcome_one"] = {
            "odd": best_odd,
            "liquidity_usdc": sum(
                x["liquidity_usdc"]
                for x in side_one
                if abs(x["odd"] - best_odd) < 0.002
            )
        }

    if side_two:
        best_odd = side_two[0]["odd"]

        sides["outcome_two"] = {
            "odd": best_odd,
            "liquidity_usdc": sum(
                x["liquidity_usdc"]
                for x in side_two
                if abs(x["odd"] - best_odd) < 0.002
            )
        }


def obter_melhores_odds_sx(relevant_markets):
    best_by_market = {}

    hashes = list(dict.fromkeys(
        market["market_hash"] for market in relevant_markets
    ))

    for i in range(0, len(hashes), 50):
        batch = hashes[i:i + 50]

        try:
            response = session.get(
                f"{SX_BASE}/orders-v3/odds/best",
                params={"marketHashes": ",".join(batch)},
                timeout=20
            )

            if response.status_code == 400:
                response = session.get(
                    f"{SX_BASE}/orders-v3/odds/best",
                    params=[
                        ("marketHashes", value)
                        for value in batch
                    ],
                    timeout=20
                )

            response.raise_for_status()

            payload = response.json()
            data = (
                payload.get("data", payload)
                if isinstance(payload, dict)
                else payload
            )

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        registar_livro_sx(
                            item.get("marketHash"),
                            item,
                            best_by_market
                        )

            elif isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict):
                        registar_livro_sx(
                            value.get("marketHash") or key,
                            value,
                            best_by_market
                        )

        except Exception:
            logger.exception(
                "Falha a obter odds SX (lote %d).",
                i // 50 + 1
            )

    return best_by_market


# ============================================================
# POLYMARKET — ORDER BOOK ATUALIZADO VIA REST
# ============================================================

def obter_livro_poly_fresco(token_id):
    try:
        book = http_get(
            f"{POLY_CLOB}/book",
            params={"token_id": str(token_id)},
            timeout=8
        )

        asks = book.get("asks", []) or []
        valid = []

        for level in asks:
            try:
                price = float(level["price"])
                size = float(level["size"])

                if 0 < price < 1 and size > 0:
                    valid.append({
                        "price": price,
                        "size": size
                    })

            except (KeyError, TypeError, ValueError):
                continue

        if not valid:
            return None

        valid.sort(key=lambda item: item["price"])
        best = valid[0]

        # Soma do valor USD de todos os níveis de venda.
        liquidity = sum(
            item["price"] * item["size"]
            for item in valid
        )

        result = {
            "price": best["price"],
            "size": best["size"],
            "liquidity_usd": liquidity,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "source": "rest"
        }

        with orderbook_lock:
            live_orderbooks[str(token_id)] = result

        return result

    except requests.RequestException as exc:
        logger.warning(
            "Não foi possível obter orderbook Poly %s: %s",
            token_id,
            exc
        )
        return None


# ============================================================
# MATCHING DOS OUTCOMES POLYMARKET COM A SX BET
# ============================================================

def extrair_opcoes_poly_para_sx(poly_event, sx_market):
    team1 = sx_market["team_one"]
    team2 = sx_market["team_two"]
    options = {}

    for market in poly_event["markets"]:
        outcomes = market["outcomes"]
        token_ids = market["token_ids"]

        if len(outcomes) != 2 or len(token_ids) != 2:
            continue

        for index, outcome in enumerate(outcomes):
            match1 = equipas_coincidem(outcome, team1)
            match2 = equipas_coincidem(outcome, team2)

            if match1 and not match2:
                options["sx_one"] = {
                    "token_id": token_ids[index],
                    "label": f"{team1} ({outcome})",
                    "poly_outcome": outcome,
                    "question": market["question"]
                }

            elif match2 and not match1:
                options["sx_two"] = {
                    "token_id": token_ids[index],
                    "label": f"{team2} ({outcome})",
                    "poly_outcome": outcome,
                    "question": market["question"]
                }

    return options


# ============================================================
# ANÁLISE DE CRUZAMENTOS E ALERTAS TELEGRAM
# ============================================================

def analisar_cruzamento(jogos_poly, mercados_sx, odds_sx):
    alerts_sent = 0
    matched_hashes = set()
    unmatched_logged = 0

    for sx_market in mercados_sx:
        market_hash = sx_market["market_hash"]
        sides_sx = odds_sx.get(market_hash)

        if not sides_sx:
            continue

        team1 = sx_market["team_one"]
        team2 = sx_market["team_two"]

        matched_event = None
        matched_options = None

        for poly_event in jogos_poly:
            if not jogo_coincide_com_titulo(
                team1,
                team2,
                poly_event["title"]
            ):
                continue

            options = extrair_opcoes_poly_para_sx(
                poly_event,
                sx_market
            )

            if options:
                matched_event = poly_event
                matched_options = options
                break

        if not matched_event:
            if unmatched_logged < MAX_MATCH_LOGS:
                logger.info(
                    "SEM MATCH | SX='%s' vs '%s' | "
                    "procurar título Poly compatível",
                    team1,
                    team2
                )
                unmatched_logged += 1

            continue

        matched_hashes.add(market_hash)

        logger.info(
            "MATCH OK | SX='%s' vs '%s' | Poly='%s' | outcomes=%s",
            team1,
            team2,
            matched_event["title"],
            [
                v.get("poly_outcome")
                for v in matched_options.values()
            ]
        )

        pairs = [
            ("sx_one", "outcome_two", sx_market["outcome_two"]),
            ("sx_two", "outcome_one", sx_market["outcome_one"])
        ]

        for poly_key, sx_key, sx_label in pairs:
            if poly_key not in matched_options or sx_key not in sides_sx:
                continue

            poly_option = matched_options[poly_key]
            sx_option = sides_sx[sx_key]

            poly_book = obter_livro_poly_fresco(
                poly_option["token_id"]
            )

            if not poly_book:
                logger.info(
                    "SEM ASK | %s | token=%s",
                    poly_option["label"],
                    poly_option["token_id"]
                )
                continue

            poly_price = poly_book["price"]
            sx_odd = sx_option["odd"]

            if poly_price <= 0 or sx_odd <= 1:
                continue

            # Estimativa simplificada da margem.
            implied_sum = poly_price + (1.0 / sx_odd)
            profit_pct = (
                (1.0 / implied_sum) - 1.0
            ) * 100.0

            poly_liquidity = poly_book["liquidity_usd"]
            sx_liquidity = sx_option["liquidity_usdc"]

            if poly_liquidity < MIN_LIQUIDITY_USD:
                logger.info(
                    "IGNORADO - liquidez Poly baixa: $%.2f",
                    poly_liquidity
                )
                continue

            if profit_pct < MIN_PROFIT_PCT:
                continue

            message = (
                "🛡️ PAPER TRADING V10.0 — ALERTA DE TESTE\n\n"
                f"Jogo: {team1} vs {team2}\n"
                f"Polymarket: {matched_event['title']}\n"
                f"Outcome Poly: {poly_option['poly_outcome']}\n"
                f"Pergunta Poly: {poly_option['question']}\n\n"
                f"BUY Poly: {poly_price * 100:.2f}¢\n"
                f"Odd SX: {sx_odd:.3f}\n"
                f"Margem estimada: {profit_pct:.2f}%\n\n"
                f"Liquidez Poly (asks): ${poly_liquidity:.2f}\n"
                f"Liquidez SX: ${sx_liquidity:.2f}\n"
                f"Preço consultado: {poly_book['checked_at']}\n"
                f"Fonte do preço: {poly_book['source']}\n\n"
                "MODO SIMULAÇÃO — NÃO REALIZAR APOSTAS REAIS.\n"
                "Confirma manualmente preço e liquidez antes de "
                "qualquer avaliação."
            )

            if enviar_alerta_telegram(message):
                alerts_sent += 1

            # Um alerta por mercado SX em cada ciclo.
            break

    with state_lock:
        scanner_status["matched_games"] = len(matched_hashes)
        scanner_status["unmatched_sx_markets"] = max(
            0,
            len(mercados_sx) - len(matched_hashes)
        )
        scanner_status["alerts_sent"] += alerts_sent

    return alerts_sent

# ============================================================
# SCANNER — CICLO DE ANÁLISE
# ============================================================

def ciclo_scanner():
    started = datetime.now(timezone.utc)
    logger.info("========== INÍCIO DO SCAN V10.0 ==========")

    jogos_poly = obter_jogos_polymarket()
    mercados_sx = obter_mercados_sx()

    # Subscrever os tokens encontrados no WebSocket.
    all_tokens = []

    for event in jogos_poly:
        for market in event["markets"]:
            all_tokens.extend(market["token_ids"])

    subscrever_tokens_poly(all_tokens)

    relevant_sx = [
        sx_market
        for sx_market in mercados_sx
        if any(
            jogo_coincide_com_titulo(
                sx_market["team_one"],
                sx_market["team_two"],
                poly_event["title"]
            )
            for poly_event in jogos_poly
        )
    ]

    logger.info(
        "Resumo recolha | Poly events=%d | SX markets=%d | SX relevantes=%d",
        len(jogos_poly),
        len(mercados_sx),
        len(relevant_sx)
    )

    odds_sx = (
        obter_melhores_odds_sx(relevant_sx)
        if relevant_sx
        else {}
    )

    alerts = analisar_cruzamento(
        jogos_poly,
        relevant_sx,
        odds_sx
    )

    elapsed = (
        datetime.now(timezone.utc) - started
    ).total_seconds()

    with state_lock:
        scanner_status["last_scan"] = (
            datetime.now(timezone.utc).isoformat()
        )
        scanner_status["last_error"] = None

    logger.info(
        "FIM DO SCAN | alertas=%d | duração=%.1fs",
        alerts,
        elapsed
    )


# ============================================================
# SCANNER CONTÍNUO
# ============================================================

def correr_robo_continuo():
    while True:
        try:
            ciclo_scanner()

        except Exception as exc:
            logger.exception("Erro inesperado no scanner.")

            with state_lock:
                scanner_status["last_error"] = str(exc)

        time.sleep(SCAN_INTERVAL)


# ============================================================
# HEALTH CHECK
# ============================================================

class SimpleHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        with state_lock:
            status = dict(scanner_status)

        body = (
            "PAPER TRADING V10.0 ONLINE\n"
            f"Mode: {status['mode']}\n"
            f"WebSocket: {status['ws_connected']}\n"
            f"Last scan: {status['last_scan']}\n"
            f"Poly events: {status['poly_events_found']}\n"
            f"SX markets: {status['sx_markets_found']}\n"
            f"Matched games: {status['matched_games']}\n"
            f"Alerts sent: {status['alerts_sent']}\n"
            f"Last error: {status['last_error']}\n"
            "AUTO_BET: False\n"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


# ============================================================
# ARRANQUE DO PROGRAMA
# ============================================================

if __name__ == "__main__":
    logger.info("A iniciar PAPER TRADING V10.0.")
    logger.info(
        "AUTO_BET=%s (deve permanecer False)",
        AUTO_BET
    )

    if AUTO_BET:
        raise RuntimeError(
            "Proteção: AUTO_BET tem de permanecer False."
        )

    # Testa o Telegram antes de iniciar o scanner.
    testar_telegram()

    # WebSocket da Polymarket.
    threading.Thread(
        target=correr_websocket_poly,
        daemon=True,
        name="polymarket-websocket"
    ).start()

    # Scanner de mercados.
    threading.Thread(
        target=correr_robo_continuo,
        daemon=True,
        name="paper-scanner"
    ).start()

    # Endpoint simples para monitorização.
    port = int(os.environ.get("PORT", "10000"))

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        SimpleHandler
    )

    logger.info(
        "Health server ativo na porta %d.",
        port
    )

    server.serve_forever()
