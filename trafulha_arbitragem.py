
"""Paper Trading V11.0 — Polymarket + SX Bet, apenas simulação.

Requisitos: pip install requests websocket-client centrifuge-python aiohttp
Configuração: SX_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, EUR_PER_USD.
EXECUÇÃO REAL NÃO EXISTE NESTE PROGRAMA.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SX_API = "https://api.sx.bet"
SX_WS = "wss://realtime.sx.bet/connection/websocket"

SX_KEY = os.getenv("SX_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FX = float(os.getenv("EUR_PER_USD", "0") or "0")  # Variável vazia no CI = sem cotação

INITIAL_EUR = 1000.0
MAX_TOTAL_EUR = 100.0
MIN_TOTAL_EUR = 5.0
MIN_NET_ROI = 0.015              # 1,5% após buffers
POLY_FEE_BUFFER = 0.010        # estimativa prudente; não é taxa oficial
SX_FEE_BUFFER = 0.010          # estimativa prudente; não é taxa oficial
SCANNER_SECONDS = 10          # fallback REST, mais rápido com sinais WS
BOOK_TIMEOUT_SECONDS = min(7.0, max(1.0, float(
    os.getenv("MONITOR_BOOK_TIMEOUT_SECONDS", "2.0")
)))
CATALOG_SECONDS = 240        # atualizar novas partidas/mercados
MAX_SX_PAGES = 30
MAX_POLY_SERIES = 70
MAX_EVENTS_PER_SERIES = 150
# Discovery runs off the hot path. Bound both each series and the whole refresh.
POLY_MAX_PAGES_PER_SERIES = min(10, max(1, int(
    os.getenv("MONITOR_POLY_PAGES_PER_SERIES", "4")
)))
POLY_MAX_REQUESTS_PER_REFRESH = min(400, max(10, int(
    os.getenv("MONITOR_POLY_REQUEST_BUDGET", "140")
)))
DISCOVERY_STATS = {}
_POLY_SERIES_CURSOR = 0
_MATCHED_POLY_SERIES = set()
MAX_PAIRS_PER_SCAN = 25       # controla requests; aumenta gradualmente
MAX_LIVE_AGE_H = 8
MAX_FUTURE_H = 168            # jogos nos próximos 7 dias
MAX_START_DIFF_H = 3
MAX_SNAPSHOT_GAP_S = 6
DB_FILE = os.getenv("PAPER_DB", "paper_v11.sqlite3")

# Só mercados binários de vencedor, nunca spreads/totais ou 1X2 com empate.
# Tipo 52 = 12; tipo 226 = 12 incluindo prolongamento.
ALLOWED_SX_TYPES = {52, 226}
# Futebol e hóquei podem ter empate/regras diferentes: excluir até validar
# contratos de liquidação nos dois lados. Continua a descobri-los nos catálogos.
EXCLUDED_SETTLEMENT_SPORTS = {"soccer", "hockey"}

LOG = logging.getLogger("paper_v11")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "PaperTradingV11/1.0", "Accept": "application/json"})
# Nunca colocar a chave SX nos cabeçalhos globais: também usamos Gamma/CLOB.
# Sessions não são partilhadas entre threads; reutilizar conexões por worker.
_HTTP_LOCAL = threading.local()

def http_session():
    if threading.current_thread() is threading.main_thread():
        return HTTP
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "PaperTradingV13/1.0", "Accept": "application/json"})
        _HTTP_LOCAL.session = session
    return session

wake = threading.Event()
watch_lock = threading.Lock()
watched_assets: set[str] = set()
watched_sx_hashes: set[str] = set()
poly_ws_client = None


def get_json(url, params=None, timeout=12):
    # Aplicar a chave somente a api.sx.bet, nunca a hosts terceiros.
    sx_headers = {"x-sx-api-key": SX_KEY} if (
        SX_KEY and url.startswith(SX_API + "/")
    ) else None
    r = http_session().get(url, params=params, headers=sx_headers, timeout=timeout, allow_redirects=False)
    if 300 <= getattr(r, "status_code", 0) < 400:
        raise ValueError("Redirecionamento inesperado da API")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "failure":
        raise ValueError(f"API failure: {data.get('message', data)}")
    return data


def telegram(message):
    if not (TG_TOKEN and TG_CHAT):
        LOG.info("Telegram não configurado | %s", message.replace("\n", " | "))
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": message, "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise ValueError(response.text[:200])
    except Exception as exc:
        # requests pode incluir o URL com token na mensagem da exceção.
        LOG.error("Telegram falhou: %s", type(exc).__name__)


def values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except ValueError:
            return []
    return []


def ascii_name(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b(fc|cf|afc|sc|bc|the)\b", " ", text)
    return " ".join(text.split())


ALIASES = {
    "psg": "paris saint germain",
    "man utd": "manchester united",
    "man city": "manchester city",
    "ny knicks": "new york knicks",
    "la lakers": "los angeles lakers",
    "gs warriors": "golden state warriors",
}


def comparable(name):
    n = ascii_name(name)
    return ascii_name(ALIASES.get(n, n))


def same_team(a, b):
    a, b = comparable(a), comparable(b)
    if not a or not b:
        return False
    if a == b:
        return True
    aa, bb = a.split(), b.split()
    # Último nome individual ou alcunha da equipa; exige as duas equipas do evento.
    small, large = (aa, bb) if len(aa) <= len(bb) else (bb, aa)
    return len(small) == 1 and len(small[0]) >= 5 and small[0] == large[-1]


def parse_title(title):
    # Ex.: "NBA: Lakers vs Celtics", "Player A @ Player B"
    text = str(title).split(":", 1)[-1].strip()
    parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", text, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    return (parts[0].strip(), parts[1].strip())


def sport_family(label):
    """Classifica sem confundir NFL com futebol e sem inventar ligas."""
    name = ascii_name(label)
    if not name:
        return "unknown"
    if name.startswith("cric") or name in {"cricket"}:
        return "cricket"
    if name.startswith("hb") or "handball" in name:
        return "handball"
    if name in {"nba", "wnba", "ncaab", "ncaaw"} or any(
        x in name for x in ("basketball", "ncaa basketball")
    ):
        return "basketball"
    if name in {"nfl", "cfb", "ncaaf"} or any(
        x in name for x in ("american football", "college football")
    ):
        return "american_football"
    if any(x in name for x in ("tennis", "atp", "wta", "challenger")):
        return "tennis"
    if name in {"mlb", "kbo", "npb"} or "baseball" in name:
        return "baseball"
    if name == "nhl" or "hockey" in name:
        return "hockey"
    if any(x in name for x in ("soccer", "epl", "la liga", "ucl", "football league", "premier league")):
        return "soccer"
    if any(x in name for x in ("ufc", "mma", "boxing", "mixed martial")):
        return "combat"
    if any(x in name for x in ("esport", "counter strike", "dota", "league of legends")):
        return "esports"
    for family in ("rugby", "volleyball"):
        if family in name:
            return family
    # 'football' isolado é ambíguo; depende do sportId ou da liga.
    return name


SX_SPORTS_BY_ID = None


def sx_sport_family(market):
    """Preferir rótulo fornecido pela SX; IDs são consultados dinamicamente.

    Os números de IDs variam entre exemplos e versões da documentação;
    nunca adivinhar a modalidade a partir de um número fixo.
    """
    label = ascii_name(market.get("sportLabel", ""))
    if label in {"football", "american football", "gridiron"}:
        return "american_football"
    if label in {"soccer", "association football"}:
        return "soccer"
    if label in {"e sports", "esports"}:
        return "esports"
    if label in {"mixed martial arts", "mma"}:
        return "combat"
    if label:
        return sport_family(label)

    league_family = sport_family(market.get("leagueLabel", ""))
    if league_family in {
        "american_football", "basketball", "baseball", "hockey",
        "soccer", "tennis", "combat", "cricket", "esports",
    }:
        return league_family

    global SX_SPORTS_BY_ID
    try:
        sport_id = int(market.get("sportId", market.get("sportID")))
    except (TypeError, ValueError):
        return "unknown"

    if SX_SPORTS_BY_ID is None:
        SX_SPORTS_BY_ID = {}
        try:
            payload = get_json(SX_API + "/sports", timeout=8)
            data = payload.get("data", payload) if isinstance(payload, dict) else payload
            if isinstance(data, dict):
                data = data.get("sports", data.get("items", []))
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    sid = item.get("sportId", item.get("sportID"))
                    sname = item.get("label", item.get("sportLabel", ""))
                    try:
                        sx_id = int(sid)
                    except (TypeError, ValueError):
                        continue
                    sname = ascii_name(sname)
                    if sname == "football":
                        SX_SPORTS_BY_ID[sx_id] = "american_football"
                    elif sname == "e sports":
                        SX_SPORTS_BY_ID[sx_id] = "esports"
                    else:
                        SX_SPORTS_BY_ID[sx_id] = sport_family(sname)
        except Exception as exc:
            LOG.warning("Não foi possível ler /sports da SX: %s", type(exc).__name__)
    return SX_SPORTS_BY_ID.get(sport_id, "unknown")


def unix_time(value):
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            return v / 1000 if v > 10**11 else v
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        pass
    return None


def match_time(poly_ts, sx_ts):
    return bool(poly_ts and sx_ts and abs(poly_ts - sx_ts) <= MAX_START_DIFF_H * 3600)


def is_poly_moneyline(market):
    """Aceitar apenas mercados de vencedor (moneyline) identificados pela Gamma.

    Rejeitar tipos vazios/desconhecidos, spreads, totais e parciais mesmo
    quando os resultados têm nomes de equipas iguais.
    """
    return str(market.get("sportsMarketType") or "").strip().lower() == "moneyline"


def _discovery_stats(source, stats):
    """Publish one complete, JSON-safe diagnostics snapshot, never API secrets."""
    DISCOVERY_STATS[source] = dict(stats)
    LOG.info("DISCOVERY_V15 | %s | %s", source,
             json.dumps(stats, sort_keys=True, separators=(",", ":")))


def _select_poly_series(series):
    """Retain matched leagues; rotate the remaining compatible leagues fairly.

    This is metadata selection, never a cache of old quotes or old markets.
    All returned markets are fetched anew in the current discovery pass.
    """
    global _POLY_SERIES_CURSOR
    priorities = {
        "basketball": 0, "american_football": 1, "tennis": 2,
        "baseball": 3, "cricket": 4, "combat": 5, "esports": 6,
    }
    eligible = sorted(
        ((sid, sport) for sid, sport in series.items() if sport in priorities),
        key=lambda item: (priorities[item[1]], int(item[0])),
    )
    limit = min(MAX_POLY_SERIES, POLY_MAX_REQUESTS_PER_REFRESH)
    # Leave at least a quarter of the allowance for discoveries whenever the
    # catalogue is larger than the allowance; matched leagues cannot starve it.
    anchor_limit = limit if len(eligible) <= limit else max(0, 3 * limit // 4)
    anchors = [item for item in eligible if item[0] in _MATCHED_POLY_SERIES][:anchor_limit]
    anchored = {sid for sid, _ in anchors}
    rotating = [item for item in eligible if item[0] not in anchored]
    count = min(max(0, limit - len(anchors)), len(rotating))
    offset = _POLY_SERIES_CURSOR % len(rotating) if rotating else 0
    chosen = [rotating[(offset + i) % len(rotating)] for i in range(count)]
    _POLY_SERIES_CURSOR = (offset + count) % len(rotating) if rotating else 0
    return anchors + chosen, len(eligible)


def _poly_markets_from_event(event, family, sid, fetched_epoch, counts):
    teams = parse_title(event.get("title", ""))
    if not teams:
        counts["unparsed_event_name"] += 1
        return []
    result = []
    for m in event.get("markets", []) or []:
        if not isinstance(m, dict):
            counts["invalid_market"] += 1
            continue
        if m.get("closed") or m.get("active") is False:
            counts["inactive_market"] += 1
            continue
        if not is_poly_moneyline(m):
            counts["unsupported_market_type"] += 1
            continue
        outs = values(m.get("outcomes"))
        ids = values(m.get("positionIds")) if m.get("version") == "v2" else values(m.get("clobTokenIds"))
        if len(outs) != 2 or len(ids) != 2:
            counts["invalid_outcomes_or_assets"] += 1
            continue
        if {ascii_name(x) for x in outs} == {"yes", "no"}:
            counts["yes_no_not_equivalent"] += 1
            continue
        if not ((same_team(outs[0], teams[0]) and same_team(outs[1], teams[1]))
                or (same_team(outs[1], teams[0]) and same_team(outs[0], teams[1]))):
            counts["outcomes_not_participants"] += 1
            continue
        start = unix_time(m.get("gameStartTime") or event.get("startTime") or event.get("gameStartTime"))
        if not start or m.get("id") is None:
            counts["missing_start_or_id"] += 1
            continue
        result.append({
            "id": str(m["id"]), "sport": family, "teams": teams,
            "event_id": str(event["id"]) if event.get("id") is not None else None,
            "event_live": event.get("live") is True,
            "event_ended": event.get("ended") is True,
            "title": event.get("title", ""), "start": start,
            "outcomes": outs, "assets": [str(x) for x in ids],
            "slug": event.get("slug", ""), "type": m.get("sportsMarketType"),
            "version": m.get("version"), "series_id": sid,
            # Retain official cost fields without coercing missing/invalid
            # data to zero. The independent costs module validates them.
            "feesEnabled": m.get("feesEnabled"),
            "feeSchedule": m.get("feeSchedule"),
            "fees_fetched_epoch": fetched_epoch,
        })
    return result


def discover_poly():
    try:
        metadata = get_json(POLY_GAMMA + "/sports")
    except Exception as exc:
        _discovery_stats("poly", {"complete": False, "fetch_errors": 1,
                                  "error": type(exc).__name__})
        raise
    if not isinstance(metadata, list):
        _discovery_stats("poly", {"complete": False, "fetch_errors": 1,
                                  "error": "invalid_sports_response"})
        raise ValueError("Gamma sports response invalid")
    series = {}
    for row in metadata:
        if not isinstance(row, dict):
            continue
        family = sport_family(row.get("sport") or row.get("name"))
        for sid in re.findall(r"\d+", str(row.get("series", ""))):
            series[sid] = family
    selected, eligible_count = _select_poly_series(series)
    LOG.info("Gamma series: %d conhecidas, %d compatíveis, %d selecionadas; rotação V15",
             len(series), eligible_count, len(selected))
    counts = Counter()
    queue = deque((sid, family, 0, 0) for sid, family in selected)
    result, seen_events, seen_markets = [], set(), set()
    truncated_series, complete_series, failed_series = set(), set(), set()
    requests = 0
    # Breadth-first: every selected series gets a first page before a busy
    # series can consume the entire budget with its following pages.
    while queue and requests < POLY_MAX_REQUESTS_PER_REFRESH:
        sid, family, offset, page_index = queue.popleft()
        requests += 1
        try:
            events = get_json(POLY_GAMMA + "/events", {
                "active": "true", "closed": "false", "series_id": sid,
                "limit": MAX_EVENTS_PER_SERIES, "offset": offset,
            })
            if not isinstance(events, list):
                raise ValueError("Gamma events response invalid")
        except Exception as exc:
            counts["fetch_errors"] += 1
            failed_series.add(sid)
            LOG.warning("Gamma series %s page %d: %s; snapshot incompleto",
                        sid, page_index + 1, type(exc).__name__)
            # Retry the catalogue as a unit; do not spend another full request
            # budget while the upstream is unavailable.
            break
        fetched_epoch = time.time()
        counts["pages"] += 1
        counts["events_received"] += len(events)
        for event in events:
            if not isinstance(event, dict) or event.get("id") is None:
                counts["invalid_event"] += 1
                continue
            eid = str(event["id"])
            if eid in seen_events:
                counts["duplicate_event"] += 1
                continue
            seen_events.add(eid)
            for market in _poly_markets_from_event(event, family, sid, fetched_epoch, counts):
                if market["id"] in seen_markets:
                    counts["duplicate_market"] += 1
                    continue
                seen_markets.add(market["id"])
                result.append(market)
        if len(events) >= MAX_EVENTS_PER_SERIES:
            if page_index + 1 < POLY_MAX_PAGES_PER_SERIES:
                queue.append((sid, family, offset + MAX_EVENTS_PER_SERIES, page_index + 1))
            else:
                truncated_series.add(sid)
        else:
            complete_series.add(sid)
    truncated_series.update(sid for sid, _, _, _ in queue)
    stats = dict(counts)
    stats.update(
        series_known=len(series), series_compatible=eligible_count,
        series_selected=len(selected), series_not_selected=eligible_count - len(selected),
        series_skipped_unsupported=len(series) - eligible_count,
        series_complete=len(complete_series), series_truncated=len(truncated_series),
        series_failed=len(failed_series), requests=requests,
        request_budget=POLY_MAX_REQUESTS_PER_REFRESH,
        markets=len(result), complete=(not failed_series and not truncated_series
                                      and len(selected) == eligible_count),
    )
    _discovery_stats("poly", stats)
    if failed_series:
        # The monitor's SnapshotRefresher retains its previous complete
        # catalogue, rather than replacing it with a failed partial response.
        raise ValueError("Gamma catalogue fetch incomplete")
    LOG.info("Polymarket: %d mercados binários desportivos elegíveis", len(result))
    return result


def discover_sx():
    all_markets, page_key = [], None
    seen_markets, used_keys = set(), set()
    counts = Counter()
    complete = False
    try:
        for _ in range(MAX_SX_PAGES):
            params = {"type": "52,226", "pageSize": 100, "onlyMainLine": "true"}
            if page_key:
                params["paginationKey"] = page_key
            response = get_json(SX_API + "/markets/active", params)
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("markets"), list):
                raise ValueError("SX markets response invalid")
            batch = data["markets"]
            counts["pages"] += 1
            counts["markets_received"] += len(batch)
            for m in batch:
                if (not isinstance(m, dict) or m.get("status") != "ACTIVE"
                        or m.get("type") not in ALLOWED_SX_TYPES
                        or not m.get("marketHash") or not m.get("teamOneName")
                        or not m.get("teamTwoName")):
                    counts["inactive_or_unsupported"] += 1
                    continue
                if m["marketHash"] in seen_markets:
                    counts["duplicate_market"] += 1
                    continue
                game = unix_time(m.get("gameTime"))
                if not game:
                    counts["missing_game_time"] += 1
                    continue
                seen_markets.add(m["marketHash"])
                all_markets.append({
                    "hash": m["marketHash"], "teams": (m["teamOneName"], m["teamTwoName"]),
                    "outcomes": (m.get("outcomeOneName", ""), m.get("outcomeTwoName", "")),
                    "start": game, "sport": sx_sport_family(m),
                    "type": m["type"], "league": m.get("leagueLabel", ""),
                    "event_id": m.get("sportXeventId"),
                    "sport_id": m.get("sportId"), "league_id": m.get("leagueId"),
                    "live_enabled": m.get("liveEnabled") is True,
                })
            page_key = data.get("nextKey")
            if not page_key:
                complete = True
                break
            if not batch or page_key in used_keys:
                raise ValueError("SX pagination made no progress")
            used_keys.add(page_key)
    except Exception:
        _discovery_stats("sx", {**dict(counts), "complete": False,
                               "fetch_errors": 1, "markets": len(all_markets)})
        raise
    _discovery_stats("sx", {**dict(counts), "complete": complete,
                           "truncated": not complete, "page_limit": MAX_SX_PAGES,
                           "markets": len(all_markets)})
    if not complete:
        LOG.warning("SX DISCOVERY: limite de %d páginas; há nextKey por consultar", MAX_SX_PAGES)
    LOG.info("SX Bet: %d mercados 2-way ativos", len(all_markets))
    return all_markets


def pair_markets(poly, sx):
    global _MATCHED_POLY_SERIES
    result, matched_series = [], set()
    counts = Counter()
    by_sport = {}
    for p in poly:
        by_sport.setdefault(p["sport"], []).append(p)
    for s in sx:
        if s["sport"] in EXCLUDED_SETTLEMENT_SPORTS:
            counts["excluded_settlement_sport"] += 1
            continue
        a, b = s["teams"]
        expected_type = 226 if s["sport"] in {"basketball", "american_football"} else 52
        if s["type"] != expected_type:
            counts["incompatible_sx_period_type"] += 1
            continue
        if not (same_team(s["outcomes"][0], a) and same_team(s["outcomes"][1], b)):
            counts["sx_outcomes_not_participants"] += 1
            continue
        same_sport = by_sport.get(s["sport"], [])
        if not same_sport:
            counts["no_poly_sport"] += 1
            continue
        same_start = [p for p in same_sport if match_time(p["start"], s["start"])]
        if not same_start:
            counts["no_poly_start_match"] += 1
            continue
        named = []
        for p in same_start:
            x, y = p["teams"]
            if ((same_team(a, x) and same_team(b, y)) or
                    (same_team(a, y) and same_team(b, x))):
                named.append(p)
        if not named:
            counts["no_poly_participant_match"] += 1
            continue
        candidates = []
        for p in named:
            poly_index = {}
            for idx, label in enumerate(p["outcomes"]):
                for i, team in enumerate((a, b)):
                    if same_team(label, team):
                        poly_index[i] = idx
            if len(poly_index) != 2 or poly_index[0] == poly_index[1]:
                continue
            candidates.append((p, poly_index))
        if len(candidates) == 1:
            p, mapping = candidates[0]
            counts["matched_sx_market"] += 1
            if p.get("series_id"):
                matched_series.add(str(p["series_id"]))
            for target in (0, 1):
                result.append({
                    "poly": p, "sx": s, "poly_asset": p["assets"][mapping[target]],
                    "poly_label": p["outcomes"][mapping[target]],
                    "sx_target": 1 - target,
                    "sx_label": s["outcomes"][1 - target],
                    "key": f"{p['id']}:{s['hash']}:{target}",
                })
        elif candidates:
            counts["ambiguous_poly_markets"] += 1
        else:
            counts["poly_outcomes_not_participants"] += 1
    _MATCHED_POLY_SERIES = matched_series
    stats = {**dict(counts), "poly_markets": len(poly), "sx_markets": len(sx),
             "directions": len(result), "matched_series": len(matched_series)}
    DISCOVERY_STATS["matching"] = stats
    LOG.info("MATCHING_V15 | %s", json.dumps(stats, sort_keys=True, separators=(",", ":")))
    LOG.info("Matching confirmado por nome, desporto e hora: %d pares", len(result))
    return result


def poly_book(asset):
    data = get_json(POLY_CLOB + "/book", {"token_id": asset}, timeout=BOOK_TIMEOUT_SECONDS)
    asks = []
    for level in data.get("asks", []):
        try:
            price, shares = float(level["price"]), float(level["size"])
            if 0 < price < 1 and shares > 0:
                asks.append((price, shares))
        except (KeyError, ValueError, TypeError):
            pass
    asks.sort()
    return asks, time.monotonic()


def sx_book(market_hash, target):
    """Para apostar num outcome, consumir as ordens do outcome contrário.

    Retorna níveis (preço por unidade de payout, capacidade de payout).
    """
    raw = get_json(SX_API + "/orderbook-v3/snapshot/market", {
        "marketHash": market_hash,
    }, timeout=BOOK_TIMEOUT_SECONDS)
    data = raw["data"]
    other_side = "outcomeTwo" if target == 0 else "outcomeOne"
    levels = []
    for level in data.get(other_side, []) or []:
        try:
            maker = int(level["percentageOdds"])
            size = int(level["size"]) / 1_000_000
            if not (0 < maker < 10**20) or size <= 0:
                continue
            maker_p = maker / 10**20
            taker_p = 1 - maker_p
            # Taker payout capacity = resting stake / maker probability.
            levels.append((taker_p, size / maker_p))
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            continue
    levels.sort()
    return levels, time.monotonic()


def cost_for_payout(levels, payout):
    need, cost = payout, 0.0
    for price, capacity in levels:
        take = min(need, capacity)
        cost += take * price
        need -= take
        if need <= 1e-7:
            return cost
    return None


@dataclass
class Quote:
    payout: float
    poly_usd: float
    sx_usd: float
    fees_usd: float
    profit_usd: float
    roi: float


def price_quote(poly_levels, sx_levels, free_poly, free_sx):
    if not poly_levels or not sx_levels or FX <= 0:
        return None
    poly_capacity = sum(cap for _, cap in poly_levels)
    sx_capacity = sum(cap for _, cap in sx_levels)
    max_payout = min(poly_capacity, sx_capacity, 20000.0)
    max_usd = MAX_TOTAL_EUR / FX
    low, high = 0.0, max_payout

    def simulate(q):
        pc = cost_for_payout(poly_levels, q)
        sc = cost_for_payout(sx_levels, q)
        if pc is None or sc is None:
            return None
        fees = pc * POLY_FEE_BUFFER + sc * SX_FEE_BUFFER
        if pc * (1 + POLY_FEE_BUFFER) > free_poly or sc * (1 + SX_FEE_BUFFER) > free_sx:
            return None
        if pc + sc + fees > max_usd:
            return None
        profit = q - pc - sc - fees
        return Quote(q, pc, sc, fees, profit, profit / (pc + sc + fees))

    best = None
    for _ in range(35):
        mid = (low + high) / 2
        quote = simulate(mid)
        if quote:
            low, best = mid, quote
        else:
            high = mid
    if not best or (best.poly_usd + best.sx_usd) * FX < MIN_TOTAL_EUR:
        return None
    return best if best.roi >= MIN_NET_ROI else None


def initialize_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wallets (
            platform TEXT PRIMARY KEY, free_usd REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT UNIQUE NOT NULL, created_utc TEXT NOT NULL,
            sport TEXT NOT NULL, event TEXT NOT NULL, poly_label TEXT NOT NULL,
            sx_label TEXT NOT NULL, poly_asset TEXT NOT NULL, sx_hash TEXT NOT NULL,
            poly_cost REAL NOT NULL, sx_cost REAL NOT NULL,
            fee_buffer REAL NOT NULL, payout REAL NOT NULL,
            estimated_profit REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
            settled_side TEXT
        );
    """)
    previous_fx = conn.execute("SELECT value FROM meta WHERE key='fx'").fetchone()
    if previous_fx and abs(float(previous_fx[0]) - FX) > 1e-9:
        raise ValueError("EUR_PER_USD mudou; usa um novo PAPER_DB para reiniciar a simulação")
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('fx',?)", (str(FX),))
    for name in ("poly", "sx"):
        conn.execute(
            "INSERT OR IGNORE INTO wallets(platform,free_usd) VALUES (?,?)",
            (name, INITIAL_EUR / FX),
        )
    conn.commit()


