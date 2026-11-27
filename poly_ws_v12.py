"""Sinais Polymarket CLOB WebSocket; preços finais sempre confirmados por REST.

Adaptação opcional, sem qualquer execução de ordens.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from feed_coverage_v15 import read_limit, select_identifiers

LOG = logging.getLogger("poly_ws_v12")
URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Local load bound, not a claim about the exchange's maximum allowance.
MAX_ASSETS = read_limit("MONITOR_POLY_WS_ASSETS", 512, 512)


class PolyBookHints:
    def __init__(self, max_assets=None):
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.assets = ()
        self.hot = {}  # asset/hash -> primeiro evento monotónico pendente
        self.running = False
        self.socket = None
        self.valid_updates = 0
        self.last_update_epoch = None
        self._close_thread = None
        self._closing_socket = None
        self._close_pending = None
        self.max_assets = MAX_ASSETS if max_assets is None else max_assets
        if type(self.max_assets) is not int or not 1 <= self.max_assets <= 512:
            raise ValueError("Limite local Poly WS deve estar entre 1 e 512")
        self._selection_cursor = 0
        self.available_assets = 0

    def _request_close_locked(self, ws):
        """Coalesce reconnect requests; caller holds ``self.lock``.

        Keep at most one close in progress and one pending socket. A newer
        current socket supersedes an obsolete pending one; ``opened`` reads
        the final asset selection under the same lock when reconnecting.
        """
        if ws is None or ws is self._closing_socket or ws is self._close_pending:
            return
        self._close_pending = ws
        if self._close_thread is None:
            self._close_thread = threading.Thread(
                target=self._close_sockets, name="PolyBookHintsClose", daemon=True,
            )
            self._close_thread.start()

    def _close_sockets(self):
        while True:
            with self.lock:
                ws = self._close_pending
                self._close_pending = None
                if ws is None:
                    self._close_thread = None
                    return
                self._closing_socket = ws
            try:
                # websocket-client may wait for its close handshake. Never
                # hold the state lock or block the scanner on this call.
                ws.close()
            except Exception:
                pass
            finally:
                with self.lock:
                    self._closing_socket = None

    def update(self, assets):
        selection, cursor, count = select_identifiers(assets, self.max_assets, self._selection_cursor)
        with self.lock:
            # The same membership in a different catalogue order needs no
            # reconnect. Real membership changes retain the non-blocking close.
            changed = set(selection) != set(self.assets)
            if changed:
                self.assets = selection
                self._request_close_locked(self.socket)
            self._selection_cursor = cursor
            self.available_assets = count
        LOG.info("WS_COVERAGE_V15 | Poly | disponíveis=%d | selecionados=%d | omitidos=%d | limite=%d",
                 count, len(selection), count - len(selection), self.max_assets)

    def coverage(self):
        with self.lock:
            return {"available": self.available_assets, "selected": len(self.assets),
                    "omitted": max(0, self.available_assets - len(self.assets)),
                    "limit": self.max_assets}

    def mark(self, asset):
        asset = str(asset)
        first_valid = False
        with self.lock:
            if asset in self.assets:
                self.hot.setdefault(asset, time.monotonic())
                self.wake.set()
                self.valid_updates += 1
                self.last_update_epoch = time.time()
                first_valid = self.valid_updates == 1
        if first_valid:
            LOG.info(
                "Poly WS V12: primeiro evento de livro recebido "
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
                LOG.info(
                    "Poly WS V12: ligação estabelecida; subscrição enviada "
                    "para %d tokens. A aguardar primeiro evento de livro.",
                    len(selected),
                )

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
            self._request_close_locked(self.socket)
        self.wake.set()


def self_test():
    watcher = PolyBookHints()
    watcher.update(["a", "b", "a"])
    assert watcher.assets == ("a", "b")
    watcher.consume(json.dumps({"event_type": "book", "asset_id": "a"}))
    assert watcher.take_hot() == {"a"}
    assert watcher.valid_updates == 1
    assert watcher.last_update_epoch is not None
    watcher.consume(json.dumps({
        "event_type": "price_change",
        "price_changes": [{"asset_id": "b"}, {"asset_id": "unknown"}],
    }))
    assert watcher.take_hot() == {"b"}
    assert watcher.valid_updates == 2
    # Coalescer dois eventos, mantendo o instante do primeiro.
    watcher.consume(json.dumps({"event_type": "book", "asset_id": "a"}))
    first = watcher.hot["a"]
    watcher.consume(json.dumps({"event_type": "best_bid_ask", "asset_id": "a"}))
    assert watcher.hot["a"] == first
    stamped = watcher.take_events()
    assert stamped == {"a": first}
    assert first <= time.monotonic()
    watcher.consume("not-json")
    assert not watcher.wake.is_set()
    print("✅ Poly WS V12: teste local de sinais e subscrições OK")


if __name__ == "__main__":
    self_test()
