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
# 1. CONFIGURACOES (PAPER TRADING V9.2 - SMART MATCHING)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
SX_API_KEY = os.getenv("SX_API_KEY", "")
POLY_API_KEY = os.getenv("POLY_API_KEY", "")

PAPER_INITIAL_BANKROLL_POLY = float(os.getenv("PAPER_INITIAL_BANKROLL_POLY", "1000"))
PAPER_INITIAL_BANKROLL_SX = float(os.getenv("PAPER_INITIAL_BANKROLL_SX", "1000"))
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "-3.0"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "15"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "100"))
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
logger = logging.getLogger("ArbitragePaperBotV92")

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

live_orderbooks = {}

paper_wallet = {
    "poly": {"initial": PAPER_INITIAL_BANKROLL_POLY, "available": PAPER_INITIAL_BANKROLL_POLY, "invested": 0.0, "profit": 0.0},
    "sx": {"initial": PAPER_INITIAL_BANKROLL_SX, "available": PAPER_INITIAL_BANKROLL_SX, "invested": 0.0, "profit": 0.0},
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
    "mode": "WEB3_PAPER_TRADING_SMART_MATCH_V9.2"
}


# ============================================================
# 3. WEBSOCKET CLOB
# ============================================================

def processar_mensagem_ws(ws, message):
    try:
        data = json.loads(message)
        items = data if isinstance(data, list) else [data]
        with orderbook_cache_lock:
            for item in items:
                token_id = item.get("asset_id") or item.get("token_id") or item.get("market")
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
    except Exception:
        pass


def ao_abrir_ws(ws):
    logger.info("Websocket connected")
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
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(5)


# ============================================================
# 4. UTILITARIOS DE NORMALIZACAO INTELIGENTE
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
    "sc braga": "braga"
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
    if norm_a in norm_b or norm_b in norm_a:
        return True
    
    tok_a = tokens_relevantes(norm_a)
    tok_b = tokens_relevantes(norm_b)
    if not tok_a or not tok_b:
        return False
    
    # CORREÇÃO V9.2: Basta que qualquer token relevante relevante coincida ou esteja contido
    return any(t in tok_b for t in tok_a) or any(t in tok_a for t in tok_b)


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
# 5. TELEGRAM E CARTEIRA
# ============================================================

def enviar_alerta_telegram(mensagem):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensagem, "disable_web_page_preview": True}
    try:
        response = session.post(url, json=payload, timeout=15)
        return response.ok
    except Exception:
        return False


def registar_aposta_virtual(m_hash, jogo_nome, stake_poly, stake_sx, lucro_esperado):
    with wallet_lock:
        paper_wallet["apostas_realizadas_hashes"].add(m_hash)
        paper_wallet["bets_placed"] += 1
        paper_wallet["poly"]["available"] -= stake_poly
        paper_wallet["poly"]["invested"] += stake_poly
        paper_wallet["sx"]["available"] -= stake_sx
        paper_wallet["sx"]["invested"] += stake_sx
        paper_wallet["history"].append({
            "id": paper_wallet["bets_placed"],
            "time": datetime.now(timezone.utc).isoformat(),
            "match": jogo_nome,
            "stake_poly": stake_poly,
            "stake_sx": stake_sx,
            "expected_profit": lucro_esperado
        })


