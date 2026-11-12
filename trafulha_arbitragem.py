import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

# --- CONFIGURAÇÕES ---
TELEGRAM_TOKEN = "9070256990:AAft6hXRC8i-vH0Y-F5pKYv7-tyrWDxQVs0"
CHAT_ID = "5670307735"

# Insere aqui a tua chave gratuita obtida em the-odds-api.com
ODDS_API_KEY = "7c5a2bb44a562ce579413131318b1534"


def enviar_alerta_telegram(mensagem):
  """Envia uma mensagem formatada para o teu chat do Telegram."""
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": mensagem, "parse_mode": "Markdown"}
  try:
    response = requests.post(url, json=payload)
    if response.status_code != 200:
      print(f"[Telegram] Erro ao enviar: {response.text}")
  except Exception as e:
    print(f"[Telegram] Erro de ligação: {e}")


def obter_mercados_polymarket():
  """Busca mercados ativos no Polymarket."""
  url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50"
  try:
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
      return response.json()
  except Exception as e:
    print(f"[Polymarket API] Erro: {e}")
  return []


def obter_odds_casas_tradicionais():
  """Busca odds de futebol em casas europeias através da The Odds API."""
  if ODDS_API_KEY == "A_TUA_CHAVE_DA_ODDS_API_AQUI":
    return []  # Retorna vazio se ainda não tiveres metido a chave

  url = (
      "https://api.the-odds-api.com/v4/sports/soccer_uefa_champs_league/odds/"
      f"?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal"
  )
  try:
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
      return response.json()
  except Exception as e:
    print(f"[Odds API] Erro: {e}")
  return []


def ciclo_scanner():
  print("\n[Scanner Híbrido] A analisar Polymarket e Casas Tradicionais...")
  investimento_total = 100.0

  # 1. Análise Interna Polymarket
  mercados_poly = obter_mercados_polymarket()
  for m in mercados_poly:
    try:
      volume = float(m.get("volume", 0) or 0)
      if volume <= 20000:
        continue

      question = m.get("question", "Mercado Polymarket")
      outcome_prices = m.get("outcomePrices")
      if not outcome_prices:
        continue

      prices = (
          json.loads(outcome_prices)
          if isinstance(outcome_prices, str)
          else outcome_prices
      )
      if len(prices) < 2:
        continue

      p_sim = float(prices[0])
      p_nao = float(prices[1])
      if p_sim <= 0 or p_nao <= 0:
        continue

      odd_sim = 1 / p_sim
      odd_nao = 1 / p_nao
      soma_prob = (1 / odd_sim) + (1 / odd_nao)

      if soma_prob < 0.98:  # Oportunidade interna detetada
        stake_1 = investimento_total * ((1 / odd_sim) / soma_prob)
        stake_2 = investimento_total * ((1 / odd_nao) / soma_prob)
        lucro_euros = (stake_1 * odd_sim) - investimento_total
        lucro_pct = (1 - soma_prob) * 100

        msg = (
            f"🚨 *Arbitragem Interna (Polymarket)* 🚨\n\n"
            f"⚽ *Evento:* {question}\n"
            f"📊 *Volume:* ${volume:,.2f}\n\n"
            f"👉 *Comprar SIM* (Odd {odd_sim:.2f}) -> **{stake_1:.2f}€**\n"
            f"👉 *Comprar NÃO* (Odd {odd_nao:.2f}) -> **{stake_2:.2f}€**\n\n"
            f"💰 *Lucro Líquido:* **+{lucro_euros:.2f}€** (~{lucro_pct:.2f}%)\n"
        )
        enviar_alerta_telegram(msg)
    except Exception as e:
      print(f"Erro no loop Polymarket: {e}")

  # 2. Análise Cruzada (Polymarket vs Casas Tradicionais tipo Betano/Betclic)
  dados_odds = obter_odds_casas_tradicionais()
  for evento in dados_odds:
    home_team = evento.get("home_team")
    away_team = evento.get("away_team")
    bookmakers = evento.get("bookmakers", [])

    for bookmaker in bookmakers:
      bookmaker_name = bookmaker.get("title")  # Ex: Betclic, Betano, etc.
      markets = bookmaker.get("markets", [])
      for market in markets:
        if market.get("key") == "h2h":
          outcomes = market.get("outcomes", [])
          for outcome in outcomes:
            # Aqui cruzamos a melhor odd da casa tradicional com o Polymarket
            pass  # (Lógica expansível de cruzamento de odds)


def correr_robo_continuo():
  while True:
    ciclo_scanner()
    time.sleep(300)  # De 5 em 5 minutos


# --- SERVIDOR WEB PARA O RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"Robo Hibrido a funcionar 24/7!")

  def do_HEAD(self):
    self.send_response(200)
    self.end_headers()


def iniciar_servidor_web():
  port = int(os.environ.get("PORT", 10000))
  server = HTTPServer(("0.0.0.0", port), SimpleHandler)
  server.serve_forever()


if __name__ == "__main__":
  t = threading.Thread(target=correr_robo_continuo)
  t.daemon = True
  t.start()
  iniciar_servidor_web()
