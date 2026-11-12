
import os
import json
import time
import threading
import logging
import hashlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# ============================================================
# 1. CONFIGURACOES
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Banca separada por moeda
BANKROLL_EUR = float(os.getenv("BANKROLL_EUR", "100"))
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "100"))

MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "900"))

MIN_POLY_VOLUME = float(os.getenv("MIN_POLY_VOLUME", "20000"))

# Margem estimada para custos do Polymarket.
# Configurar conforme as taxas reais aplicaveis.
POLY_FEE_BUFFER_PCT = float(
    os.getenv("POLY_FEE_BUFFER_PCT", "1.0")
)

# Filtro das casas de apostas.
BOOKMAKER_FILTER = [
    x.strip().lower()
    for x in os.getenv(
        "BOOKMAKER_FILTER", "betano,betclic"
    ).split(",")
    if x.strip()
]

# Opcional: definir competicoes manualmente.
# Exemplo: soccer_epl,tennis_atp,basketball_nba
CUSTOM_SPORT_KEYS = [
    x.strip()
    for x in os.getenv("SPORT_KEYS", "").split(",")
    if x.strip()
]

SPORT_PREFIXES = {
    "football": ["soccer_"],
    "tennis": ["tennis_"],
    "basketball": ["basketball_"],
    "hockey": ["icehockey_"],
    "baseball": ["baseball_"]
}

SPORT_LIMIT_PER_CATEGORY = int(
    os.getenv("SPORT_LIMIT_PER_CATEGORY", "2")
)

MAX_POLY_PAGES = int(os.getenv("MAX_POLY_PAGES", "2"))
MAX_POLY_MARKETS = int(os.getenv("MAX_POLY_MARKETS", "50"))

ALERT_COOLDOWN = 1800

# Apenas scanner. Nunca faz apostas automaticamente.
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

session.headers.update({
    "User-Agent": "ArbitrageScanner/3.0"
})

state_lock = threading.Lock()

scanner_status = {
    "running": True,
    "last_scan": None,
    "last_error": None,
    "events_checked": 0,
    "markets_checked": 0,
    "opportunities": 0,
    "sports": [],
    "mode": "SCAN_ONLY"
}

alert_history = {}


# ============================================================
# 3. HTTP
# ============================================================

def http_get(url, params=None, timeout=20):

    response = session.get(
        url,
        params=params,
        timeout=timeout
    )

    response.raise_for_status()

    return response.json()


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

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "disable_web_page_preview": True
    }

    try:
        response = session.post(
            url,
            json=payload,
            timeout=15
        )

        response.raise_for_status()

        if alert_key:
            alert_history[alert_key] = now

        logger.info("Alerta enviado para Telegram.")
        return True

    except requests.RequestException as e:
        logger.error("Erro Telegram: %s", e)
        return False


def gerar_chave_alerta(texto):

    return hashlib.sha256(
        texto.encode("utf-8")
    ).hexdigest()


# ============================================================
# 5. DESCOBRIR COMPETICOES
# ============================================================

def descobrir_desportos():

    if not ODDS_API_KEY:
        return []

    if CUSTOM_SPORT_KEYS:
        return CUSTOM_SPORT_KEYS

    try:

        sports = http_get(
            f"{ODDS_BASE}/sports",
            params={"apiKey": ODDS_API_KEY}
        )

        encontrados = []

        prioridades = {
            "football": [
                "soccer_uefa_champs_league",
                "soccer_epl",
                "soccer_spain_la_liga"
            ],
            "tennis": [
                "tennis_atp",
                "tennis_wta"
            ],
            "basketball": [
                "basketball_nba",
                "basketball_euroleague"
            ],
            "hockey": [
                "icehockey_nhl"
            ],
            "baseball": [
                "baseball_mlb"
            ]
        }

        for category, prefixes in SPORT_PREFIXES.items():

            matches = [
                sport["key"]
                for sport in sports
                if sport.get("active")
                and any(
                    sport["key"].startswith(prefix)
                    for prefix in prefixes
                )
            ]

            ordered = []

            for preferred in prioridades[category]:
                if preferred in matches:
                    ordered.append(preferred)

            for item in matches:
                if item not in ordered:
                    ordered.append(item)

            encontrados.extend(
                ordered[:SPORT_LIMIT_PER_CATEGORY]
            )

        logger.info("Competicoes: %s", encontrados)

        return encontrados

    except requests.RequestException as e:
        logger.error("Erro ao descobrir competicoes: %s", e)
        return []


