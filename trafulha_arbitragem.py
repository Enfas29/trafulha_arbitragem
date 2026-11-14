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
# 1. CONFIGURACOES (100% WEB3: POLYMARKET x SX BET V3)
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

BANKROLL_USD = float(os.getenv("BANKROLL_USD", "100"))
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
# 2. LOGGING E ESTADO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ArbitrageWeb3Bot")

session = requests.Session()
session.headers.update({"User-Agent": "TrafulhaArbitrageWeb3/5.6"})

state_lock = threading.Lock()
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
    "sx_usdc_token": None,
    "mode": "WEB3_POLYMARKET_X_SXBET_V3"
}

alert_history = {}
sx_usdc_token_cache = None
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
    # Futebol Europeu
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
    # NBA
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
# 4. TELEGRAM
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
# 6. SX BET - METADATA USDC E SUMMARY ENDPOINTS V3
# ============================================================

def _procurar_endereco_usdc(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "usdc" in str(k).lower() and isinstance(v, str) and v.startswith("0x") and len(v) == 42:
                return v
            if "usdc" in str(k).lower() and isinstance(v, dict):
                for sub_v in v.values():
                    if isinstance(sub_v, str) and sub_v.startswith("0x") and len(sub_v) == 42:
                        return sub_v

        tem_usdc = any("usdc" in str(v).lower() for v in obj.values() if isinstance(v, str))
        if tem_usdc:
            for k in ("address", "tokenAddress", "baseToken", "contractAddress", "id"):
                val = obj.get(k)
                if isinstance(val, str) and val.startswith("0x") and len(val) == 42:
                    return val
            for val in obj.values():
                if isinstance(val, str) and val.startswith("0x") and len(val) == 42:
                    return val

        for v in obj.values():
            encontrado = _procurar_endereco_usdc(v)
            if encontrado:
                return encontrado

    elif isinstance(obj, list):
        for item in obj:
            encontrado = _procurar_endereco_usdc(item)
            if encontrado:
                return encontrado

    return None


def obter_usdc_token_sx():
    global sx_usdc_token_cache
    if sx_usdc_token_cache:
        return sx_usdc_token_cache

    for meta_path in ("/metadata/obv3", "/metadata/token", "/metadata"):
        try:
            resp = http_get(f"{SX_BASE}{meta_path}", timeout=12)
            addr = _procurar_endereco_usdc(resp)
            if addr:
                sx_usdc_token_cache = addr
                logger.info("SX Bet USDC baseToken detetado (%s): %s", meta_path, addr)
                with state_lock:
                    scanner_status["sx_usdc_token"] = addr
                return addr
        except Exception:
            continue

    sx_usdc_token_cache = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"
    return sx_usdc_token_cache


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
                "bet_group": str(m.get("betGroup") or m.get("group") or ""),
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


def _normalizar_odd_sx(raw_val):
    try:
        v = float(raw_val)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None

    if v > 1e12:
        prob = v / 1e20
        if not (0.01 < prob < 0.99):
            return None
        odd_bruta = 1.0 / prob
    elif 1.01 <= v <= 100.0:
        odd_bruta = v
    elif 0.01 < v < 1.0:
        odd_bruta = 1.0 / v
    else:
        return None

    fator_taxa_sx = 1.0 - (SX_FEE_BUFFER_PCT / 100.0)
    return 1.0 + (odd_bruta - 1.0) * fator_taxa_sx


def _normalizar_liquidez_usdc(raw_val):
    try:
        v = float(raw_val)
    except (ValueError, TypeError):
        return 0.0
    if v <= 0:
        return 0.0
    return (v / 1e6) if v >= 10000 else v


def _extrair_lado_summary(bloco):
    if not isinstance(bloco, dict):
        return None

    odd_val = None
    for k in ("percentageOdds", "odds", "decimalOdds", "bestOdds", "price", "impliedProbability", "takerOdds"):
        if bloco.get(k) is not None:
            odd_val = _normalizar_odd_sx(bloco[k])
            if odd_val:
                break

    if not odd_val:
        return None

    liq_val = 0.0
    for k in ("liquidity", "availableLiquidity", "totalLiquidity", "maxBet", "amount", "size", "takerLiquidity"):
        if bloco.get(k) is not None:
            liq_val = _normalizar_liquidez_usdc(bloco[k])
            if liq_val > 0:
                break

    if liq_val <= 0:
        liq_val = max(MIN_LIQUIDITY_USD, 25.0)

    return {"odd": odd_val, "liquidity_usdc": liq_val}


def _varrer_summaries_sx(obj, melhores_por_mercado, mapa_eventos):
    if isinstance(obj, list):
        for item in obj:
            _varrer_summaries_sx(item, melhores_por_mercado, mapa_eventos)
        return

    if not isinstance(obj, dict):
        return

    m_hash = obj.get("marketHash")

    if not m_hash:
        ev_id = str(obj.get("sportXeventId") or obj.get("eventId") or "")
        m_type = obj.get("type")
        if ev_id and m_type is not None:
            m_hash = mapa_eventos.get((ev_id, m_type))

    if m_hash:
        lados = melhores_por_mercado.setdefault(m_hash, {})

        if isinstance(obj.get("outcomeOne"), dict):
            res1 = _extrair_lado_summary(obj["outcomeOne"])
            if res1:
                lados["outcome_one"] = res1
        if isinstance(obj.get("outcomeTwo"), dict):
            res2 = _extrair_lado_summary(obj["outcomeTwo"])
            if res2:
                lados["outcome_two"] = res2

        for k_odd in ("outcomeOnePercentageOdds", "outcomeOneOdds", "bestOutcomeOneOdds"):
            if obj.get(k_odd) is not None and "outcome_one" not in lados:
                o1 = _normalizar_odd_sx(obj[k_odd])
                if o1:
                    liq1 = 0.0
                    for k_liq in ("outcomeOneLiquidity", "outcomeOneAvailableLiquidity", "outcomeOneMaxBet", "liquidity"):
                        if obj.get(k_liq) is not None:
                            liq1 = _normalizar_liquidez_usdc(obj[k_liq])
                            if liq1 > 0:
                                break
                    lados["outcome_one"] = {
                        "odd": o1,
                        "liquidity_usdc": liq1 if liq1 > 0 else max(MIN_LIQUIDITY_USD, 25.0)
                    }

        for k_odd in ("outcomeTwoPercentageOdds", "outcomeTwoOdds", "bestOutcomeTwoOdds"):
            if obj.get(k_odd) is not None and "outcome_two" not in lados:
                o2 = _normalizar_odd_sx(obj[k_odd])
                if o2:
                    liq2 = 0.0
                    for k_liq in ("outcomeTwoLiquidity", "outcomeTwoAvailableLiquidity", "outcomeTwoMaxBet", "liquidity"):
                        if obj.get(k_liq) is not None:
                            liq2 = _normalizar_liquidez_usdc(obj[k_liq])
                            if liq2 > 0:
                                break
                    lados["outcome_two"] = {
                        "odd": o2,
                        "liquidity_usdc": liq2 if liq2 > 0 else max(MIN_LIQUIDITY_USD, 25.0)
                    }

    for k, v in obj.items():
        if str(k).startswith("0x") and len(str(k)) >= 40 and isinstance(v, dict):
            copia = dict(v, marketHash=k)
            _varrer_summaries_sx(copia, melhores_por_mercado, mapa_eventos)
        elif isinstance(v, (dict, list)):
            _varrer_summaries_sx(v, melhores_por_mercado, mapa_eventos)


def obter_melhores_odds_sx(mercados_relevantes, log_diagnostico=False):
    melhores_por_mercado = {}
    if not mercados_relevantes:
        return melhores_por_mercado

    base_token = obter_usdc_token_sx()
    mapa_eventos = {
        (m["event_id"], m["type"]): m["market_hash"]
        for m in mercados_relevantes
        if m.get("event_id")
    }

    amostras_log = []

    # 1) Consulta global dos jogos futuros e populares em USDC
    for summary_path in (f"/summary/upcoming/{base_token}", f"/summary/popular/{base_token}", f"/summary/inplay/{base_token}"):
        try:
            resp = session.get(f"{SX_BASE}{summary_path}", timeout=20)
            if resp.ok:
                payload = resp.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                _varrer_summaries_sx(data, melhores_por_mercado, mapa_eventos)
                if log_diagnostico and len(amostras_log) < 2:
                    amostras_log.append(f"{summary_path} -> HTTP 200: {json.dumps(data, ensure_ascii=False)[:260]}")
            elif log_diagnostico and len(amostras_log) < 2:
                amostras_log.append(f"{summary_path} -> HTTP {resp.status_code}: {resp.text[:140]}")
        except Exception as e:
            if log_diagnostico and len(amostras_log) < 2:
                amostras_log.append(f"{summary_path} -> ERR: {e}")

    # 2) Consulta por evento individual para mercados relevantes ainda sem odds
    eventos_em_falta = []
    vistos_ev = set()
    for m in mercados_relevantes:
        ev_id = m.get("event_id")
        if ev_id and ev_id not in vistos_ev and m["market_hash"] not in melhores_por_mercado:
            vistos_ev.add(ev_id)
            eventos_em_falta.append(m)

    for m_item in eventos_em_falta[:25]:
        ev_id = m_item["event_id"]
        b_group = m_item.get("bet_group") or "1X2"
        rotas_evento = [
            f"/summary/event/summary/{ev_id}/{base_token}",
            f"/summary/birdseye/{ev_id}/{base_token}",
            f"/summary/event/{ev_id}/{b_group}/{base_token}"
        ]
        for ev_path in rotas_evento:
            try:
                resp = session.get(f"{SX_BASE}{ev_path}", timeout=12)
                if resp.ok:
                    payload = resp.json()
                    data = payload.get("data", payload) if isinstance(payload, dict) else payload
                    _varrer_summaries_sx(data, melhores_por_mercado, mapa_eventos)
                    if log_diagnostico and len(amostras_log) < 3:
                        amostras_log.append(f"{ev_path} -> HTTP 200: {json.dumps(data, ensure_ascii=False)[:260]}")
                    break
                elif log_diagnostico and len(amostras_log) < 3:
                    amostras_log.append(f"{ev_path} -> HTTP {resp.status_code}: {resp.text[:120]}")
            except Exception:
                continue

    if log_diagnostico:
        for linha in amostras_log:
            logger.info("Diagnostico API SX | %s", linha)

    return melhores_por_mercado


# ============================================================
# 7. MOTOR DE CRUZAMENTO: POLYMARKET x SX BET
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

            elif normalizar_texto(out1_sx) in ("tie", "draw") and ("draw" in normalizar_texto(alvo_texto) or "tie" in normalizar_texto(alvo_texto)):
                opcoes["sx_one"] = {
                    "token_id": tids[yes_idx],
                    "indicative_price": iprices[yes_idx],
                    "volume": vol,
                    "label": "Empate (Yes)"
                }
                opcoes["sx_two"] = {
                    "token_id": tids[no_idx],
                    "indicative_price": iprices[no_idx],
                    "volume": vol,
                    "label": "Sem Empate (No)"
                }

            elif equipas_coincidem(alvo_texto, team1) and not equipas_coincidem(alvo_texto, team2):
                if equipas_coincidem(team1, out1_sx):
                    opcoes["sx_one"] = {
                        "token_id": tids[yes_idx],
                        "indicative_price": iprices[yes_idx],
                        "volume": vol,
                        "label": f"{team1} Vence (Yes)"
                    }
                    opcoes["sx_two"] = {
                        "token_id": tids[no_idx],
                        "indicative_price": iprices[no_idx],
                        "volume": vol,
                        "label": f"{team2} Vence (No)"
                    }

            elif equipas_coincidem(alvo_texto, team2) and not equipas_coincidem(alvo_texto, team1):
                if equipas_coincidem(team2, out2_sx):
                    opcoes["sx_two"] = {
                        "token_id": tids[yes_idx],
                        "indicative_price": iprices[yes_idx],
                        "volume": vol,
                        "label": f"{team2} Vence (Yes)"
                    }
                    opcoes["sx_one"] = {
                        "token_id": tids[no_idx],
                        "indicative_price": iprices[no_idx],
                        "volume": vol,
                        "label": f"{team1} Vence (No)"
                    }

    return opcoes


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

                max_banca_poly = liq_poly_usd / prop_poly
                max_banca_sx = opt_sx["liquidity_usdc"] / prop_sx
                banca_efetiva = min(BANKROLL_USD, max_banca_poly, max_banca_sx)

                if banca_efetiva < 10.0:
                    continue

                stake_poly = banca_efetiva * prop_poly
                stake_sx = banca_efetiva * prop_sx
                retorno_usd = banca_efetiva / soma_prob
                lucro_usd = retorno_usd - banca_efetiva

                mensagem = (
                    "Arbitragem Web3 Detetada (Polymarket x SX Bet)!\n\n"
                    f"Jogo: {team1} vs {team2}\n"
                    f"Competicao: {sx_m['sport']} - {sx_m['league']}\n"
                    f"Data e Hora: {formatar_data_pt(dt_sx or dt_poly)}\n\n"
                    f"Lado 1 (Polymarket - USDC):\n"
                    f"- Aposta: {opt_p['label']}\n"
                    f"- Odd Liquida: {odd_poly:.2f} (Preco: ${ask['price']:.3f})\n"
                    f"- Valor a colocar: ${stake_poly:.2f} USDC\n"
                    f"- Liquidez no topo: ${liq_poly_usd:.2f} USDC\n\n"
                    f"Lado 2 (SX Bet - USDC):\n"
                    f"- Aposta: {label_sx}\n"
                    f"- Odd Taker: {odd_sx:.2f}\n"
                    f"- Valor a colocar: ${stake_sx:.2f} USDC\n"
                    f"- Liquidez disponivel: ${opt_sx['liquidity_usdc']:.2f} USDC\n\n"
                    f"Investimento Total: ${banca_efetiva:.2f} USDC\n"
                    f"Retorno Garantido: ~${retorno_usd:.2f} USDC\n"
                    f"Lucro Liquido: {lucro_usd:+.2f} USDC (~{lucro_pct:.2f}%)"
                )

                key = gerar_chave_alerta(f"web3_{m_hash}_{poly_key}_{round(odd_poly,2)}_{round(odd_sx,2)}")
                if enviar_alerta_telegram(mensagem, key):
                    alertas_enviados += 1

    return alertas_enviados


# ============================================================
# 8. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():
    global diagnostico_inicial_feito
    logger.info("A iniciar scanner Web3 V5.6 (Polymarket x SX Bet | MIN_PROFIT = %s%%).", MIN_PROFIT_PCT)

    stats = {
        "jogos_casados": 0,
        "melhor_margem": None
    }

    jogos_poly = obter_jogos_polymarket()
    mercados_sx = obter_mercados_sx()

    mercados_relevantes = []
    exemplo_par = None
    for sx_m in mercados_sx:
        for jp in jogos_poly:
            if jogo_coincide_com_titulo(sx_m["team_one"], sx_m["team_two"], jp["title"]):
                mercados_relevantes.append(sx_m)
                if exemplo_par is None:
                    exemplo_par = (sx_m, jp)
                break

    fazer_diag = not diagnostico_inicial_feito
    odds_sx = obter_melhores_odds_sx(mercados_relevantes, log_diagnostico=fazer_diag) if mercados_relevantes else {}

    if fazer_diag:
        diagnostico_inicial_feito = True
        ex_str = (
            f"SX[{exemplo_par[0]['team_one']} vs {exemplo_par[0]['team_two']} | out: {exemplo_par[0]['outcome_one']}/{exemplo_par[0]['outcome_two']} | ev:{exemplo_par[0]['event_id']} | bg:{exemplo_par[0]['bet_group']}] "
            f"<-> Poly[{exemplo_par[1]['title']}]"
        ) if exemplo_par else "Nenhum"
        logger.info(
            "Diagnostico Cruzamento | Pares por nome: %s | Mercados SX com odds: %s | Exemplo: %s",
            len(mercados_relevantes),
            len(odds_sx),
            ex_str
        )

    total_alertas = analisar_cruzamento_web3(jogos_poly, mercados_relevantes, odds_sx, stats)

    melhor_str = f"{stats['melhor_margem']:.2f}%" if stats["melhor_margem"] is not None else "N/A"
    logger.info(
        "Ciclo concluido | Jogos Polymarket: %s | Mercados SX Bet: %s | Pares nome: %s | Jogos casados: %s | Melhor margem: %s | Alertas: %s",
        len(jogos_poly),
        len(mercados_sx),
        len(mercados_relevantes),
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
            logger.exception("Erro geral do scanner Web3.")
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
            data["auto_bet"] = AUTO_BET
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = (
                "ROBO ARBITRAGEM WEB3 V5.6 ONLINE\n"
                "Plataformas: Polymarket x SX Bet V3 (USDC)\n"
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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")

    scanner_thread = threading.Thread(target=correr_robo_continuo, daemon=True)
    scanner_thread.start()
    iniciar_servidor_web()