def balances(conn):
    return {name: amount for name, amount in conn.execute("SELECT platform,free_usd FROM wallets")}


def book_paper(conn, pair, q):
    # Transação única: nada é debitado se não houver saldo em ambos.
    with conn:
        exists = conn.execute("SELECT 1 FROM trades WHERE match_key=?", (pair["key"],)).fetchone()
        if exists:
            return None
        free = balances(conn)
        poly_charge = q.poly_usd * (1 + POLY_FEE_BUFFER)
        sx_charge = q.sx_usd * (1 + SX_FEE_BUFFER)
        if free["poly"] + 1e-7 < poly_charge or free["sx"] + 1e-7 < sx_charge:
            return None
        conn.execute("UPDATE wallets SET free_usd=free_usd-? WHERE platform='poly'", (poly_charge,))
        conn.execute("UPDATE wallets SET free_usd=free_usd-? WHERE platform='sx'", (sx_charge,))
        cursor = conn.execute("""
            INSERT INTO trades(match_key,created_utc,sport,event,poly_label,sx_label,
                poly_asset,sx_hash,poly_cost,sx_cost,fee_buffer,payout,estimated_profit,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')
        """, (pair["key"], datetime.now(timezone.utc).isoformat(),
              pair["sx"]["sport"], " vs ".join(pair["sx"]["teams"]),
              pair["poly_label"], pair["sx_label"], pair["poly_asset"],
              pair["sx"]["hash"], q.poly_usd, q.sx_usd,
              q.fees_usd, q.payout, q.profit_usd))
    return cursor.lastrowid


