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
# 1. CONFIGURACOES (PAPER TRADING V6.1 - EMPATE COBERTO)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
SX_API_KEY = os.getenv("SX_API_KEY", "")

PAPER_TRADING = True
PAPER_INITIAL_BANKROLL = float(os.getenv("PAPER_INITIAL_BANKROLL", "1000"))
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "2000"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "15"))

POLY_FEE_BUFFER_PCT = float(os.getenv("POLY_FEE_BUFFER_PCT", "1.0"))
SX_FEE_BUFFER_PCT = float(os.getenv("SX_FEE_BUFFER_PCT", "0.0"))

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "10"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "300"))

ALERT_COOLDOWN = 1800
AUTO_BET = False

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
SX_BASE = "https://api.sx.bet"


# ============================================================
# 2. LOGGING E ESTADO DA CARTEIRA VIRTUAL
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ArbitragePaperBot")

session = requests.Session()
session.headers.update({"User-Agent": "TrafulhaPaperBot/6.1"})
if SX_API_KEY:
    session.headers.update({"x-sx-api-key": SX_API_KEY})

state_lock = threading.Lock()
wallet_lock = threading.Lock()

paper_wallet = {
    "initial_bankroll": PAPER_INITIAL_BANKROLL,
    "current_bankroll": PAPER_INITIAL_BANKROLL,
    "total_staked": 0.0,
    "total_profit": 0.0,
    "bets_placed": 0,
    "history": []
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
    "sx_odds_endpoint": None,
    "mode": "WEB3_PAPER_TRADING_SAFE_DRAW_V6.1"
}

alert_history = {}
diagnostico_inicial_feito = False


# ============================================================
# 3. UTILITARIOS HTTP E NORMALIZACAO DE EQUIPAS
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
    "borussia dortmund": "dortmund",
    "atlanta hawks": "hawks",
    "boston celtics": "celtics",
    "brooklyn nets": "nets",
    "charlotte hornets": "hornets",
    "chicago bulls": "bulls",
    "cleveland cavaliers": "cavaliers",
    "dallas mavericks": "mavericks",
    "denver nuggets": "nuggets",
    "detroit pistons": "pistons",
    "golden state warriors": "warriors",
    "houston rockets": "rockets",
    "indiana pacers": "pacers",
    "los angeles clippers": "clippers",
    "la clippers": "clippers",
    "los angeles lakers": "lakers",
    "la lakers": "lakers",
    "memphis grizzlies": "grizzlies",
    "miami heat": "heat",
    "milwaukee bucks": "bucks",
    "minnesota timberwolves": "timberwolves",
    "new orleans pelicans": "pelicans",
    "new york knicks": "knicks",
    "oklahoma city thunder": "thunder",
    "orlando magic": "magic",
    "philadelphia 76ers": "76ers",
    "phoenix suns": "suns",
    "portland trail blazers": "trail blazers",
    "sacramento kings": "kings",
    "san antonio spurs": "spurs",
    "toronto raptors": "raptors",
    "utah jazz": "jazz",
    "washington wizards": "wizards"
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
# 4. TELEGRAM E PAPER WALLET
# ============================================================

def enviar_alerta_telegram(mensagem, alert_key=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")
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
        logger.info("Alerta enviado para Telegram!")
        return True
    except requests.RequestException as e:
        logger.error("Erro de rede Telegram: %s", e)
        return False


def gerar_chave_alerta(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def registar_aposta_virtual(jogo_nome, stake_total, lucro_esperado, odds_detalhes):
    with wallet_lock:
        paper_wallet["bets_placed"] += 1
        paper_wallet["total_staked"] += stake_total
        paper_wallet["total_profit"] += lucro_esperado
        paper_wallet["current_bankroll"] += lucro_esperado
        
        registo = {
            "id": paper_wallet["bets_placed"],
            "time": datetime.now(timezone.utc).isoformat(),
            "match": jogo_nome,
            "stake": stake_total,
            "expected_profit": lucro_esperado,
            "details": odds_detalhes,
            "bankroll_after": paper_wallet["current_bankroll"]
        }
        paper_wallet["history"].append(registo)
        logger.info("PAPER TRADING | Aposta registada #${} | Jogo: {} | Stake: ${:.2f} | Lucro Est.: +${:.2f} | Nova Banca: ${:.2f}".format(
            registo["id"], jogo_nome, stake_total, lucro_esperado, paper_wallet["current_bankroll"]
        ))


# ============================================================
# 5. POLYMARKET - RECOLHA DE JOGOS E ORDER BOOK CLOB
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

                q_lower = str(m.get("question", "")).lower()
                g_lower = str(m.get("groupItemTitle", "")).lower()
                if any(w in f"{q_lower} {g_lower}" for w in ["spread", "over", "under", "total", "goals", "points", "handicap", "o/u"]):
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
                    "slug": ev.get("slug", ""),
                    "end_date": parse_timestamp_utc(ev.get("endDate") or ev.get("startDate")),
                    "volume": ev_volume,
                    "markets": submercados
                })

            if len(jogos_poly) >= MAX_POLY_MARKETS:
                return jogos_poly

    return jogos_poly


