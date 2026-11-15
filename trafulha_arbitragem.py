import os
import re
import json
import time
import threading
import logging
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import websocket


# ============================================================
# 1. CONFIGURACOES (PAPER TRADING V9.0 - WEBSOCKET CLOB)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
SX_API_KEY = os.getenv("SX_API_KEY", "")
POLY_API_KEY = os.getenv("POLY_API_KEY", "")

PAPER_INITIAL_BANKROLL_POLY = float(os.getenv("PAPER_INITIAL_BANKROLL_POLY", "1000"))
PAPER_INITIAL_BANKROLL_SX = float(os.getenv("PAPER_INITIAL_BANKROLL_SX", "1000"))
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "15"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "1000"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "15"))

POLY_FEE_BUFFER_PCT = float(os.getenv("POLY_FEE_BUFFER_PCT", "1.0"))
SX_FEE_BUFFER_PCT = float(os.getenv("SX_FEE_BUFFER_PCT", "0.0"))

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "15"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "400"))

AUTO_BET = False

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SX_BASE = "https://api.sx.bet"


# ============================================================
# 2. LOGGING E ESTADO GLOBAL
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ArbitragePaperBotV90")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache"
})

if SX_API_KEY:
    session.headers.update({"x-sx-api-key": SX_API_KEY})

if POLY_API_KEY:
    session.headers.update({"POLY_API_KEY": POLY_API_KEY})

state_lock = threading.Lock()
wallet_lock = threading.Lock()
orderbook_cache_lock = threading.Lock()

# Dicionário em memória para guardar o melhor ask em tempo real de cada token_id
live_orderbooks = {}

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
    "poly_games_checked": 0,
    "sx_markets_checked": 0,
    "name_matched_games": 0,
    "matched_games": 0,
    "best_margin_pct": None,
    "opportunities": 0,
    "ws_connected": False,
    "mode": "WEB3_PAPER_TRADING_WEBSOCKET_V9.0"
}

diagnostico_inicial_feito = False


# ============================================================
# 3. WEBSOCKET DA POLYMARKET (CLIENTE EM BACKGROUND)
# ============================================================

def processar_mensagem_ws(ws, message):
    try:
        data = json.loads(message)
        # O CLOB envia eventos de livro de ordens (book ou price_change)
        items = data if isinstance(data, list) else [data]
        
        with orderbook_cache_lock:
            for item in items:
                event_type = item.get("event_type", item.get("type"))
                token_id = item.get("asset_id") or item.get("token_id")
                
                if not token_id and "market" in item:
                    token_id = item.get("market")

                asks = item.get("asks", [])
                if not token_id or not asks:
                    continue

                asks_validos = []
                for nivel in asks:
                    try:
                        preco = float(nivel["price"])
                        quantidade = float(nivel["size"])
                        if 0 < preco < 1 and quantidade > 0:
                            asks_validos.append({"price": preco, "size": quantidade})
                    except (ValueError, TypeError, KeyError):
                        continue

                if asks_validos:
                    melhor_ask = min(asks_validos, key=lambda x: x["price"])
                    live_orderbooks[str(token_id)] = {
                        "price": melhor_ask["price"],
                        "size": melhor_ask["size"],
                        "liquidity_usd": melhor_ask["price"] * melhor_ask["size"],
                        "checked_at": datetime.now(timezone.utc).isoformat()
                    }
    except Exception as e:
        logger.error("Erro ao processar mensagem WebSocket: %s", e)


def ao_abrir_ws(ws):
    logger.info("Ligado ao WebSocket CLOB da Polymarket!")
    with state_lock:
        scanner_status["ws_connected"] = True


def ao_fechar_ws(ws, close_status_code, close_msg):
    logger.warning("WebSocket Polymarket fechado. Código: %s | Motivo: %s", close_status_code, close_msg)
    with state_lock:
        scanner_status["ws_connected"] = False