def settle(conn, trade_id, winner):
    # Liquidação manual, somente depois de confirmar regras e resultado nas duas fontes.
    row = conn.execute("""SELECT status,poly_cost,sx_cost,fee_buffer,payout
                          FROM trades WHERE id=?""", (trade_id,)).fetchone()
    if not row or row[0] != "OPEN":
        raise ValueError("Operação inexistente ou já liquidada")
    _, poly, sx, fees, payout = row
    with conn:
        if winner == "poly":
            conn.execute("UPDATE wallets SET free_usd=free_usd+? WHERE platform='poly'", (payout,))
        elif winner == "sx":
            conn.execute("UPDATE wallets SET free_usd=free_usd+? WHERE platform='sx'", (payout,))
        elif winner == "void":
            # Estimativa: devolução dos stakes, com buffers de fees não recuperados.
            conn.execute("UPDATE wallets SET free_usd=free_usd+? WHERE platform='poly'", (poly,))
            conn.execute("UPDATE wallets SET free_usd=free_usd+? WHERE platform='sx'", (sx,))
        else:
            raise ValueError("Usa poly, sx ou void")
        conn.execute("UPDATE trades SET status='SETTLED', settled_side=? WHERE id=?", (winner, trade_id))
    LOG.info("Liquidação manual #%s: %s | %s", trade_id, winner, balances(conn))


