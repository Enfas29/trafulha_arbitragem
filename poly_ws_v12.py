"""Sinais Polymarket CLOB WebSocket; preços finais sempre confirmados por REST.

Adaptação opcional, sem qualquer execução de ordens.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

LOG = logging.getLogger("poly_ws_v12")
URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_ASSETS = 24


class PolyBookHints:
    def __init__(self):
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.assets = ()
        self.hot = set()
        self.running = False
        self.socket = None

    def update(self, assets):
        selection = tuple(dict.fromkeys(str(a) for a in assets if a))[:MAX_ASSETS]
        with self.lock:
            changed = selection != self.assets
            self.assets = selection
            ws = self.socket
        if changed and ws is not None:
            try:
                ws.close()  # reconectar com um conjunto de ativos coerente
            except Exception:
                pass

    def mark(self, asset):
        asset = str(asset)
        with self.lock:
            if asset in self.assets:
                self.hot.add(asset)
                self.wake.set()

    def take_hot(self):
        with self.lock:
            found = set(self.hot)
            self.hot.clear()
            self.wake.clear()
            return found

    def consume(self, message):
        if message == "PONG":
            return
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        events = payload if isinstance(payload, list) else [payload]
        for item in events:
            if not isinstance(item, dict):
                continue
            if item.get("event_type") not in {"book", "price_change", "best_bid_ask"}:
                continue
            identifier = item.get("asset_id")
            if identifier:
                self.mark(identifier)
            for price in item.get("price_changes", []) or []:
                if isinstance(price, dict) and price.get("asset_id"):
                    self.mark(price["asset_id"])

    def run(self):
        try:
            import websocket
        except ImportError:
            LOG.warning("websocket-client indisponível; continuar em REST.")
            return
        self.running = True
        while self.running:
            with self.lock:
                tokens = self.assets
            if not tokens:
                time.sleep(3)
                continue

            def opened(ws):
                with self.lock:
                    self.socket = ws
                    selected = self.assets
                ws.send(json.dumps({"type": "market", "assets_ids": list(selected)}))

                def heartbeats():
                    while ws.keep_running:
                        try:
                            ws.send("PING")
                        except Exception:
                            break
                        time.sleep(10)
                threading.Thread(target=heartbeats, daemon=True).start()

            def closed(ws, *_):
                with self.lock:
                    if self.socket is ws:
                        self.socket = None

            ws = websocket.WebSocketApp(
                URL,
                on_open=opened,
                on_message=lambda _ws, message: self.consume(message),
                on_close=closed,
                on_error=lambda _ws, err: LOG.warning(
                    "Poly WS desconectado: %s", type(err).__name__
                ),
            )
            try:
                ws.run_forever(ping_interval=0)
            except Exception as exc:
                LOG.warning("Poly WS: %s; REST mantém-se ativo.",
                            type(exc).__name__)
            time.sleep(5)

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self.run, name="PolyBookHints",
                         daemon=True).start()

    def stop(self):
        self.running = False
        with self.lock:
            ws = self.socket
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self.wake.set()


def self_test():
    watcher = PolyBookHints()
    watcher.update(["a", "b", "a"])
    assert watcher.assets == ("a", "b")
    watcher.consume(json.dumps({"event_type": "book", "asset_id": "a"}))
    assert watcher.take_hot() == {"a"}
    watcher.consume(json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "b"}, {"asset_id": "unknown"}],
    }))
    assert watcher.take_hot() == {"b"}
    watcher.consume("not-json")
    assert not watcher.wake.is_set()
    print("✅ Poly WS V12: teste local de sinais e subscrições OK")


if __name__ == "__main__":
    self_test()
