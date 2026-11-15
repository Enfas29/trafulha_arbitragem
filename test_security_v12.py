"""Teste de isolamento de credenciais. Não acede a serviços externos."""
import trafulha_arbitragem as bot


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return []


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


def main():
    original_http, original_key = bot.HTTP, bot.SX_KEY
    fake = Session()
    try:
        bot.HTTP, bot.SX_KEY = fake, "DO_NOT_LEAK"
        bot.get_json("https://gamma-api.polymarket.com/events")
        bot.get_json("https://clob.polymarket.com/book")
        bot.get_json("https://api.sx.bet/markets/active")
        assert "x-sx-api-key" not in (fake.calls[0][1]["headers"] or {})
        assert "x-sx-api-key" not in (fake.calls[1][1]["headers"] or {})
        assert fake.calls[2][1]["headers"]["x-sx-api-key"] == "DO_NOT_LEAK"
    finally:
        bot.HTTP, bot.SX_KEY = original_http, original_key
    print("✅ Segurança V12: chave SX limitada exclusivamente a api.sx.bet")


if __name__ == "__main__":
    main()