def notify_trade(pair, q, trade_id):
    s = pair["sx"]
    now = time.time()
    stage = "APÓS INÍCIO*" if s["start"] <= now else "PRÉ-JOGO"
    telegram(
        f"✅ PAPER #{trade_id} · {stage}\n"
        f"{s['sport'].upper()} | {s['teams'][0]} vs {s['teams'][1]}\n"
        f"Poly: {pair['poly_label']} · {q.poly_usd * FX:.2f}€\n"
        f"SX: {pair['sx_label']} · {q.sx_usd * FX:.2f}€\n"
        f"Total: {(q.poly_usd + q.sx_usd + q.fees_usd) * FX:.2f}€\n"
        f"Lucro estimado: +{q.profit_usd * FX:.2f}€ ({q.roi * 100:.2f}%)\n"
        "🧪 Simulado, não liquidado"
    )


def check_pairs(conn, pairs, cursor_offset=0):
    if not pairs:
        return 0
    budget = MAX_PAIRS_PER_SCAN
    checked = 0
    current = time.time()
    for pair in pairs[cursor_offset:] + pairs[:cursor_offset]:
        if checked >= budget:
            break
        s = pair["sx"]
        if not (current - MAX_LIVE_AGE_H*3600 <= s["start"] <= current + MAX_FUTURE_H*3600):
            continue
        if s["start"] < current and not s["live_enabled"]:
            continue
        if conn.execute("SELECT 1 FROM trades WHERE match_key=?", (pair["key"],)).fetchone():
            continue
        checked += 1
        try:
            poly_levels, t1 = poly_book(pair["poly_asset"])
            sx_levels, t2 = sx_book(s["hash"], pair["sx_target"])
            if abs(t1 - t2) > MAX_SNAPSHOT_GAP_S:
                continue
            money = balances(conn)
            q = price_quote(poly_levels, sx_levels, money["poly"], money["sx"])
            if not q:
                continue
            trade_id = book_paper(conn, pair, q)
            if trade_id:
                LOG.info("PAPER #%s | %s | ROI líquido estimado %.2f%%",
                         trade_id, " vs ".join(s["teams"]), 100 * q.roi)
                notify_trade(pair, q, trade_id)
        except Exception as exc:
            LOG.warning("Falha leitura %s: %s", s["hash"][:12], exc)
    return checked


