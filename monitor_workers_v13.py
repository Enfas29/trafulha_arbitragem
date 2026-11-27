"""Trabalho auxiliar limitado, sem acesso à contabilidade das operações."""
from __future__ import annotations

import logging
import queue
import threading

LOG = logging.getLogger("monitor_workers_v13")


class SnapshotRefresher:
    """Uma descoberta de catálogo de cada vez; só publica resultados completos."""

    def __init__(self, loader):
        self.loader = loader
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.busy = False
        self.ready = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="catalog-v13", daemon=True)
            self.thread.start()

    def request(self):
        with self.lock:
            if self.busy or self.ready is not None or self.stop_event.is_set():
                return False
            self.busy = True
        self.wake.set()
        return True

    def poll(self):
        with self.lock:
            result, self.ready = self.ready, None
        return result

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.wait()
            self.wake.clear()
            if self.stop_event.is_set():
                break
            try:
                result = (True, self.loader())
            except Exception as exc:
                # Nunca propagar URLs, credenciais ou mensagens de requests.
                result = (False, type(exc).__name__)
            with self.lock:
                self.ready = result
                self.busy = False

    def stop(self, timeout=1.0):
        self.stop_event.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout)


class AsyncNoticeSender:
    """Resumos/saúde em fila limitada. Aberturas/fechos usam a outbox Paper."""

    def __init__(self, sender, max_pending=8):
        self.sender = sender
        self.pending = queue.Queue(maxsize=max(1, int(max_pending)))
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="summaries-v13", daemon=True)
            self.thread.start()

    def submit(self, message):
        if self.stop_event.is_set():
            return False
        try:
            self.pending.put_nowait(message)
            return True
        except queue.Full:
            LOG.warning("RESUMO | fila de mensagens cheia; envio adiado")
            return False

    def _run(self):
        while not self.stop_event.is_set():
            try:
                message = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.sender(message)
            except Exception as exc:
                LOG.warning("RESUMO | envio falhou (%s)", type(exc).__name__)
            finally:
                self.pending.task_done()

    def stop(self, timeout=1.0):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
