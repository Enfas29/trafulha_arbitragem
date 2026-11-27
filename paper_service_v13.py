"""Acompanhamento de resultados virtuais, separado da leitura de mercados.

Cada worker tem a sua ligação SQLite. Nunca envia ordens às plataformas.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

import paper_v12 as paper

LOG = logging.getLogger("paper_service_v13")
MIN_FREE_BYTES = 32 * 1024 * 1024


class PaperLifecycleWorker:
    def __init__(self, db_path, interval_seconds=60, batch_size=5,
                 settlement_pass=None):
        self.db_path = str(db_path)
        self.interval_seconds = max(60, min(3600, int(interval_seconds)))
        self.batch_size = max(1, min(10, int(batch_size)))
        self.settlement_pass = settlement_pass
        self.stop_event = threading.Event()
        self.thread = None
        self.last_check_epoch = None
        self.last_counts = None

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._run, name="PaperResultsV13", daemon=True,
        )
        self.thread.start()

    def stop(self, timeout=15):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                LOG.warning("PAPER_RESULTADOS | pedido de paragem; consulta em conclusão")

    def _run(self):
        conn = None
        try:
            process = self.settlement_pass
            if process is None:
                from paper_results_v13 import run_settlement_pass, probe_public_sources
                process = run_settlement_pass
                LOG.info("PAPER_API_CHECK | %s", probe_public_sources())
            conn = paper.open_database(self.db_path)
            LOG.info(
                "PAPER_RESULTADOS | ATIVO | intervalo=%ds | lote=%d | "
                "duas fontes oficiais; apenas créditos fictícios",
                self.interval_seconds, self.batch_size,
            )
            while not self.stop_event.is_set():
                if shutil.disk_usage(Path(self.db_path).resolve().parent).free < MIN_FREE_BYTES:
                    LOG.error("PAPER_RESULTADOS | PAUSADO | reserva de disco atingida")
                else:
                    try:
                        paper.retry_pending_open_notices(conn)
                    except Exception as exc:
                        LOG.warning("PAPER_ABERTURA | reenvio adiado (%s)", type(exc).__name__)
                    try:
                        counts = process(conn, limit=self.batch_size)
                        self.last_counts = counts
                        self.last_check_epoch = time.time()
                        LOG.info("PAPER_RESULTADOS | %s", counts)
                    except Exception as exc:
                        # Não perder avisos já gravados se uma consulta falhar.
                        LOG.warning("PAPER_RESULTADOS | consulta adiada (%s)", type(exc).__name__)
                    try:
                        delivered = paper.retry_pending_settlement_notices(conn)
                        if delivered:
                            LOG.info("PAPER_FECHO | %d aviso(s) confirmado(s) pelo Telegram", delivered)
                    except Exception as exc:
                        LOG.warning("PAPER_FECHO | reenvio adiado (%s)", type(exc).__name__)
                if self.stop_event.wait(self.interval_seconds):
                    break
        except Exception as exc:
            LOG.error("PAPER_RESULTADOS | worker indisponível (%s)", type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