# ============================================================
# 6. RECOLHA DE DADOS
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
    with orderbook_cache_lock:
        if token_id in live_orderbooks:
            return live_orderbooks[token_id]
    try:
        book = http_get(f"https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=6)
        asks = book.get("asks", [])
        if not asks:
            return None
        asks_validos = [{"price": float(n["price"]), "size": float(n["size"])} for n in asks if 0 < float(n["price"]) < 1 and float(n["size"]) > 0]
        if not asks_validos:
            return None
        melhor_ask = min(asks_validos, key=lambda x: x["price"])
        res = {"price": melhor_ask["price"], "size": melhor_ask["size"], "liquidity_usd": melhor_ask["price"] * melhor_ask["size"], "checked_at": datetime.now(timezone.utc).isoformat()}
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
            eventos = http_get(f"{POLY_GAMMA}/events", params={"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100, "offset": page * 100}, timeout=20)
        except Exception:
            break
        if not eventos:
            break
        for ev in eventos:
            ev_id = str(ev.get("id", ""))
            if ev_id in vistos:
                continue
            vistos.add(ev_id)
            titulo = ev.get("title", "")
            if not parece_evento_desportivo(f"{titulo} {ev.get('description', '')}"):
                continue
            mercados_ev = ev.get("markets", [])
            if not mercados_ev:
                continue
            submercados = []
            for m in mercados_ev:
                if m.get("closed") is True or m.get("active") is False:
                    continue
                outcomes = converter_lista(m.get("outcomes"))
                token_ids = converter_lista(m.get("clobTokenIds"))
                if len(outcomes) != 2 or len(token_ids) != 2:
                    continue
                submercados.append({"id": str(m.get("id", "")), "question": m.get("question", titulo), "outcomes": outcomes, "token_ids": token_ids})
            if submercados:
                jogos_poly.append({"event_id": ev_id, "title": titulo, "end_date": parse_timestamp_utc(ev.get("endDate") or ev.get("startDate")), "markets": submercados})
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
        except Exception:
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
            team1, team2 = m.get("teamOneName", ""), m.get("teamTwoName", "")
            out1, out2 = m.get("outcomeOneName", ""), m.get("outcomeTwoName", "")
            m_hash = m.get("marketHash", "")
            if not (team1 and team2 and out1 and out2 and m_hash):
                continue
            mercados.append({
                "market_hash": m_hash,
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


def _extrair_niveis_lado_obv3(lado_raw):
    itens = [lado_raw] if isinstance(lado_raw, dict) else (lado_raw if isinstance(lado_raw, list) else [])
    niveis_taker = []
    fator_taxa_sx = 1.0 - (SX_FEE_BUFFER_PCT / 100.0)
    for item in itens:
        if not isinstance(item, dict):
            continue
        raw_odds, raw_size = item.get("percentageOdds"), item.get("size") if item.get("size") is not None else item.get("totalBetSize")
        if raw_odds is None or raw_size is None:
            continue
        try:
            maker_prob = float(raw_odds) / 1e20
            maker_stake_usdc = float(raw_size) / 1e6
        except Exception:
            continue
        if not (0.01 < maker_prob < 0.99) or maker_stake_usdc <= 0:
            continue
        taker_prob = 1.0 - maker_prob
        taker_odd = 1.0 + ((1.0 / taker_prob) - 1.0) * fator_taxa_sx
        taker_liq = maker_stake_usdc * (taker_prob / maker_prob)
        if taker_liq > 0:
            niveis_taker.append({"odd": taker_odd, "liquidity_usdc": taker_liq})
    niveis_taker.sort(key=lambda x: x["odd"], reverse=True)
    return niveis_taker


def _registar_livro_mercado_obv3(m_hash, bloco, melhores_por_mercado):
    if not m_hash or not isinstance(bloco, dict):
        return
    n1 = _extrair_niveis_lado_obv3(bloco.get("outcomeTwo"))
    n2 = _extrair_niveis_lado_obv3(bloco.get("outcomeOne"))
    if not n1 and not n2:
        return
    lados = melhores_por_mercado.setdefault(m_hash, {})
    if n1:
        lados["outcome_one"] = {"odd": n1[0]["odd"], "liquidity_usdc": sum(x["liquidity_usdc"] for x in n1 if abs(x["odd"] - n1[0]["odd"]) < 0.002)}
    if n2:
        lados["outcome_two"] = {"odd": n2[0]["odd"], "liquidity_usdc": sum(x["liquidity_usdc"] for x in n2 if abs(x["odd"] - n2[0]["odd"]) < 0.002)}


def obter_melhores_odds_sx(mercados_relevantes):
    melhores_por_mercado = {}
    if not mercados_relevantes:
        return melhores_por_mercado
    hashes_unicos = list(dict.fromkeys(m["market_hash"] for m in mercados_relevantes))
    for i in range(0, len(hashes_unicos), 50):
        lote = hashes_unicos[i:i + 50]
        try:
            resp = session.get(f"{SX_BASE}/orders-v3/odds/best", params={"marketHashes": ",".join(lote)}, timeout=20)
            if resp.status_code == 400:
                resp = session.get(f"{SX_BASE}/orders-v3/odds/best", params=[("marketHashes", h) for h in lote], timeout=20)
            if resp.ok:
                payload = resp.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            _registar_livro_mercado_obv3(item.get("marketHash"), item, melhores_por_mercado)
                elif isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            _registar_livro_mercado_obv3(v.get("marketHash") or k, v, melhores_por_mercado)
        except Exception:
            pass
    return melhores_por_mercado


def extrair_opcoes_poly_para_sx(jogo_poly, sx_market):
    team1, team2 = sx_market["team_one"], sx_market["team_two"]
    opcoes = {}
    for m in jogo_poly["markets"]:
        outs, tids = m["outcomes"], m["token_ids"]
        if len(outs) != 2 or len(tids) != 2:
            continue
        for idx, out_name in enumerate(outs):
            tid = tids[idx]
            if equipas_coincidem(out_name, team1) and not equipas_coincidem(out_name, team2):
                opcoes["sx_one"] = {"token_id": tid, "label": f"{team1} (Yes)"}
            elif equipas_coincidem(out_name, team2) and not equipas_coincidem(out_name, team1):
                opcoes["sx_two"] = {"token_id": tid, "label": f"{team2} (Yes)"}
    return opcoes


# ============================================================
# 7. ANALISE COM CRUZAMENTO INTELIGENTE
# ============================================================

def analisar_cruzamento_web3(jogos_poly, mercados_sx, odds_sx, stats):
    alertas_enviados = 0

    for sx_m in mercados_sx:
        m_hash = sx_m["market_hash"]
        if m_hash not in odds_sx:
            continue

        lados_sx = odds_sx[m_hash]
        team1, team2 = sx_m["team_one"], sx_m["team_two"]

        for jogo_poly in jogos_poly:
            if not jogo_coincide_com_titulo(team1, team2, jogo_poly["title"]):
                continue

            opcoes_poly = extrair_opcoes_poly_para_sx(jogo_poly, sx_m)
            if not opcoes_poly:
                continue

            stats["jogos_casados"] += 1
            logger.info("JOGO CASADO COM SUCESSO: %s vs %s <-> %s", team1, team2, jogo_poly["title"])

            pares = [("sx_one", "outcome_two", sx_m["outcome_two"]), ("sx_two", "outcome_one", sx_m["outcome_one"])]
            for poly_key, sx_key, label_sx in pares:
                if poly_key not in opcoes_poly or sx_key not in lados_sx:
                    continue

                opt_p = opcoes_poly[poly_key]
                opt_sx = lados_sx[sx_key]

                ask = obter_preco_websocket_ou_rest(opt_p["token_id"])
                if not ask:
                    continue

                preco_bruto_poly = ask["price"]
                odd_bruta_poly = 1.0 / preco_bruto_poly
                odd_bruta_sx = opt_sx["odd"]

                soma_prob_bruta = (1.0 / odd_bruta_poly) + (1.0 / odd_bruta_sx)
                lucro_pct_bruto = ((1.0 / soma_prob_bruta) - 1.0) * 100.0

                if lucro_pct_bruto < MIN_PROFIT_PCT:
                    continue

                mensagem = (
                    "🛡️ [TESTE SMART V9.2 - ALERTA DE JOGO CASADO]\n\n"
                    f"Jogo: {team1} vs {team2}\n"
                    f"Poly Title: {jogo_poly['title']}\n"
                    f"Poly Preco: {preco_bruto_poly*100:.2f}¢ | Odd: {odd_bruta_poly:.2f}\n"
                    f"SX Bet Odd: {odd_bruta_sx:.2f}\n"
                    f"Margem Calculada: {lucro_pct_bruto:.2f}%\n"
                )
                if enviar_alerta_telegram(mensagem):
                    alertas_enviados += 1
                break

    return alertas_enviados


# ============================================================
# 8. CICLO PRINCIPAL E WEB SERVER
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

    logger.info("Ciclo concluido | Poly Encontrados: %d | SX Encontrados: %d | Casados: %d | Alertas: %d",
                len(jogos_poly), len(mercados_sx), stats["jogos_casados"], total_alertas)


def correr_robo_continuo():
    while True:
        try:
            ciclo_scanner()
        except Exception as e:
            logger.exception("Erro no scanner.")
        time.sleep(SCAN_INTERVAL)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ROBO V9.2 ONLINE")

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    threading.Thread(target=correr_websocket_poly, daemon=True).start()
    threading.Thread(target=correr_robo_continuo, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler).serve_forever()
