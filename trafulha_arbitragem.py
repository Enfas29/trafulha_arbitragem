import requests

def testar_telegram():
    token = "9070256990:AAFt6hXRC8i-vH0Y-F5pKYv7-tyrWDxQVs0"
    chat_id = "5670307735"
    mensagem = "🚨 **Teste do Robô Trafulha:** Canal do Telegram ligado com sucesso! Pronto para caçar arbitragens."
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": mensagem,
        "parse_mode": "Markdown"
    }
    
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("Mensagem de teste enviada com sucesso para o Telegram!")
    else:
        print(f"Erro ao enviar: {response.text}")

testar_telegram()