# ============================================================
# 6. OBTER ODDS
# ============================================================

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

        logger.info(
            "%s: %s eventos",
            sport_key,
            len(dados)
        )

        return dados

    except requests.RequestException as e:
        logger.error("Erro em %s: %s", sport_key, e)
        return []


# ============================================================
# 7. VALIDACAO DOS RESULTADOS
# ============================================================

def normalizar_nome(nome):

    return " ".join(
        str(nome).lower().strip().split()
    )


def obter_resultados_esperados(evento):

    sport = evento.get("sport_key", "")

    home = normalizar_nome(
        evento.get("home_team", "")
    )

    away = normalizar_nome(
        evento.get("away_team", "")
    )

    if not home or not away:
        return []

    # Futebol: obrigatoriamente 1X2.
    if sport.startswith("soccer_"):

        return {
            home,
            away,
            "draw"
        }

    # Outros desportos: vencedor entre dois participantes.
    return {
        home,
        away
    }


# ============================================================
# 8. MOTOR DE ARBITRAGEM TRADICIONAL
# ============================================================

def calcular_arbitragem_casas(evento):

    esperados = obter_resultados_esperados(evento)

    if not esperados:
        return None

    melhores_odds = {}

    for bookmaker in evento.get("bookmakers", []):

        bookmaker_key = bookmaker.get(
            "key", ""
        ).lower()

        bookmaker_name = bookmaker.get(
            "title", bookmaker_key
        )

        if (
            BOOKMAKER_FILTER
            and bookmaker_key not in BOOKMAKER_FILTER
        ):
            continue

        for market in bookmaker.get("markets", []):

            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []):

                nome = normalizar_nome(
                    outcome.get("name", "")
                )

                # Ignorar resultados que nao pertencem
                # ao conjunto esperado.
                if nome not in esperados:
                    continue

                try:
                    odd = float(
                        outcome.get("price", 0)
                    )
                except (ValueError, TypeError):
                    continue

                if odd <= 1:
                    continue

                if (
                    nome not in melhores_odds
                    or odd > melhores_odds[nome]["odd"]
                ):

                    melhores_odds[nome] = {
                        "odd": odd,
                        "bookmaker": bookmaker_name
                    }

    # CORRECAO 2:
    # Todos os resultados precisam de estar cobertos.
    if set(melhores_odds.keys()) != esperados:
        return None

    # Exigir pelo menos duas casas diferentes.
    casas = {
        info["bookmaker"]
        for info in melhores_odds.values()
    }

    if len(casas) < 2:
        return None

    soma_probabilidades = sum(
        1 / melhores_odds[resultado]["odd"]
        for resultado in esperados
    )

    if soma_probabilidades >= 1:
        return None

    lucro_pct = (
        (1 / soma_probabilidades) - 1
    ) * 100

    if lucro_pct < MIN_PROFIT_PCT:
        return None

    stakes = {}

    for resultado in esperados:

        odd = melhores_odds[resultado]["odd"]

        stake = (
            BANKROLL_EUR
            * (1 / odd)
            / soma_probabilidades
        )

        stakes[resultado] = {
            "odd": odd,
            "stake": stake,
            "bookmaker": melhores_odds[
                resultado
            ]["bookmaker"]
        }

    lucro_eur = BANKROLL_EUR * (
        (1 / soma_probabilidades) - 1
    )

    return {
        "results": stakes,
        "profit_pct": lucro_pct,
        "profit_eur": lucro_eur,
        "probability_sum": soma_probabilidades
    }


# ============================================================
# 9. ALERTA TRADICIONAL
# ============================================================

def analisar_evento(evento):

    arbitragem = calcular_arbitragem_casas(evento)

    if not arbitragem:
        return False

    home = evento.get("home_team", "?")
    away = evento.get("away_team", "?")
    sport = evento.get("sport_key", "Desporto")

    linhas = [
        "ARBITRAGEM DETETADA",
        "",
        f"Desporto: {sport}",
        f"Evento: {home} vs {away}",
        "",
        "ODDS E STAKES:"
    ]

    for resultado, info in arbitragem["results"].items():

        linhas.append(
            f"{resultado}: Odd {info['odd']:.2f} | "
            f"{info['stake']:.2f} EUR | "
            f"{info['bookmaker']}"
        )

    linhas.extend([
        "",
        f"Banca: {BANKROLL_EUR:.2f} EUR",
        f"Lucro teorico: {arbitragem['profit_eur']:.2f} EUR",
        f"Margem: {arbitragem['profit_pct']:.2f}%",
        "",
        "Confirmar odds, limites e regras de liquidacao.",
        "Scanner informativo - sem apostas automaticas."
    ])

    mensagem = "\n".join(linhas)

    key = gerar_chave_alerta(
        str(evento.get("id", "")) + mensagem
    )

    return enviar_alerta_telegram(mensagem, key)