def poly_stream():
    try:
        import websocket
    except ImportError:
        LOG.warning("Instala websocket-client para receber gatilhos Polymarket")
        return
    while True:
        try:
            with watch_lock:
                assets = list(watched_assets)[:500]
            if not assets:
                time.sleep(5)
                continue

            def on_open(ws):
                global poly_ws_client
                with watch_lock:
                    poly_ws_client = ws
                    latest_assets = list(watched_assets)[:500]
                ws.send(json.dumps({"assets_ids": latest_assets, "type": "market"}))
                def ping_loop():
                    while ws.keep_running:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        time.sleep(10)
                threading.Thread(target=ping_loop, daemon=True).start()

            def on_message(_ws, msg):
                if msg == "PONG":
                    return
                try:
                    payload = json.loads(msg)
                    items = payload if isinstance(payload, list) else [payload]
                    if any(isinstance(x, dict) and x.get("event_type") in
                           ("book", "price_change") for x in items):
                        wake.set()
                except (ValueError, TypeError):
                    return

            def on_close(ws, *_):
                global poly_ws_client
                with watch_lock:
                    if poly_ws_client is ws:
                        poly_ws_client = None

            ws = websocket.WebSocketApp(
                POLY_WS, on_open=on_open, on_message=on_message,
                on_error=lambda _w, e: LOG.warning("Poly WS: %s", e),
                on_close=on_close,
            )
            ws.run_forever(ping_interval=0)
        except Exception as exc:
            LOG.warning("Reconexão Poly WS: %s", exc)
        time.sleep(5)


