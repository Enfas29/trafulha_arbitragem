"""Resultados e avisos virtuais, separados da leitura de mercados.

Cada thread tem a sua ligação SQLite. Telegram lento não bloqueia o scanner
nem as consultas de resultados. Nunca envia ordens às plataformas.
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
NOTICE_RETRY_SECONDS = 60
NOTICE_BATCH_SIZE = 3


class PaperLifecycleWorker:
    def __init__(self, db_path, interval_seconds=60, batch_size=5,
                 settlement_pass=None, results_enabled=True):
        self.db_path = str(db_path)
        self.interval_seconds = max(60, min(3600, int(interval_seconds)))
        self.batch_size = max(1, min(10, int(batch_size)))
        self.settlement_pass = settlement_pass
        self.results_enabled = bool(results_enabled)
        self.stop_event = threading.Event()
        self.notice_event = threading.Event()
        self.thread = None
        self.notifier_thread = None
        self._started = False
        self.last_check_epoch = None
        self.last_counts = None

    def start(self):
        if self._started:
            return
        self._started = True
        self.notifier_thread = threading.Thread(
            target=self._run_notices, name="PaperNoticesV13", daemon=True,
        )
        self.notifier_thread.start()
        if self.results_enabled:
            self.thread = threading.Thread(
                target=self._run, name="PaperResultsV13", daemon=True,
            )
            self.thread.start()

    def notify(self):
        """Acordar o remetente depois do commit da outbox; nunca espera HTTP."""
        self.notice_event.set()

    def is_alive(self):
        """O monitor deve detetar a perda de qualquer thread necessária."""
        return bool(
            self.notifier_thread is not None and self.notifier_thread.is_alive()
            and (not self.results_enabled
                 or self.thread is not None and self.thread.is_alive())
        )

    def stop(self, timeout=15):
        self.stop_event.set()
        self.notice_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in (self.thread, self.notifier_thread):
            if worker is None or worker is threading.current_thread():
                continue
            worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                LOG.warning("PAPER_WORKER | pedido de paragem; %s em conclusão", worker.name)

    def _disk_available(self):
        return shutil.disk_usage(Path(self.db_path).resolve().parent).free >= MIN_FREE_BYTES

    @staticmethod
    def _notices_due(conn):
        """Drenar lotes restantes sem repetir avisos ainda em recuo de 60 s.

        Um fecho bloqueado por uma abertura falhada não alimenta um hot loop.
        A decisão de enviar continua nos claims atómicos de paper_v12.
        """
        retry_before = int(time.time()) - NOTICE_RETRY_SECONDS
        return conn.execute("""
            SELECT 1 FROM paper_v12_open_notifications n
            JOIN paper_v12_trades t ON t.id=n.trade_id
            WHERE n.sent_epoch IS NULL
              AND t.status IN ('OPEN_CONDITIONAL','REVIEW_REQUIRED','SETTLED')
              AND (n.last_attempt_epoch IS NULL OR n.last_attempt_epoch<=?)
            UNION ALL
            SELECT 1 FROM paper_v12_settlement_notifications n
            JOIN paper_v12_trades t ON t.id=n.trade_id
            WHERE n.sent_epoch IS NULL AND t.status='SETTLED'
              AND (n.last_attempt_epoch IS NULL OR n.last_attempt_epoch<=?)
              AND NOT EXISTS(SELECT 1 FROM paper_v12_notifications done
                             WHERE done.trade_id=n.trade_id)
              AND NOT EXISTS(SELECT 1 FROM paper_v12_open_notifications opening
                             WHERE opening.trade_id=n.trade_id AND opening.sent_epoch IS NULL)
            LIMIT 1
        """, (retry_before, retry_before)).fetchone() is not None

    def _run_notices(self):
        conn = None
        try:
            conn = paper.open_database(self.db_path)
            LOG.info("PAPER_AVISOS | ATIVO | fila persistente; Telegram fora do scanner")
            while not self.stop_event.is_set():
                # Limpar ANTES da consulta: um commit/notify durante HTTP não
                # se perde e provoca outra passagem sem esperar 60 segundos.
                self.notice_event.clear()
                failed = False
                if not self._disk_available():
                    LOG.error("PAPER_AVISOS | PAUSADO | reserva de disco atingida")
                    failed = True
                else:
                    try:
                        opened = paper.retry_pending_open_notices(conn, limit=NOTICE_BATCH_SIZE)
                        if opened:
                            LOG.info("PAPER_ABERTURA | %d aviso(s) confirmado(s) pelo Telegram", opened)
                    except Exception as exc:
                        failed = True
                        LOG.warning("PAPER_ABERTURA | reenvio adiado (%s)", type(exc).__name__)
                    if self.stop_event.is_set():
                        break
                    try:
                        closed = paper.retry_pending_settlement_notices(conn, limit=NOTICE_BATCH_SIZE)
                        if closed:
                            LOG.info("PAPER_FECHO | %d aviso(s) confirmado(s) pelo Telegram", closed)
                    except Exception as exc:
                        failed = True
                        LOG.warning("PAPER_FECHO | reenvio adiado (%s)", type(exc).__name__)
                    if not failed and self._notices_due(conn):
                        self.notice_event.set()
                if self.stop_event.is_set():
                    break
                if failed:
                    # Falhas de SQLite/disco não devem provocar um loop com
                    # os eventos de wake; os avisos persistidos ficam intactos.
                    if self.stop_event.wait(NOTICE_RETRY_SECONDS):
                        break
                else:
                    self.notice_event.wait(NOTICE_RETRY_SECONDS)
        except Exception as exc:
            LOG.error("PAPER_AVISOS | worker indisponível (%s)", type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

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
                if not self._disk_available():
                    LOG.error("PAPER_RESULTADOS | PAUSADO | reserva de disco atingida")
                else:
                    try:
                        counts = process(conn, limit=self.batch_size)
                        self.last_counts = counts
                        self.last_check_epoch = time.time()
                        LOG.info("PAPER_RESULTADOS | %s", counts)
                    except Exception as exc:
                        LOG.warning("PAPER_RESULTADOS | consulta adiada (%s)", type(exc).__name__)
                    # Também acorda após falha parcial: um resultado anterior
                    # do mesmo lote pode já ter persistido o respetivo aviso.
                    self.notify()
                if self.stop_event.wait(self.interval_seconds):
                    break
        except Exception as exc:
            LOG.error("PAPER_RESULTADOS | worker indisponível (%s)", type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()