# ============================================================
# 10. POLYMARKET - CONVERSAO DE LISTAS
# ============================================================

def converter_lista(valor):

    if isinstance(valor, str):

        try:
            return json.loads(valor)
        except json.JSONDecodeError:
            return []

    if isinstance(valor, list):
        return valor

    return []


# ============================================================
# 11. POLYMARKET - MERCADOS DESPORTIVOS
# ============================================================

SPORT_KEYWORDS = [
    "football", "soccer", "tennis",
    "basketball", "nba", "wnba",
    "nhl", "hockey", "baseball",
    "mlb", "atp", "wta",
    "premier league", "champions league",
    "world cup", "euroleague",
    " vs ", " vs.", "match winner"
]


def e_mercado_desportivo(market):

    texto = (
        str(market.get("question", "")) + " "
        + str(market.get("description", ""))
    ).lower()

    return any(
        keyword in texto
        for keyword in SPORT_KEYWORDS
    )


def obter_mercados_polymarket():

    mercados = []

    for page in range(MAX_POLY_PAGES):

        try:

            dados = http_get(
                f"{POLY_GAMMA}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": page * 100
                }
            )

            if not dados:
                break

            for market in dados:

                if not e_mercado_desportivo(market):
                    continue

                try:
                    volume = float(
                        market.get("volume", 0) or 0
                    )
                except (ValueError, TypeError):
                    volume = 0

                if volume < MIN_POLY_VOLUME:
                    continue

                mercados.append(market)

                if len(mercados) >= MAX_POLY_MARKETS:
                    return mercados

        except requests.RequestException as e:

            logger.error(
                "Erro Polymarket: %s",
                e
            )

            break

    return mercados


# ============================================================
# 12. POLYMARKET - MELHOR PRECO DE COMPRA
# ============================================================

def obter_melhor_ask(token_id):

    try:

        book = http_get(
            f"{POLY_CLOB}/book",
            params={"token_id": token_id},
            timeout=15
        )

        asks = book.get("asks", [])

        validas = []

        for ask in asks:

            try:
                price = float(ask["price"])
                size = float(ask["size"])
            except (ValueError, TypeError, KeyError):
                continue

            if 0 < price < 1 and size > 0:

                validas.append({
                    "price": price,
                    "size": size
                })

        if not validas:
            return None

        return min(
            validas,
            key=lambda x: x["price"]
        )

    except requests.RequestException as e:

        logger.warning(
            "Erro no livro CLOB: %s",
            e
        )

        return None


# ============================================================
# 13. POLYMARKET - ARBITRAGEM INTERNA
# ============================================================

def analisar_mercado_polymarket(market):

    outcomes = converter_lista(
        market.get("outcomes")
    )

    token_ids = converter_lista(
        market.get("clobTokenIds")
    )

    if len(outcomes) != 2 or len(token_ids) != 2:
        return False

    if not all(token_ids):
        return False

    ask_1 = obter_melhor_ask(token_ids[0])
    ask_2 = obter_melhor_ask(token_ids[1])

    if not ask_1 or not ask_2:
        return False

    price_1 = ask_1["price"]
    price_2 = ask_2["price"]

    total_price = price_1 + price_2

    if total_price >= 1:
        return False

    # CORRECAO 1:
    # Todo o calculo Polymarket e feito em USD.
    # Nunca misturar BANKROLL_EUR com BANKROLL_USD.

    max_shares = min(
        ask_1["size"],
        ask_2["size"]
    )

    shares = min(
        BANKROLL_USD / total_price,
        max_shares
    )

    if shares <= 0:
        return False

    custo_1 = shares * price_1
    custo_2 = shares * price_2

    custo_total_usd = custo_1 + custo_2

    lucro_bruto_usd = shares - custo_total_usd

    # Reserva estimada para taxas.
    taxa_estimada_usd = (
        custo_total_usd
        * POLY_FEE_BUFFER_PCT / 100
    )

    lucro_liquido_estimado_usd = (
        lucro_bruto_usd - taxa_estimada_usd
    )

    lucro_pct = (
        lucro_liquido_estimado_usd
        / custo_total_usd
    ) * 100

    if lucro_liquido_estimado_usd <= 0:
        return False

    if lucro_pct < MIN_PROFIT_PCT:
        return False

    volume = float(
        market.get("volume", 0) or 0
    )

    question = market.get(
        "question", "Mercado desportivo"
    )

    mensagem = (
        "ARBITRAGEM INTERNA POLYMARKET\n\n"
        f"Evento: {question}\n"
        f"Volume: {volume:,.2f} USD\n\n"
        f"Comprar {outcomes[0]}:\n"
        f"Preco: {price_1:.4f} USD\n"
        f"Custo: {custo_1:.2f} USD\n\n"
        f"Comprar {outcomes[1]}:\n"
        f"Preco: {price_2:.4f} USD\n"
        f"Custo: {custo_2:.2f} USD\n\n"
        f"Shares: {shares:.2f}\n"
        f"Custo total: {custo_total_usd:.2f} USD\n"
        f"Taxas estimadas: {taxa_estimada_usd:.2f} USD\n"
        f"Lucro estimado: "
        f"{lucro_liquido_estimado_usd:.2f} USD\n"
        f"Margem estimada: {lucro_pct:.2f}%\n\n"
        "Confirmar liquidez, taxas e execucao."
    )

    key = gerar_chave_alerta(
        str(market.get("id", "")) + mensagem
    )

    return enviar_alerta_telegram(
        mensagem,
        key
    )


