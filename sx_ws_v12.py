"""Sinais WebSocket SX Bet V3: aceleram leitura REST, não geram apostas.

Canal orderbook_v3:{marketHash}. Feed opcional: falhas de WS nunca
bloqueiam a recolha REST periódica. Nenhum preço WS é usado para simular fill.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import requests

LOG = logging.getLogger("sx_ws_v12")
SX_WS = "wss://realtime.sx.bet/connection/websocket"
SX_TOKEN_URL = "https://api.sx.bet/user/realtime-token-v3/api-key"
MAX_WATCHED = 12


def make_subscription_handler(base_class, market_hash, on_book):
    """Handler de eventos compatível com centrifuge-python.

    SubscriptionEventHandler é uma classe base com métodos assíncronos,
    não um construtor que aceite callbacks por palavra-chave.
    """
    class BookHandler(base_class):
        async def on_publication(self, ctx):
            on_book(market_hash)

        async def on_subscribed(self, ctx):
            LOG.info("SX WS V12: canal de livro subscrito (%s).",
                     market_hash[:12])

    return BookHandler()


class SXBookHints:
    def __init__(self, key=None):
        self.key = key if key is not None else os.getenv("SX_API_KEY", "")
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.watched = ()
        self.hot = {}  # asset/hash -> primeiro evento monotónico pendente
        self.running = False
        self.last_message_epoch = None
        self.received_events = 0

    def update(self, hashes):
        chosen = tuple(dict.fromkeys(str(h) for h in hashes if h))[:MAX_WATCHED]
        with self.lock:
            self.watched = chosen

    def signal(self, market_hash):
        first_event = False
        with self.lock:
            if market_hash in self.watched:
                self.hot.setdefault(market_hash, time.monotonic())
                self.last_message_epoch = time.time()
                self.received_events += 1
                first_event = self.received_events == 1
                self.wake.set()
        if first_event:
            LOG.info(
                "SX WS V12: primeiro evento de livro recebido "
                "(REST continua a confirmar as odds)."
            )

    def take_events(self):
        """Cache breve: chave -> instante monotónico do 1.º sinal pendente.

        Guarda apenas IDENTIFICADORES, nunca odds/fills executáveis.
        """
        with self.lock:
            result = dict(self.hot)
            self.hot.clear()
            self.wake.clear()
            return result

    def take_hot(self):
        """Compatibilidade com clientes que só pretendem as chaves alteradas."""
        return set(self.take_events())

    async def get_token(self, ctx=None):
        if not self.key:
            raise RuntimeError("SX_API_KEY não configurada")
        def request():
            response = requests.get(
                SX_TOKEN_URL, headers={"x-sx-api-key": self.key}, timeout=12
            )
            response.raise_for_status()
            return response.json()["token"]
        return await asyncio.to_thread(request)

    async def serve(self):
        try:
            from centrifuge import Client, SubscriptionEventHandler
        except ImportError:
            LOG.warning("centrifuge-python ausente; continuar só com REST.")
            return
        while self.running:
            with self.lock:
                selected = self.watched
            if not selected:
                await asyncio.sleep(5)
                continue
            client = None
            phase = "criar cliente"
            try:
                client = Client(SX_WS, get_token=self.get_token)
                phase = "ligar"
                await client.connect()
                subscriptions = []
                phase = "subscrever"
                for market_hash in selected:
                    listener = make_subscription_handler(
                        SubscriptionEventHandler, market_hash, self.signal
                    )
                    sub = client.new_subscription(
                        f"orderbook_v3:{market_hash}", listener
                    )
                    subscriptions.append(sub)
                    await sub.subscribe()
                LOG.info(
                    "SX WS V12: conexão iniciada; %d subscrições solicitadas. "
                    "A aguardar eventos.",
                    len(subscriptions),
                )
                phase = "receber eventos"
                while self.running:
                    await asyncio.sleep(3)
                    with self.lock:
                        if self.watched != selected:
                            break
            except Exception as exc:
                # Não incluir tokens nem cabeçalhos de autenticação nos logs.
                LOG.warning(
                    "SX WS indisponível na fase '%s' (%s); "
                    "REST mantém-se ativo.",
                    phase, type(exc).__name__,
                )
                await asyncio.sleep(10)
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    def start(self):
        if not self.key or self.running:
            return False
        self.running = True
        threading.Thread(
            target=lambda: asyncio.run(self.serve()),
            name="SXBookHints", daemon=True,
        ).start()
        return True

    def stop(self):
        self.running = False
        self.wake.set()


def self_test():
    f = SXBookHints(key="test")
    f.update(["a", "a", "b"])
    assert f.watched == ("a", "b")
    f.signal("not-watched")
    assert not f.wake.is_set()
    f.signal("b")
    assert f.wake.is_set()
    assert f.hot["b"] <= time.monotonic()
    first = f.hot["b"]
    f.signal("b")
    assert f.hot["b"] == first
    assert f.take_hot() == {"b"}
    assert f.received_events == 2
    assert f.last_message_epoch is not None
    assert not f.wake.is_set()
    f.update(["c"])
    assert f.watched == ("c",)

    # A classe oficial de eventos não aceita callbacks no construtor.
    # Simular essa interface sem ligar às APIs.
    class StrictEventHandler:
        def __init__(self):
            pass

    listener = make_subscription_handler(StrictEventHandler, "c", f.signal)
    asyncio.run(listener.on_subscribed(None))
    asyncio.run(listener.on_publication(None))
    assert f.take_hot() == {"c"}
    assert f.received_events == 3

    # Verificar também a classe REAL instalada em requirements.txt.
    from centrifuge import SubscriptionEventHandler
    official = make_subscription_handler(
        SubscriptionEventHandler, "c", f.signal
    )
    asyncio.run(official.on_publication(None))
    assert f.take_hot() == {"c"}
    assert f.received_events == 4
    print("✅ SX WS V12: eventos e handler oficial centrifuge-python OK (offline)")


if __name__ == "__main__":
    self_test()
