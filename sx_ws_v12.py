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


class SXBookHints:
    def __init__(self, key=None):
        self.key = key if key is not None else os.getenv("SX_API_KEY", "")
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.watched = ()
        self.hot = set()
        self.running = False
        self.last_message_epoch = None

    def update(self, hashes):
        chosen = tuple(dict.fromkeys(str(h) for h in hashes if h))[:MAX_WATCHED]
        with self.lock:
            self.watched = chosen

    def signal(self, market_hash):
        with self.lock:
            if market_hash in self.watched:
                self.hot.add(market_hash)
                self.last_message_epoch = time.time()
                self.wake.set()

    def take_hot(self):
        with self.lock:
            result = set(self.hot)
            self.hot.clear()
            self.wake.clear()
            return result

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
            try:
                client = Client(SX_WS, get_token=self.get_token)
                await client.connect()
                for market_hash in selected:
                    async def on_update(ctx, market_hash=market_hash):
                        self.signal(market_hash)
                    listener = SubscriptionEventHandler(on_publication=on_update)
                    sub = client.new_subscription(
                        f"orderbook_v3:{market_hash}", listener
                    )
                    await sub.subscribe()
                LOG.info("SX WS V12: %d canais de livros subscritos.", len(selected))
                while self.running:
                    await asyncio.sleep(3)
                    with self.lock:
                        if self.watched != selected:
                            break
            except Exception as exc:
                LOG.warning("SX WS indisponível (%s), REST mantém-se ativo.",
                            type(exc).__name__)
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
    assert f.take_hot() == {"b"}
    assert not f.wake.is_set()
    f.update(["c"])
    assert f.watched == ("c",)
    print("✅ SX WS V12: testes locais do mecanismo de sinais OK")


if __name__ == "__main__":
    self_test()