# ============================================================
# 6. SX BET - MERCADOS ATIVOS E ORDERBOOK OBV3 (USDC)
# ============================================================

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
            logger.error("Erro ao ler mercados SX Bet: %s", e)
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


def obter_melhores_odds_sx(mercados_relevantes, log_diagnostico=False):
    melhores_por_mercado = {}
    if not mercados_relevantes:
        return melhores_por_mercado

    hashes_unicos = list(dict.fromkeys(m["market_hash"] for m in mercados_relevantes))
    rota_usada = None
    diag_msg = None

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
                rota_usada = "/orders-v3/odds/best"
                if diag_msg is None:
                    diag_msg = f"/orders-v3/odds/best -> HTTP 200: {json.dumps(data, ensure_ascii=False)[:260]}"
            elif diag_msg is None:
                diag_msg = f"/orders-v3/odds/best -> HTTP {resp.status_code}: {resp.text[:160]}"
        except Exception as e:
            if diag_msg is None:
                diag_msg = f"/orders-v3/odds/best -> ERR: {e}"

    if not melhores_por_mercado:
        for m_hash in hashes_unicos[:40]:
            try:
                resp = session.get(
                    f"{SX_BASE}/orderbook-v3/snapshot/market",
                    params={"marketHash": m_hash},
                    timeout=12
                )
                if resp.ok:
                    payload = resp.json()
                    data = payload.get("data", payload) if isinstance(payload, dict) else payload
                    _parsear_payload_obv3(data, melhores_por_mercado, m_hash_fallback=m_hash)
                    if rota_usada is None:
                        rota_usada = "/orderbook-v3/snapshot/market"
                    if diag_msg is None or "HTTP 200" not in diag_msg:
                        diag_msg = f"/orderbook-v3/snapshot/market -> HTTP 200: {json.dumps(data, ensure_ascii=False)[:260]}"
                elif diag_msg is None:
                    diag_msg = f"/orderbook-v3/snapshot/market -> HTTP {resp.status_code}: {resp.text[:160]}"
            except Exception:
                continue

    if rota_usada:
        with state_lock:
            scanner_status["sx_odds_endpoint"] = rota_usada

    if log_diagnostico and diag_msg:
        logger.info("Diagnostico SX OBv3 | %s", diag_msg)

    return melhores_por_mercado


# ============================================================
# 7. MOTOR DE CRUZAMENTO: POLYMARKET x SX BET (COM FILTRO DE EMPATE)
# ============================================================