async def sx_stream_async():
    if not SX_KEY:
        return
    try:
        import aiohttp
        from centrifuge import Client, SubscriptionEventHandler
    except ImportError:
        LOG.warning("Instala centrifuge-python aiohttp para SX WS")
        return

    async def token(ctx=None):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                SX_API + "/user/realtime-token-v3/api-key",
                headers={"x-sx-api-key": SX_KEY}, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                return (await resp.json())["token"]

    async def update(ctx):
        if isinstance(ctx.data, dict):
            with watch_lock:
                relevant = ctx.data.get("marketHash") in watched_sx_hashes
            if relevant:
                wake.set()

    while True:
        try:
            client = Client(SX_WS, get_token=token)
            await client.connect()
            handler = SubscriptionEventHandler(on_publication=update)
            sub = client.new_subscription("best_odds_v3:global", handler)
            await sub.subscribe()
            await asyncio.Future()
        except Exception as exc:
            LOG.warning("SX WS a reconectar: %s", exc)
            await asyncio.sleep(7)


def main():
    if FX <= 0:
        raise SystemExit("Configura EUR_PER_USD (EUR por 1 USD) para não fingir paridade EUR/USDC")
    if not SX_KEY:
        raise SystemExit("Configura SX_API_KEY para consultar orderbook V3 da SX Bet")
    conn = sqlite3.connect(DB_FILE)
    initialize_db(conn)
    threading.Thread(target=poly_stream, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(sx_stream_async()), daemon=True).start()
    telegram("🟢 PAPER V11 ligado\nPolymarket + SX Bet | Modo simulação")
    pairs, last_catalog, offset = [], 0, 0
    while True:
        now = time.monotonic()
        if now - last_catalog >= CATALOG_SECONDS:
            try:
                poly, sx = discover_poly(), discover_sx()
                pairs = pair_markets(poly, sx)
                with watch_lock:
                    old = set(watched_assets)
                    new = {p["poly_asset"] for p in pairs}
                    new = set(list(new)[:500])
                    watched_assets.clear()
                    watched_assets.update(new)
                    watched_sx_hashes.clear()
                    watched_sx_hashes.update(p["sx"]["hash"] for p in pairs)
                    ws = poly_ws_client
                if ws is not None:
                    for operation, assets in (("unsubscribe", old - new), ("subscribe", new - old)):
                        if assets:
                            try:
                                ws.send(json.dumps({"assets_ids": list(assets), "operation": operation}))
                            except Exception as exc:
                                LOG.warning("WS Poly atualizar ativos: %s", exc)
                last_catalog = now
                LOG.info("Saldo livre (USDC estimado): %s", balances(conn))
            except Exception as exc:
                LOG.exception("Erro no catálogo: %s", exc)
                last_catalog = now - CATALOG_SECONDS + 30
        try:
            checked = check_pairs(conn, pairs, offset)
            offset = (offset + checked) % len(pairs) if pairs else 0
        except Exception:
            LOG.exception("Falha no scanner")
        wake.wait(timeout=SCANNER_SECONDS)
        wake.clear()
        # Proteção simples contra floods de mensagens da WS.
        time.sleep(2)


