import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_BOT_TOKEN = "SEU_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "SEU_TELEGRAM_CHAT_ID"

def enviar_alerta_telegram(mensagem):
    if TELEGRAM_BOT_TOKEN == "SEU_TELEGRAM_BOT_TOKEN":
        logging.info(f"\n[SIMULAÇÃO TELEGRAM]:\n{mensagem}")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Erro ao enviar para o Telegram: {e}")

def obter_mercados_polymarket():
    url = "https://clob.polymarket.com/markets"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            mercados_validos = []
            for mercado in data.get("data", []):
                volume = float(mercado.get("volume", 0))
                if volume >= 20000:
                    mercados_validos.append({
                        "titulo": mercado.get("question"),
                        "preco_sim": float(mercado.get("best_bid", 0.5)),
                        "volume": volume
                    })
            return mercados_validos
    except Exception as e:
        logging.error(f"Erro na API do Polymarket: {e}")
    return []

def obter_odds_casas_portuguesas():
    """
    [Ponto de Integração]
    Recolhe as odds da Betclic e da Betano para os mesmos jogos.
    """
    return [
        {
            "jogo": "Benfica vs Porto",
            "mercado": "Vitória Benfica",
            "odds": {
                "Betclic": 2.05,
                "Betano": 2.15  # Betano com odd superior
            },
            "preco_poly_oposto": 0.48
        }
    ]

def executar_varrimento_arbitragem():
    logging.info("A analisar Polymarket vs [Betclic + Betano]...")
    
    # Valida ligação à API do Polymarket (filtro > 20.000$)
    _ = obter_mercados_polymarket()
    eventos = obter_odds_casas_portuguesas()
    
    for evento in eventos:
        # Escolhe automaticamente a casa portuguesa com a odd mais alta
        melhor_casa = max(evento["odds"], key=evento["odds"].get)
        melhor_odd = evento["odds"][melhor_casa]
        
        prob_casa = 1 / melhor_odd
        preco_poly = evento["preco_poly_oposto"]
        
        soma_probabilidades = prob_casa + preco_poly
        
        # Gatilho: Soma inferior a 0.98 (margem > 2%)
        if soma_probabilidades < 0.98:
            lucro_estimado = (1 - soma_probabilidades) * 100
            
            # Cálculo automático para um orçamento base de 100€ por operação
            orcamento = 100.0
            aposta_casa = (orcamento * prob_casa) / soma_probabilidades
            aposta_poly_eur = orcamento - aposta_casa
            
            alerta = (
                f"🚨 **ARBITRAGEM DETETADA (MULTI-CASA)** 🚨\n\n"
                f"⚽ **Jogo:** {evento['jogo']} ({evento['mercado']})\n"
                f"🏆 **Melhor Casa:** {melhor_casa} @ **{melhor_odd}**\n"
                f"🟣 **Polymarket:** Preço oposto @ **{preco_poly}$** (Vol > 20k$)\n\n"
                f"📊 **Soma Probabilidades:** {soma_probabilidades*100:.2f}%\n"
                f"💰 **Lucro Limpo Estimado:** +{lucro_estimado:.2f}%\n\n"
                f"🧮 **Distribuição sugerida (Base 100€):**\n"
                f"• Meter **{aposta_casa:.2f}€** na {melhor_casa}\n"
                f"• Meter **{aposta_poly_eur:.2f}€** (em USDC) no Polymarket"
            )
            enviar_alerta_telegram(alerta)

if __name__ == "__main__":
    while True:
        executar_varrimento_arbitragem()
        time.sleep(300)