def extrair_opcoes_poly_para_sx(jogo_poly, sx_market):
    out1_sx = sx_market["outcome_one"]
    out2_sx = sx_market["outcome_two"]
    team1 = sx_market["team_one"]
    team2 = sx_market["team_two"]

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
                if equipas_coincidem(out_name, out1_sx) and not equipas_coincidem(out_name, out2_sx):
                    opcoes["sx_one"] = {
                        "token_id": tids[idx],
                        "indicative_price": iprices[idx],
                        "volume": vol,
                        "label": out_name
                    }
                elif equipas_coincidem(out_name, out2_sx) and not equipas_coincidem(out_name, out1_sx):
                    opcoes["sx_two"] = {
                        "token_id": tids[idx],
                        "indicative_price": iprices[idx],
                        "volume": vol,
                        "label": out_name
                    }

        elif "yes" in outs_norm and "no" in outs_norm:
            yes_idx = outs_norm.index("yes")
            no_idx = outs_norm.index("no")
            alvo_texto = g if g else q

            # Filtro rigoroso: So aceita se cobrir o empate (Not Team ou Dupla Hipoteses)
            is_not_bet = normalizar_texto(out2_sx).startswith("not ") or "not " in normalizar_texto(out1_sx)
            is_draw_covered = "draw" in normalizar_texto(alvo_texto) or "tie" in normalizar_texto(alvo_texto) or is_not_beta_safe(out1_sx, out2_sx)

            if is_not_bet or is_draw_covered:
                if normalizar_texto(out2_sx).startswith("not ") and equipas_coincidem(alvo_texto, out1_sx):
                    opcoes["sx_one"] = {
                        "token_id": tids[yes_idx],
                        "indicative_price": iprices[yes_idx],
                        "volume": vol,
                        "label": f"{out1_sx} (Yes)"
                    }
                    opcoes["sx_two"] = {
                        "token_id": tids[no_idx],
                        "indicative_price": iprices[no_idx],
                        "volume": vol,
                        "label": f"{out2_sx} (No)"
                    }
                elif normalizar_texto(out1_sx).startswith("not ") and equipas_coincidem(alvo_texto, out2_sx):
                    opcoes["sx_two"] = {
                        "token_id": tids[yes_idx],
                        "indicative_price": iprices[yes_idx],
                        "volume": vol,
                        "label": f"{out2_sx} (Yes)"
                    }
                    opcoes["sx_one"] = {
                        "token_id": tids[no_idx],
                        "indicative_price": iprices[no_idx],
                        "volume": vol,
                        "label": f"{out1_sx} (No)"
                    }

    return opcoes


def is_not_beta_safe(out1, out2):
    o1 = normalizar_texto(out1)
    o2 = normalizar_texto(out2)
    return o1.startswith("not ") or o2.startswith("not ")


def calcular_odd_liquida_poly(ask_price):
    fator_taxa = 1.0 + (POLY_FEE_BUFFER_PCT / 100.0)
    preco_com_taxa = ask_price * fator_taxa
    if preco_com_taxa >= 1.0:
        return None
    return 1.0 / preco_com_taxa


def analisar_cruzamento_web3(jogos_poly, mercados_sx, odds_sx, stats):
    alertas_enviados = 0

    fator_min = max(0.5, 1.0 + (MIN_PROFIT_PCT / 100.0))
    max_soma_prob = 1.0 / fator_min
    limite_prefiltro = max_soma_prob + 0.03

    for sx_m in mercados_sx:
        m_hash = sx_m["market_hash"]
        if m_hash not in odds_sx:
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

                if opt_p["indicative_price"] > 0:
                    odd_ind = calcular_odd_liquida_poly(opt_p["indicative_price"])
                    if odd_ind and (1.0 / odd_ind + 1.0 / opt_sx["odd"]) > limite_prefiltro:
                        continue

                ask = obter_melhor_ask_clob(opt_p["token_id"])
                if not ask:
                    continue

                liq_poly_usd = ask["size"] * ask["price"]
                if liq_poly_usd < MIN_LIQUIDITY_USD:
                    continue

                odd_poly = calcular_odd_liquida_poly(ask["price"])
                if not odd_poly:
                    continue

                odd_sx = opt_sx["odd"]
                soma_prob = (1.0 / odd_poly) + (1.0 / odd_sx)
                lucro_pct = ((1.0 / soma_prob) - 1.0) * 100.0

                if stats["melhor_margem"] is None or lucro_pct > stats["melhor_margem"]:
                    stats["melhor_margem"] = lucro_pct

                if lucro_pct < MIN_PROFIT_PCT:
                    continue

                prop_poly = (1.0 / odd_poly) / soma_prob
                prop_sx = (1.0 / odd_sx) / soma_prob

                banca_efetiva = min(PAPER_STAKE_USD, liq_poly_usd / prop_poly, opt_sx["liquidity_usdc"] / prop_sx)

                if banca_efetiva < 10.0:
                    continue

                stake_poly = banca_efetiva * prop_poly
                stake_sx = banca_efetiva * prop_sx
                retorno_usd = banca_efetiva / soma_prob
                lucro_usd = retorno_usd - banca_efetiva

                preco_cents = round(ask["price"] * 100)

                mensagem = (
                    "🛡️ [PAPER TRADING - EMPATE COBERTO]\n\n"
                    f"Jogo: {team1} vs {team2}\n"
                    f"Competicao: {sx_m['sport']} - {sx_m['league']}\n"
                    f"Data e Hora: {formatar_data_pt(dt_sx or dt_poly)}\n\n"
                    f"Lado 1 (Polymarket - USDC):\n"
                    f"- Aposta: {opt_p['label']}\n"
                    f"- Preco Polymarket: {preco_cents}¢ (Odd Liquida: {odd_poly:.2f})\n"
                    f"- Valor simulado: ${stake_poly:.2f} USDC\n"
                    f"- Liquidez no topo: ${liq_poly_usd:.2f} USDC\n\n"
                    f"Lado 2 (SX Bet - USDC):\n"
                    f"- Aposta: {label_sx}\n"
                    f"- Odd Taker: {odd_sx:.2f}\n"
                    f"- Valor simulado: ${stake_sx:.2f} USDC\n"
                    f"- Liquidez disponivel: ${opt_sx['liquidity_usdc']:.2f} USDC\n\n"
                    f"Investimento Total Simulado: ${banca_efetiva:.2f} USDC\n"
                    f"Retorno Garantido: ~${retorno_usd:.2f} USDC\n"
                    f"Lucro Liquido Simulado: {lucro_usd:+.2f} USDC (~{lucro_pct:.2f}%)\n"
                    f"💰 Nova Banca Virtual: ${paper_wallet['current_bankroll'] + lucro_usd:.2f}"
                )

                key = gerar_chave_alerta(f"paper_safe_{m_hash}_{poly_key}_{round(odd_poly,2)}_{round(odd_sx,2)}")
                if enviar_alerta_telegram(mensagem, key):
                    alertas_enviados += 1
                    registar_aposta_virtual(f"{team1} vs {team2}", banca_efetiva, lucro_usd, {
                        "poly_label": opt_p['label'], "poly_price": preco_cents, "poly_stake": stake_poly,
                        "sx_label": label_sx, "sx_odd": odd_sx, "sx_stake": stake_sx
                    })

    return alertas_enviados