def correr_websocket_poly():
    while True:
        try:
            ws = websocket.WebSocketApp(
                POLY_CLOB_WS,
                on_open=ao_abrir_ws,
                on_message=processar_mensagem_ws,
                on_close=ao_fechar_ws
            )
            # Executa com ping automático para manter a ligação ativa
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.error("Erro na thread do WebSocket: %s", e)
        time.sleep(5)


def subscrever_tokens_websocket(ws_app, token_ids):
    if not token_ids:
        return
    try:
        payload = {
            "assets_ids": token_ids,
            "type": "market"
        }
        ws_app.send(json.dumps(payload))
    except Exception:
        pass


# ============================================================
# 4. UTILITARIOS HTTP E NORMALIZACAO
# ============================================================

def http_get(url, params=None, timeout=20):
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


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
    "borussia dortmund": "dortmund"
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


def jogo_coincide_com_titulo(team1, team2, titulo_poly):
    partes = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", str(titulo_poly), maxsplit=1, flags=re.IGNORECASE)
    if len(partes) == 2:
        lado_a, lado_b = partes[0], partes[1]
        direto = equipas_coincidem(team1, lado_a) and equipas_coincidem(team2, lado_b)
        invertido = equipas_coincidem(team1, lado_b) and equipas_coincidem(team2, lado_a)
        if direto or invertido:
            return True
    return equipas_coincidem(team1, titulo_poly) and equipas_coincidem(team2, titulo_poly)