# ============================================================
# 14. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():

    logger.info("A iniciar scanner V3.")

    total_eventos = 0
    total_mercados = 0
    total_alertas = 0

    # CASAS TRADICIONAIS

    sports = descobrir_desportos()

    with state_lock:
        scanner_status["sports"] = sports

    for sport in sports:

        eventos = obter_odds_desporto(sport)

        total_eventos += len(eventos)

        for evento in eventos:

            evento["sport_key"] = sport

            try:

                if analisar_evento(evento):
                    total_alertas += 1

            except Exception:

                logger.exception(
                    "Erro ao analisar evento."
                )

    # POLYMARKET

    mercados = obter_mercados_polymarket()

    total_mercados = len(mercados)

    for market in mercados:

        try:

            if analisar_mercado_polymarket(market):
                total_alertas += 1

        except Exception:

            logger.exception(
                "Erro ao analisar mercado Polymarket."
            )

    with state_lock:

        scanner_status["last_scan"] = (
            datetime.now(timezone.utc).isoformat()
        )

        scanner_status["events_checked"] = total_eventos

        scanner_status["markets_checked"] = total_mercados

        scanner_status["opportunities"] += total_alertas

        scanner_status["last_error"] = None

    logger.info(
        "Ciclo terminado | Eventos: %s | Mercados: %s | Alertas: %s",
        total_eventos,
        total_mercados,
        total_alertas
    )


# ============================================================
# 15. SCANNER CONTINUO
# ============================================================

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
# 16. SERVIDOR RENDER
# ============================================================

class SimpleHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/health":

            with state_lock:
                data = dict(scanner_status)

            data["auto_bet"] = AUTO_BET

            body = json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()
            self.wfile.write(body)

        else:

            body = (
                "ROBO HIBRIDO V3 ONLINE\n"
                "Polymarket + The Odds API\n"
                "Mode: SCAN ONLY\n"
                "Health: /health\n"
            ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()
            self.wfile.write(body)

    def do_HEAD(self):

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_web():

    port = int(
        os.environ.get("PORT", 10000)
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        SimpleHandler
    )

    logger.info(
        "Servidor ativo na porta %s",
        port
    )

    server.serve_forever()


# ============================================================
# 17. ARRANQUE
# ============================================================

print("===== TESTE DE CONFIGURACAO =====")
print("TELEGRAM_TOKEN:", bool(os.getenv("TELEGRAM_TOKEN")))
print("CHAT_ID:", bool(os.getenv("CHAT_ID")))
print("ODDS_API_KEY:", bool(os.getenv("ODDS_API_KEY")))
print("=================================")


if __name__ == "__main__":

    if not ODDS_API_KEY:
        logger.warning("ODDS_API_KEY nao configurada.")
if __name__ == "__main__":

    if not ODDS_API_KEY:
        logger.warning("ODDS_API_KEY nao configurada.")

    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram nao configurado.")

    scanner_thread = threading.Thread(
        target=correr_robo_continuo,
        daemon=True
    )

    scanner_thread.start()

    iniciar_servidor_web()
