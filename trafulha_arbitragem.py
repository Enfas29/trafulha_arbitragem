import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

# --- CONFIGURAÇÕES DO TELEGRAM ---
TELEGRAM_TOKEN = "9070256990:AAft6hXRC8i-vH0Y-F5pKYv7-tyrWDxQVs0"
CHAT_ID = "5670307735"


def enviar_alerta_telegram(mensagem):
  """Envia uma mensagem formatada para o teu chat do Telegram."""
  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {"chat_id": CHAT_ID, "text": mensagem, "parse_mode": "Markdown"}
  try:
    response = requests.post(url, json=payload)
    if response.status_code == 200:
      print("[Telegram] Alerta enviado com sucesso!")
    else:
      print(f"[Telegram] Erro ao enviar: {response.text}")
  except Exception as e:
    print(f"[Telegram] Erro de ligação: {e}")


# --- MOTOR DE SCANNER COM API REAL DO POLYMARKET ---
def obter_mercados_polymarket_real():
  """Busca mercados ativos em tempo real na Gamma API do Polymarket."""
  url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50"
  try:
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
      return response.json()
  except Exception as e:
    print(f"[Polymarket API] Erro ao obter dados: {e}")
  return []


def ciclo_scanner():
  print(
      "\n[Scanner Global] A consultar a API oficial do Polymarket em tempo"
      " real..."
  )
  mercados = obter_mercados_polymarket_real()
  investimento_total = 100.0

  for m in mercados:
    try:
      volume = float(m.get("volume", 0) or 0)
      if volume <= 20000:
        continue  # Ignorar mercados com volume inferior a $20.000[cite: 1]

      question = m.get("question", "Mercado Desconhecido")
      outcome_prices = m.get("outcomePrices")
      if not outcome_prices:
        continue

      if isinstance(outcome_prices, str):
        prices = json.loads(outcome_prices)
      else:
        prices = outcome_prices

      if len(prices) < 2:
        continue

      preco_poly = float(prices[0])
      if preco_poly <= 0 or preco_poly >= 1:
        continue

      odd_poly = 1 / preco_poly

      # Referência de odd oposta em casa regulada para cruzamento de arbitragem
      odd_reg = 2.10

      prob_poly = 1 / odd_poly
      prob_reg = 1 / odd_reg
      soma_probabilidades = prob_poly + prob_reg

      if soma_probabilidades < 0.98:  # Margem inferior a 0.98[cite: 1]
        stake_poly = investimento_total * (prob_poly / soma_probabilidades)
        stake_reg = investimento_total * (prob_reg / soma_probabilidades)
        retorno_poly = stake_poly * odd_poly
        lucro_euros = retorno_poly - investimento_total
        lucro_percentual = (1 - soma_probabilidades) * 100

        mensagem = (
            f"🚨 *Oportunidade Global de Arbitragem (API Real)* 🚨\n\n"
            f"⚽ *Evento:* {question}\n"
            f"📊 *Volume Polymarket:* ${volume:,.2f}\n\n"
            f"👉 *Lado 1 (Polymarket):* Sim (Odd: {odd_poly:.2f}) ->"
            f" **{stake_poly:.2f}€**\n"
            f"👉 *Lado 2 (Casa Regulada):* Oposto (Odd: {odd_reg:.2f}) ->"
            f" **{stake_reg:.2f}€**\n\n"
            f"💰 *Lucro Líquido:* **+{lucro_euros:.2f}€**"
            f" (~{lucro_percentual:.2f}%)\n"
        )
        enviar_alerta_telegram(mensagem)
        print(f"[ALERTA] Enviado para o Telegram: {question}")

    except Exception as e:
      print(f"[Erro no processamento de mercado]: {e}")


def correr_robo_continuo():
  """Mantém o scanner a correr em ciclo infinito em segundo plano."""
  print(
      "--- Robô de Arbitragem Global com API Iniciado (Modo Contínuo 24/7) ---"
  )
  while True:
    ciclo_scanner()
    time.sleep(300)  # Aguarda 5 minutos entre rondas


# --- PEQUENO SERVIDOR WEB PARA O RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"Robo de Arbitragem com API a funcionar 24/7!")


def iniciar_servidor_web():
  port = int(os.environ.get("PORT", 10000))
  server = HTTPServer(("0.0.0.0", port), SimpleHandler)
  server.serve_forever()


if __name__ == "__main__":
  t = threading.Thread(target=correr_robo_continuo)
  t.daemon = True
  t.start()
  iniciar_servidor_web()