def parse_timestamp_utc(valor):
    if not valor:
        return None
    if isinstance(valor, (int, float)):
        try:
            return datetime.fromtimestamp(float(valor), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def formatar_data_pt(dt_obj):
    if not dt_obj:
        return "Data por confirmar"
    return dt_obj.strftime("%d/%m/%Y as %H:%M UTC")


# ============================================================
# 5. TELEGRAM E GESTAO DA CARTEIRA
# ============================================================

def enviar_alerta_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")
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
        logger.info("Alerta enviado para Telegram!")
        return True
    except requests.RequestException as e:
        logger.error("Erro de rede Telegram: %s", e)
        return False


def registar_aposta_virtual(m_hash, jogo_nome, stake_poly, stake_sx, lucro_esperado):
    with wallet_lock:
        paper_wallet["apostas_realizadas_hashes"].add(m_hash)
        paper_wallet["bets_placed"] += 1

        paper_wallet["poly"]["available"] -= stake_poly
        paper_wallet["poly"]["invested"] += stake_poly

        paper_wallet["sx"]["available"] -= stake_sx
        paper_wallet["sx"]["invested"] += stake_sx

        registo = {
            "id": paper_wallet["bets_placed"],
            "time": datetime.now(timezone.utc).isoformat(),
            "match": jogo_nome,
            "stake_poly": stake_poly,
            "stake_sx": stake_sx,
            "expected_profit": lucro_esperado
        }
        paper_wallet["history"].append(registo)
        logger.info("PAPER TRADING | Aposta #%s registada para %s | Investido Poly: $%.2f | Investido SX: $%.2f",
                    registo["id"], jogo_nome, stake_poly, stake_sx)


# ============================================================
# 6. RECOLHA DE DADOS POLYMARKET E SX BET
# ============================================================

SPORT_KEYWORDS = [
    " vs ", " vs. ", "nba", "euroleague", "premier league",
    "champions league", "la liga", "serie a", "bundesliga",
    "ligue 1", "primeira liga", "benfica", "porto", "sporting",
    "ufc", "mma", "atp", "wta", "tennis", "basketball", "soccer",
    "football", "nhl", "hockey", "nfl", "mlb", "baseball"
]


def parece_evento_desportivo(texto):
    t = str(texto).lower()
    return any(k in t for k in SPORT_KEYWORDS)


def obter_preco_websocket_ou_rest(token_id):
    """Lê em prioridade do cache em tempo real do WebSocket; se não existir, recorre ao REST."""
    with orderbook_cache_lock:
        if token_id in live_orderbooks:
            return live_orderbooks[token_id]

    # Fallback REST caso o WebSocket ainda não tenha recebido pacotes deste token
    try:
        book = http_get(f"https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=6)
        asks = book.get("asks", [])
        if not asks:
            return None

        asks_validos = []
        for nivel in asks:
            try:
                preco = float(nivel["price"])
                quantidade = float(nivel["size"])
                if 0 < preco < 1 and quantidade > 0:
                    asks_validos.append({"price": preco, "size": quantidade})
            except (ValueError, TypeError, KeyError):
                continue

        if not asks_validos:
            return None

        melhor_ask = min(asks_validos, key=lambda x: x["price"])
        res = {
            "price": melhor_ask["price"],
            "size": melhor_ask["size"],
            "liquidity_usd": melhor_ask["price"] * melhor_ask["size"],
            "checked_at": datetime.now(timezone.utc).isoformat()
        }
        with orderbook_cache_lock:
            live_orderbooks[token_id] = res
        return res
    except Exception:
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

                q_lower = str(m.get("question", "")).lower()
                g_lower = str(m.get("groupItemTitle", "")).lower()

                is_moneyline = any(w in q_lower for w in ["win the match", "winner", "vence", "moneyline", "match winner"]) or (
                    " vs " in q_lower and not any(w in q_lower for w in ["spread", "over", "under", "total", "handicap", "map", "set", "quarter", "half", "period"])
                )

                if not is_moneyline and len(mercados_ev) > 1:
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

                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue

                submercados.append({
                    "id": str(m.get("id", "")),
                    "question": m.get("question", titulo),
                    "group_title": m.get("groupItemTitle", ""),
                    "outcomes": outcomes,
                    "token_ids": token_ids,
                    "volume": volume_efetivo
                })

            if submercados:
                jogos_poly.append({
                    "event_id": ev_id,
                    "title": titulo,
                    "slug": ev.get("slug", ""),
                    "end_date": parse_timestamp_utc(ev.get("endDate") or ev.get("startDate")),
                    "volume": ev_volume,
                    "markets": submercados
                })

            if len(jogos_poly) >= MAX_POLY_MARKETS:
                return jogos_poly

    return jogos_poly


def obter_mercados_sx():
    mercados = []
    next_key = None

    for _ in range(8):
        params = {"onlyMainLine": "true"}
        if next_key:
            params["paginationKey"] = next_key

        try:
            resp = http_get(f"{SX_BASE}/markets/active", params=params, timeout=20)
        except requests.RequestException as e:
            logger.error("Erro ao ler mercados SX: %s", e)
            break

        data = resp.get("data", {})
        lista = data.get("markets", [])
        if not lista:
            break

        for m in lista:
            if m.get("status") != "ACTIVE":
                continue

            m_type = m.get("type")
            if m_type not in (1, 52, 226):
                continue

            team1 = m.get("teamOneName", "")
            team2 = m.get("teamTwoName", "")
            out1 = m.get("outcomeOneName", "")
            out2 = m.get("outcomeTwoName", "")
            m_hash = m.get("marketHash", "")

            if not (team1 and team2 and out1 and out2 and m_hash):
                continue

            mercados.append({
                "market_hash": m_hash,
                "event_id": str(m.get("sportXeventId") or m.get("eventId") or ""),
                "type": m_type,
                "team_one": team1,
                "team_two": team2,
                "outcome_one": out1,
                "outcome_two": out2,
                "sport": m.get("sportLabel", ""),
                "league": m.get("leagueLabel", ""),
                "game_time": parse_timestamp_utc(m.get("gameTime"))
            })

        next_key = data.get("nextKey")
        if not next_key:
            break

    return mercados


# ============================================================
# 7. LEITURA DE ODDS SX BET
# ============================================================

def _extrair_niveis_lado_obv3(lado_raw):
    if isinstance(lado_raw, dict):
        itens = [lado_raw]
    elif isinstance(lado_raw, list):
        itens = lado_raw
    else:
        return []

    niveis_taker = []
    fator_taxa_sx = 1.0 - (SX_FEE_BUFFER_PCT / 100.0)

    for item in itens:
        if not isinstance(item, dict):
            continue

        raw_odds = item.get("percentageOdds")
        raw_size = item.get("size") if item.get("size") is not None else item.get("totalBetSize")
        if raw_odds is None or raw_size is None:
            continue

        try:
            maker_prob = float(raw_odds) / 1e20
            maker_stake_usdc = float(raw_size) / 1e6
        except (ValueError, TypeError):
            continue

        if not (0.01 < maker_prob < 0.99) or maker_stake_usdc <= 0:
            continue

        taker_prob = 1.0 - maker_prob
        taker_odd_bruta = 1.0 / taker_prob
        taker_odd = 1.0 + (taker_odd_bruta - 1.0) * fator_taxa_sx
        taker_liq_usdc = maker_stake_usdc * (taker_prob / maker_prob)

        if taker_liq_usdc > 0:
            niveis_taker.append({
                "odd": taker_odd,
                "liquidity_usdc": taker_liq_usdc
            })

    niveis_taker.sort(key=lambda x: x["odd"], reverse=True)
    return niveis_taker


def _registar_livro_mercado_obv3(m_hash, bloco, melhores_por_mercado):
    if not m_hash or not isinstance(bloco, dict):
        return

    niveis_para_out2 = _extrair_niveis_lado_obv3(bloco.get("outcomeOne"))
    niveis_para_out1 = _extrair_niveis_lado_obv3(bloco.get("outcomeTwo"))

    if not niveis_para_out1 and not niveis_para_out2:
        return

    lados = melhores_por_mercado.setdefault(m_hash, {})

    if niveis_para_out1:
        melhor_1 = niveis_para_out1[0]
        liq_topo_1 = sum(
            n["liquidity_usdc"] for n in niveis_para_out1
            if abs(n["odd"] - melhor_1["odd"]) < 0.002
        )
        lados["outcome_one"] = {
            "odd": melhor_1["odd"],
            "liquidity_usdc": liq_topo_1,
            "levels": niveis_para_out1
        }

    if niveis_para_out2:
        melhor_2 = niveis_para_out2[0]
        liq_topo_2 = sum(
            n["liquidity_usdc"] for n in niveis_para_out2
            if abs(n["odd"] - melhor_2["odd"]) < 0.002
        )
        lados["outcome_two"] = {
            "odd": melhor_2["odd"],
            "liquidity_usdc": liq_topo_2,
            "levels": niveis_para_out2
        }


def _parsear_payload_obv3(data, melhores_por_mercado, m_hash_fallback=None):
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                mh = item.get("marketHash") or m_hash_fallback
                _registar_livro_mercado_obv3(mh, item, melhores_por_mercado)
        return

    if not isinstance(data, dict):
        return

    if "outcomeOne" in data or "outcomeTwo" in data:
        mh = data.get("marketHash") or m_hash_fallback
        _registar_livro_mercado_obv3(mh, data, melhores_por_mercado)
        return

    for k, v in data.items():
        if isinstance(v, dict) and ("outcomeOne" in v or "outcomeTwo" in v):
            mh = v.get("marketHash") or (k if str(k).startswith("0x") else m_hash_fallback)
            _registar_livro_mercado_obv3(mh, v, melhores_por_mercado)


def obter_melhores_odds_sx(mercados_relevantes):
    melhores_por_mercado = {}
    if not mercados_relevantes:
        return melhores_por_mercado

    hashes_unicos = list(dict.fromkeys(m["market_hash"] for m in mercados_relevantes))

    for i in range(0, len(hashes_unicos), 50):
        lote = hashes_unicos[i:i + 50]
        try:
            resp = session.get(
                f"{SX_BASE}/orders-v3/odds/best",
                params={"marketHashes": ",".join(lote)},
                timeout=20
            )
            if resp.status_code == 400:
                resp = session.get(
                    f"{SX_BASE}/orders-v3/odds/best",
                    params=[("marketHashes", h) for h in lote],
                    timeout=20
                )

            if resp.ok:
                payload = resp.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                _parsear_payload_obv3(data, melhores_por_mercado)
        except Exception:
            continue

    return melhores_por_mercado


# ============================================================
# 8. MAPEAMENTO E CRUZAMENTO
# ============================================================

def extrair_opcoes_poly_para_sx(jogo_poly, sx_market):
    team1 = sx_market["team_one"]
    team2 = sx_market["team_two"]
    opcoes = {}

    for m in jogo_poly["markets"]:
        outs = m["outcomes"]
        tids = m["token_ids"]
        if len(outs) != 2 or len(tids) != 2:
            continue

        for idx, out_name in enumerate(outs):
            tid = tids[idx]
            if equipas_coincidem(out_name, team1) and not equipas_coincidem(out_name, team2):
                opcoes["sx_one"] = {"token_id": tid, "label": f"{team1} (Yes)", "question": m.get("question", "")}
            elif equipas_coincidem(out_name, team2) and not equipas_coincidem(out_name, team1):
                opcoes["sx_two"] = {"token_id": tid, "label": f"{team2} (Yes)", "question": m.get("question", "")}

    return opcoes


# ============================================================
# 9. ANALISE DE ARBITRAGEM V9.0
# ============================================================

def analisar_cruzamento_web3(jogos_poly, mercados_sx, odds_sx, stats):
    alertas_enviados = 0

    for sx_m in mercados_sx:
        m_hash = sx_m["market_hash"]
        if m_hash not in odds_sx:
            continue

        with wallet_lock:
            if m_hash in paper_wallet["apostas_realizadas_hashes"]:
                continue

        lados_sx = odds_sx[m_hash]
        team1 = sx_m["team_one"]
        team2 = sx_m["team_two"]
        dt_sx = sx_m["game_time"]

        for jogo_poly in jogos_poly:
            if not jogo_coincide_com_titulo(team1, team2, jogo_poly["title"]):
                continue

            dt_poly = jogo_poly["end_date"]
            if dt_sx and dt_poly:
                diff_horas = abs((dt_poly - dt_sx).total_seconds()) / 3600.0
                if diff_horas > 96:
                    continue

            opcoes_poly = extrair_opcoes_poly_para_sx(jogo_poly, sx_m)
            if not opcoes_poly:
                continue

            stats["jogos_casados"] += 1

            pares = [
                ("sx_one", "outcome_two", sx_m["outcome_two"]),
                ("sx_two", "outcome_one", sx_m["outcome_one"])
            ]

            for poly_key, sx_key, label_sx in pares:
                if poly_key not in opcoes_poly or sx_key not in lados_sx:
                    continue

                opt_p = opcoes_poly[poly_key]
                opt_sx = lados_sx[sx_key]

                if opt_sx["liquidity_usdc"] < MIN_LIQUIDITY_USD:
                    continue

                # CONSULTA EM TEMPO REAL (WEBSOCKET CACHE OU FALLBACK REST)
                ask = obter_preco_websocket_ou_rest(opt_p["token_id"])
                if not ask:
                    continue

                liq_poly_usd = ask["size"] * ask["price"]
                if liq_poly_usd < MIN_LIQUIDITY_USD:
                    continue

                preco_bruto_poly = ask["price"]
                if not (0.01 < preco_bruto_poly < 0.99):
                    continue

                odd_bruta_poly = 1.0 / preco_bruto_poly
                odd_bruta_sx = opt_sx["odd"]

                soma_prob_bruta = (1.0 / odd_bruta_poly) + (1.0 / odd_bruta_sx)
                lucro_pct_bruto = ((1.0 / soma_prob_bruta) - 1.0) * 100.0

                fator_taxa_poly = 1.0 + (POLY_FEE_BUFFER_PCT / 100.0)
                preco_com_taxa_poly = preco_bruto_poly * fator_taxa_poly
                if preco_com_taxa_poly >= 1.0:
                    continue
                odd_liq_poly = 1.0 / preco_com_taxa_poly

                fator_taxa_sx = 1.0 - (SX_FEE_BUFFER_PCT / 100.0)
                odd_liq_sx = 1.0 + (odd_bruta_sx - 1.0) * fator_taxa_sx

                soma_prob_liq = (1.0 / odd_liq_poly) + (1.0 / odd_liq_sx)
                lucro_pct_liq = ((1.0 / soma_prob_liq) - 1.0) * 100.0

                if stats["melhor_margem"] is None or lucro_pct_liq > stats["melhor_margem"]:
                    stats["melhor_margem"] = lucro_pct_liq

                if lucro_pct_liq < MIN_PROFIT_PCT:
                    continue

                prop_poly = (1.0 / odd_liq_poly) / soma_prob_liq
                prop_sx = (1.0 / odd_liq_sx) / soma_prob_liq

                banca_efetiva = min(PAPER_STAKE_USD, liq_poly_usd / prop_poly, opt_sx["liquidity_usdc"] / prop_sx)
                if banca_efetiva < 10.0:
                    continue

                stake_poly = banca_efetiva * prop_poly
                stake_sx = banca_efetiva * prop_sx

                retorno_bruto = banca_efetiva / soma_prob_bruta
                lucro_usd_bruto = retorno_bruto - banca_efetiva
                custo_taxas_estimado = banca_efetiva * ((lucro_pct_bruto - lucro_pct_liq) / 100.0)

                retorno_liquido = banca_efetiva / soma_prob_liq
                lucro_usd_liquido = retorno_liquido - banca_efetiva

                preco_cents = preco_bruto_poly * 100
                hora_cotacao = ask["checked_at"]

                registar_aposta_virtual(
                    m_hash,
                    f"{team1} vs {team2}",
                    stake_poly,
                    stake_sx,
                    lucro_usd_liquido
                )

                mensagem = (
                    "🛡️ [PAPER TRADING - WEBSOCKET V9.0]\n\n"
                    f"Jogo: {team1} vs {team2}\n"
                    f"Mercado Poly: {opt_p['question']}\n"
                    f"Competicao: {sx_m['sport']} - {sx_m['league']}\n"
                    f"Data e Hora: {formatar_data_pt(dt_sx or dt_poly)}\n\n"
                    "Lado 1 (Polymarket - USDC):\n"
                    f"- Aposta: {opt_p['label']}\n"
                    f"- Preco BUY validado (WS): {preco_cents:.2f}¢ ({preco_bruto_poly:.4f} USDC)\n"
                    f"- Odd Bruta: {odd_bruta_poly:.4f} | Odd apos buffer: {odd_liq_poly:.4f}\n"
                    f"- Liquidez no melhor ASK: ${ask['liquidity_usd']:.2f} USDC\n"
                    f"- Cotacao confirmada (UTC): {hora_cotacao}\n"
                    f"- Valor investido: ${stake_poly:.2f} USDC\n\n"
                    "Lado 2 (SX Bet - USDC):\n"
                    f"- Aposta: Not {opt_p['label'].replace(' (Yes)', '')}\n"
                    f"- Odd Taker Real (Bruta: {odd_bruta_sx:.2f} | Liq: {odd_liq_sx:.2f})\n"
                    f"- Valor investido: ${stake_sx:.2f} USDC\n\n"
                    f"💵 **ESTRUTURA DE LUCRO (${banca_efetiva:.2f} Totais):**\n"
                    f"  • Lucro Bruto (Sem Taxas): +${lucro_usd_bruto:.2f} (~{lucro_pct_bruto:.2f}%)\n"
                    f"  • Custo de Taxas/Buffers: -${custo_taxas_estimado:.2f} ({POLY_FEE_BUFFER_PCT}% Poly / {SX_FEE_BUFFER_PCT}% SX)\n"
                    f"  • 🟢 **Lucro Líquido para ti:** +${lucro_usd_liquido:.2f} (~{lucro_pct_liq:.2f}%)\n\n"
                    "📊 **ESTADO DAS BANCAS:**\n"
                    f"🔵 **Polymarket:** Disp: ${paper_wallet['poly']['available']:.2f} | Inv: ${paper_wallet['poly']['invested']:.2f}\n"
                    f"🟢 **SX Bet:** Disp: ${paper_wallet['sx']['available']:.2f} | Inv: ${paper_wallet['sx']['invested']:.2f}\n"
                    f"💰 **Total Global:** ${(paper_wallet['poly']['available'] + paper_wallet['poly']['invested'] + paper_wallet['sx']['available'] + paper_wallet['sx']['invested']):.2f}\n\n"
                    "⚠️ PRECO DE REFERENCIA, NAO GARANTIA DE EXECUCAO."
                )

                if enviar_alerta_telegram(mensagem):
                    alertas_enviados += 1
                break

    return alertas_enviados


# ============================================================
# 10. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():
    stats = {"jogos_casados": 0, "melhor_margem": None}
    jogos_poly = obter_jogos_polymarket()
    mercados_sx = obter_mercados_sx()

    mercados_relevantes = [
        sx_m for sx_m in mercados_sx
        if any(jogo_coincide_com_titulo(sx_m["team_one"], sx_m["team_two"], jp["title"]) for jp in jogos_poly)
    ]

    odds_sx = obter_melhores_odds_sx(mercados_relevantes) if mercados_relevantes else {}
    total_alertas = analisar_cruzamento_web3(jogos_poly, mercados_relevantes, odds_sx, stats)

    melhor_str = f"{stats['melhor_margem']:.2f}%" if stats["melhor_margem"] is not None else "N/A"
    logger.info(
        "Ciclo concluido | Casados: %d | Melhor margem: %s | Alertas: %d",
        stats["jogos_casados"],
        melhor_str,
        total_alertas
    )

    with state_lock:
        scanner_status["last_scan"] = datetime.now(timezone.utc).isoformat()
        scanner_status["poly_games_checked"] = len(jogos_poly)
        scanner_status["sx_markets_checked"] = len(mercados_sx)
        scanner_status["name_matched_games"] = len(mercados_relevantes)
        scanner_status["matched_games"] = stats["jogos_casados"]
        scanner_status["best_margin_pct"] = stats["melhor_margem"]
        scanner_status["opportunities"] += total_alertas
        scanner_status["last_error"] = None


def correr_robo_continuo():
    while True:
        try:
            ciclo_scanner()
        except Exception as e:
            logger.exception("Erro geral do scanner.")
            with state_lock:
                scanner_status["last_error"] = str(e)
        time.sleep(SCAN_INTERVAL)


# ============================================================
# 11. SERVIDOR WEB RENDER
# ============================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            with state_lock:
                data = dict(scanner_status)
            with wallet_lock:
                data["paper_wallet"] = dict(paper_wallet)
            data["auto_bet"] = AUTO_BET
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            with wallet_lock:
                p_disp = paper_wallet["poly"]["available"]
                p_inv = paper_wallet["poly"]["invested"]
                s_disp = paper_wallet["sx"]["available"]
                s_inv = paper_wallet["sx"]["invested"]
            body = (
                "ROBO ARBITRAGEM PAPER TRADING V9.0 ONLINE (WEBSOCKET)\n"
                f"Polymarket Disp: ${p_disp:.2f} | Investido: ${p_inv:.2f}\n"
                f"SX Bet Disp: ${s_disp:.2f} | Investido: ${s_inv:.2f}\n"
                "Health / Wallet: /health\n"
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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")

    # 1. Iniciar thread do WebSocket da Polymarket
    ws_thread = threading.Thread(target=correr_websocket_poly, daemon=True)
    ws_thread.start()

    # 2. Iniciar thread do scanner de arbitragem
    scanner_thread = threading.Thread(target=correr_robo_continuo, daemon=True)
    scanner_thread.start()

    # 3. Iniciar servidor HTTP do Render
    iniciar_servidor_web()