def self_test():
    assert "x-sx-api-key" not in HTTP.headers
    assert same_team("Celtics", "Boston Celtics")
    assert not same_team("Boston Celtics", "Los Angeles Lakers")
    assert parse_title("NBA: Boston Celtics vs Los Angeles Lakers") == (
        "Boston Celtics", "Los Angeles Lakers")
    assert is_poly_moneyline({"sportsMarketType": "moneyline"})
    assert is_poly_moneyline({"sportsMarketType": "MONEYLINE"})
    assert not is_poly_moneyline({"sportsMarketType": "spread"})
    assert not is_poly_moneyline({"sportsMarketType": "total"})
    assert not is_poly_moneyline({"sportsMarketType": "half_moneyline"})
    assert not is_poly_moneyline({"outcomes": ["Team A", "Team B"]})
    assert sport_family("NFL") == "american_football"
    assert sx_sport_family({"sportLabel": "Basketball"}) == "basketball"
    assert sx_sport_family({"sportLabel": "Soccer"}) == "soccer"
    assert sx_sport_family({"sportLabel": "Tennis"}) == "tennis"
    assert sx_sport_family({"sportLabel": "Mixed Martial Arts"}) == "combat"
    assert sx_sport_family({"sportLabel": "Football", "sportId": 8}) == "american_football"
    assert sport_family("cricadt10") == "cricket"
    assert sport_family("hbnbi") == "handball"
    assert sx_sport_family({"sportLabel": "Football", "leagueLabel": "NFL"}) == "american_football"
    # Poly 0.45, SX 0.50 -> payout 100, custo 95, buffers ~0.95
    old_fx = globals()["FX"]
    globals()["FX"] = 1.0
    try:
        q = price_quote([(0.45, 200)], [(0.50, 200)], 1000, 1000)
        assert q and 0 < q.profit_usd < 6
        assert (q.poly_usd + q.sx_usd + q.fees_usd) <= 100.0001
        assert cost_for_payout([(0.4, 10), (0.6, 10)], 15) == 7.0
        # Não inventar margem sem profundidade suficiente.
        assert price_quote([(0.6, 10)], [(0.6, 10)], 1000, 1000) is None
        fake_poly = [{"id": "p1", "sport": "tennis", "teams": ("Novak Djokovic", "Carlos Alcaraz"),
                      "title": "Djokovic vs Alcaraz", "start": 1890000000,
                      "outcomes": ["Djokovic", "Alcaraz"], "assets": ["a", "b"]}]
        fake_sx = [{"hash": "sx1", "sport": "tennis",
                    "teams": ("Novak Djokovic", "Carlos Alcaraz"),
                    "outcomes": ("Novak Djokovic", "Carlos Alcaraz"), "start": 1890000000,
                    "type": 52, "league": "ATP", "live_enabled": False}]
        pairs = pair_markets(fake_poly, fake_sx)
        assert len(pairs) == 2 and pairs[0]["sx_target"] == 1
        test_db = sqlite3.connect(":memory:")
        initialize_db(test_db)
        trade_id = book_paper(test_db, pairs[0], q)
        assert trade_id == 1 and balances(test_db)["poly"] < 1000
        assert book_paper(test_db, pairs[0], q) is None
        settle(test_db, trade_id, "poly")
        assert test_db.execute("SELECT status FROM trades WHERE id=1").fetchone()[0] == "SETTLED"
    finally:
        globals()["FX"] = old_fx
    print("✅ Testes locais OK (sem ligação às APIs)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--settle", type=int, help="ID da simulação a liquidar manualmente")
    parser.add_argument("--winner", choices=["poly", "sx", "void"])
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.settle is not None:
        if FX <= 0 or not args.winner:
            parser.error("Liquidação exige EUR_PER_USD e --winner poly|sx|void")
        db = sqlite3.connect(DB_FILE)
        initialize_db(db)
        settle(db, args.settle, args.winner)
    else:
        main()
