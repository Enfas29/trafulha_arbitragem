import time
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
os_import = __import__('os')

# --- CONFIGURAÇÕES DO TELEGRAM ---
TELEGRAM_TOKEN = "9070256990:AAft6hXRC8i-vH0Y-F5pKYv7-tyrWDxQVs0"
CHAT_ID = "5670307735"

def enviar_alerta_telegram(mensagem):
    """Envia uma mensagem formatada para o teu chat do Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print("[Telegram] Alerta enviado com sucesso!")
        else:
            print(f"[Telegram] Erro ao enviar: {response.text}")
    except Exception as e:
        print(f"[Telegram] Erro de ligação: {e}")

# --- MOTOR DE SCANNER GLOBAL ---
def obter_dados_mercados_globais():
    mercados = [
        {
            "evento": "Real Madrid vs AC Milan (Champions League)",
            "data_hora": "25/11/2026 às 21:00",
            "volume_polymarket": 150000.0,
            "plataforma_poly": "Polymarket",
            "escolha_poly": "Real Madrid Vence",
            "odd_poly": 1.80,
            "casa_regulada": "Betano",
            "escolha_reg": "Milan ou Empate",
            "odd_reg": 2.30
        },
        {
            "evento": "Arsenal vs Chelsea (Premier League)",
            "data_hora": "26/11/2026 às 17:30",
            "volume_polymarket": 95000.0,
            "plataforma_poly": "Polymarket",
            "escolha_poly": "Arsenal Vence",
            "odd_poly": 1.90,
            "casa_regulada": "Betclic",
            "escolha_reg": "Chelsea ou Empate",
            "odd_reg": 2.15
        }
    ]
    return mercados

def ciclo_scanner():
    print("\n[Scanner Global] A procurar oportunidades em mercados internacionais...")
    mercados = obter_dados_mercados_globais()
    investimento_total = 100.0
    
    for m in mercados:
        volume = m["volume_polymarket"]
        odd_poly = m["odd_poly"]
        odd_reg = m["odd_reg"]
        
        prob_poly = 1 / odd_poly
        prob_reg = 1 / odd_reg
        
        if volume > 20000:
            soma_probabilidades = prob_poly + prob_reg
            if soma_probabilidades < 0.98:
                stake_poly = investimento_total * (prob_poly / soma_probabilidades)
                stake_reg = investimento_total * (prob_reg / soma_probabilidades)
                retorno_poly = stake_poly * odd_poly
                lucro_euros = retorno_poly - investimento_total
                lucro_percentual = (1 - soma_probabilidades) * 100
                
                mensagem = (
                    f"🚨 *Oportunidade Global de Arbitragem!* 🚨\n\n"
                    f"⚽ *Jogo:* {m['evento']}\n"
                    f"📅 *Data e Hora:* {m['data_hora']}\n"
                    f"📊 *Volume Polymarket:* ${volume:,.2f}\n\n"
                    f"👉 *Lado 1 ({m['plataforma_poly']}):* {m['escolha_poly']} (Odd: {odd_poly:.2f}) -> **{stake_poly:.2f}€**\n"
                    f"👉 *Lado 2 ({m['casa_regulada']}):* {m['escolha_reg']} (Odd: {odd_reg:.2f}) -> **{stake_reg:.2f}€**\n\n"
                    f"💰 *Lucro Líquido:* **+{lucro_euros:.2f}€** (~{lucro_percentual:.2f}%)\n"
                )
                enviar_alerta_telegram(mensagem)
                print(f"[ALERTA] Enviado para o Telegram: {m['evento']}")

def correr_robo_continuo():
    """Mantém o scanner a correr em ciclo infinito em segundo plano."""
    print("--- Robô de Arbitragem Global Iniciado (Modo Contínuo 24/7) ---")
    while True:
        ciclo_scanner()
        time.sleep(600)  # Aguarda 10 minutos entre rondas

# --- PEQUENO SERVIDOR WEB PARA O RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Robo de Arbitragem a funcionar 24/7!")

def iniciar_servidor_web():
    port = int(os_import.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

if __name__ == "__main__":
    # Arranca o robô de arbitragem numa "thread" paralela
    t = threading.Thread(target=correr_robo_continuo)
    t.daemon = True
    t.start()
    
    # Arranca o servidor web na thread principal para manter a porta aberta no Render
    iniciar_servidor_web()