# ============================================================
# 8. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():
    global diagnostico_inicial_feito
    logger.info("A iniciar scanner Paper Trading V6.1 (Empate Coberto | Banca: $%s | Stake: $%s).", 
                paper_wallet["current_bankroll"], PAPER_STAKE_USD)

    stats = {
        "jogos_casados": 0,
        "melhor_margem": None
    }

    jogos_poly = obter_jogos_polymarket()
    mercados_sx = obter_mercados_sx()

    mercados_relevantes = [
        sx_m for sx_m in mercados_sx
        if any(jogo_coincide_com_titulo(sx_m["team_one"], sx_m["team_two"], jp["title"]) for jp in jogos_poly)
    ]

    fazer_diag = not diagnostico_inicial_feito
    odds_sx = obter_melhores_odds_sx(mercados_relevantes, log_diagnostico=fazer_diag) if mercados_relevantes else {}

    if fazer_diag:
        diagnostico_inicial_feito = True
        logger.info(
            "Diagnostico Cruzamento | Pares por nome: %s | Mercados SX com odds OBv3: %s",
            len(mercados_relevantes),
            len(odds_sx)
        )

    total_alertas = analisar_cruzamento_web3(jogos_poly, mercados_relevantes, odds_sx, stats)

    melhor_str = f"{stats['melhor_margem']:.2f}%" if stats["melhor_margem"] is not None else "N/A"
    logger.info(
        "Ciclo concluido | Banca Virtual: ${:.2f} | Jogos Polymarket: {} | Mercados SX: {} | Casados: {} | Melhor margem: {} | Alertas: {}",
        paper_wallet["current_bankroll"],
        len(jogos_poly),
        len(mercados_sx),
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
            logger.exception("Erro geral do scanner Paper Trading.")
            with state_lock:
                scanner_status["last_error"] = str(e)
        time.sleep(SCAN_INTERVAL)


# ============================================================
# 9. SERVIDOR WEB RENDER
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
                banca_atual = paper_wallet["current_bankroll"]
                lucro_total = paper_wallet["total_profit"]
                total_apostas = paper_wallet["bets_placed"]
            body = (
                "ROBO ARBITRAGEM PAPER TRADING V6.1 ONLINE (EMPATE COBERTO)\n"
                f"Banca Virtual: ${banca_atual:.2f} | Lucro Acumulado: ${lucro_total:+.2f} | Apostas: {total_apostas}\n"
                "Modo: PAPER TRADING COM COBERTURA DE EMPATE\n"
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

    scanner_thread = threading.Thread(target=correr_robo_continuo, daemon=True)
    scanner_thread.start()
    iniciar_servidor_web()
