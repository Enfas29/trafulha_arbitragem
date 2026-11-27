"""Testes offline: scanner, resultados e Telegram não esperam uns pelos outros."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monitor_v11 as monitor
import paper_service_v13 as service
import paper_v12 as paper
from test_paper_lifecycle_v13 import evidence_for, new_trade


class WorkerTests(unittest.TestCase):
    def test_worker_uses_its_own_connection_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            paper.open_database(path).close()
            entered = threading.Event()
            notice_entered = threading.Event()
            observed = []
            notice_threads = []

            def notices(conn, limit):
                notice_threads.append((threading.get_ident(), paper.free_balances(conn)))
                notice_entered.set()
                return 0

            def process(conn, limit):
                observed.append((threading.get_ident(), paper.free_balances(conn), limit))
                entered.set()
                return {"checked": 0, "settled": 0}

            worker = service.PaperLifecycleWorker(path, batch_size=5, settlement_pass=process)
            with patch.object(paper, "retry_pending_settlement_notices", side_effect=notices):
                worker.start()
                first_thread = worker.thread
                first_notifier = worker.notifier_thread
                worker.start()
                self.assertIs(worker.thread, first_thread)
                self.assertIs(worker.notifier_thread, first_notifier)
                try:
                    self.assertTrue(entered.wait(3), "worker não iniciou")
                    self.assertTrue(notice_entered.wait(3), "remetente não iniciou")
                    self.assertTrue(worker.is_alive())
                finally:
                    worker.stop()
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(worker.notifier_thread.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(observed), 1)
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1], {"poly": 100000, "sx": 100000})
            self.assertEqual(observed[0][2], 5)
            self.assertNotEqual(observed[0][0], notice_threads[0][0])
            self.assertEqual(notice_threads[0][1], observed[0][1])

    def test_source_failure_does_not_skip_saved_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            paper.open_database(path).close()
            worker = service.PaperLifecycleWorker(path)
            failed_source = threading.Event()
            notice_sent = threading.Event()

            def failed(conn, limit):
                failed_source.set()
                raise TimeoutError("simulado, sem rede")

            worker.settlement_pass = failed
            with patch.object(paper, "retry_pending_settlement_notices",
                              side_effect=lambda *_, **__: notice_sent.set() or 1) as notices:
                worker.start()
                try:
                    self.assertTrue(failed_source.wait(3))
                    self.assertTrue(notice_sent.wait(3))
                finally:
                    worker.stop()
            self.assertFalse(worker.thread.is_alive())
            self.assertGreaterEqual(notices.call_count, 1)
            self.assertIsNone(worker.last_check_epoch)

    def test_low_disk_does_not_call_results_or_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            paper.open_database(path).close()
            worker = service.PaperLifecycleWorker(path, settlement_pass=lambda *_: self.fail())
            with patch.object(service.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
                 patch.object(worker.stop_event, "wait", return_value=True), \
                 patch.object(paper, "retry_pending_settlement_notices") as notices:
                worker._run()
                worker._run_notices()
            notices.assert_not_called()

    def test_notify_wakes_saved_opening_with_results_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            conn = paper.open_database(path)
            initial_poll = threading.Event()
            delivered = threading.Event()
            worker = service.PaperLifecycleWorker(path, results_enabled=False)
            retry_open = paper.retry_pending_open_notices

            def observed_poll(ledger, limit):
                result = retry_open(ledger, limit=limit)
                initial_poll.set()
                return result

            with patch.object(paper, "retry_pending_open_notices", side_effect=observed_poll), \
                 patch.object(paper, "send_telegram_notice", side_effect=lambda _: delivered.set() or True):
                worker.start()
                try:
                    self.assertTrue(initial_poll.wait(3))
                    self.assertIsNone(worker.thread)
                    self.assertTrue(worker.is_alive())
                    ident = new_trade(conn, open_notice=True)
                    worker.notify()
                    self.assertTrue(delivered.wait(3), "notify esperou pelo ciclo periódico de 60 s")
                finally:
                    worker.stop()
                    conn.close()
            ledger = paper.open_database(path)
            try:
                self.assertIsNotNone(ledger.execute(
                    "SELECT sent_epoch FROM paper_v12_open_notifications WHERE trade_id=?", (ident,)
                ).fetchone()[0])
            finally:
                ledger.close()

    def test_blocked_telegram_does_not_block_results_or_notify(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            conn = paper.open_database(path)
            new_trade(conn, open_notice=True)
            conn.close()
            sending = threading.Event()
            release_sender = threading.Event()
            checked_results = threading.Event()
            notify_returned = threading.Event()

            def blocked_sender(message):
                sending.set()
                if not release_sender.wait(3):
                    raise AssertionError("teste não desbloqueou Telegram")
                return True

            def process(conn, limit):
                if not sending.wait(3):
                    raise AssertionError("remetente não iniciou")
                checked_results.set()
                return {"checked": 0, "settled": 0}

            worker = service.PaperLifecycleWorker(path, settlement_pass=process)
            with patch.object(paper, "send_telegram_notice", side_effect=blocked_sender):
                worker.start()
                caller = None
                try:
                    self.assertTrue(sending.wait(3))
                    caller = threading.Thread(target=lambda: (worker.notify(), notify_returned.set()), daemon=True)
                    caller.start()
                    self.assertTrue(notify_returned.wait(1), "notify bloqueou no envio")
                    self.assertTrue(checked_results.wait(1), "Telegram bloqueou consultas de resultados")
                    self.assertFalse(release_sender.is_set())
                finally:
                    release_sender.set()
                    worker.stop()
                    if caller is not None:
                        caller.join(1)

    def test_slow_result_source_does_not_delay_new_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            conn = paper.open_database(path)
            source_entered = threading.Event()
            release_source = threading.Event()
            delivered = threading.Event()

            def blocked_source(ledger, limit):
                source_entered.set()
                if not release_source.wait(3):
                    raise AssertionError("teste não desbloqueou fonte")
                return {"checked": 0, "settled": 0}

            worker = service.PaperLifecycleWorker(path, settlement_pass=blocked_source)
            with patch.object(paper, "send_telegram_notice", side_effect=lambda _: delivered.set() or True):
                worker.start()
                try:
                    self.assertTrue(source_entered.wait(3))
                    new_trade(conn, open_notice=True)
                    worker.notify()
                    self.assertTrue(delivered.wait(1), "consulta de resultados atrasou o aviso novo")
                    self.assertFalse(release_source.is_set())
                finally:
                    release_source.set()
                    worker.stop()
                    conn.close()

    def test_notify_does_not_shorten_result_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            paper.open_database(path).close()
            passes = []
            waits = []
            worker = service.PaperLifecycleWorker(
                path, interval_seconds=1,
                settlement_pass=lambda conn, limit: passes.append(limit) or {"checked": 0},
            )

            def advance_without_sleep(seconds):
                waits.append(seconds)
                for _ in range(100):
                    worker.notify()
                return len(waits) == 3

            with patch.object(worker.stop_event, "wait", side_effect=advance_without_sleep):
                worker._run()
            self.assertEqual(passes, [5, 5, 5])
            self.assertEqual(waits, [60, 60, 60])

    def test_repeated_wakes_respect_failed_notice_retry_and_keep_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            conn = paper.open_database(path)
            ident = new_trade(conn, open_notice=True)
            paper.settle_verified_outcomes(conn, ident, True, False, evidence_for(conn, ident))
            balances = paper.free_balances(conn)
            worker = service.PaperLifecycleWorker(path, results_enabled=False)
            first_attempt = threading.Event()
            second_attempt = threading.Event()
            checked_again = threading.Event()
            calls = []
            poll_count = []
            retry_open = paper.retry_pending_open_notices

            def failed_sender(message):
                calls.append(message)
                (first_attempt if len(calls) == 1 else second_attempt).set()
                return False

            def observed_poll(ledger, limit):
                result = retry_open(ledger, limit=limit)
                poll_count.append(1)
                if len(poll_count) > 1:
                    checked_again.set()
                return result

            with patch.object(paper, "send_telegram_notice", side_effect=failed_sender), \
                 patch.object(paper, "retry_pending_open_notices", side_effect=observed_poll):
                worker.start()
                try:
                    self.assertTrue(first_attempt.wait(3))
                    for _ in range(100):
                        worker.notify()
                    self.assertTrue(checked_again.wait(3))
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(service.PaperLifecycleWorker._notices_due(conn))
                    self.assertEqual(conn.execute(
                        "SELECT attempts FROM paper_v12_settlement_notifications WHERE trade_id=?", (ident,)
                    ).fetchone()[0], 0, "fecho ultrapassou a abertura pendente")
                    self.assertEqual(paper.free_balances(conn), balances)
                    # Simular passagem do recuo persistido, sem esperar um minuto.
                    with conn:
                        conn.execute("UPDATE paper_v12_open_notifications SET last_attempt_epoch=? WHERE trade_id=?",
                                     (int(time.time()) - 61, ident))
                    worker.notify()
                    self.assertTrue(second_attempt.wait(3))
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0], calls[1])
                    self.assertEqual(paper.free_balances(conn), balances)
                finally:
                    worker.stop()
                    conn.close()

    def test_database_failure_backs_off_despite_wakes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            paper.open_database(path).close()
            worker = service.PaperLifecycleWorker(path, results_enabled=False)
            backing_off = threading.Event()
            original_wait = worker.stop_event.wait
            waits = []

            def observed_wait(seconds):
                waits.append(seconds)
                backing_off.set()
                return original_wait(seconds)

            with patch.object(paper, "retry_pending_open_notices", side_effect=RuntimeError("offline")) as retry, \
                 patch.object(worker.stop_event, "wait", side_effect=observed_wait):
                worker.start()
                try:
                    self.assertTrue(backing_off.wait(3))
                    for _ in range(100):
                        worker.notify()
                    self.assertEqual(retry.call_count, 1)
                    self.assertEqual(waits, [60])
                finally:
                    worker.stop()

    def test_notifier_drains_multiple_batches_and_opening_before_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            conn = paper.open_database(path)
            first = new_trade(conn, "first", open_notice=True)
            paper.settle_verified_outcomes(conn, first, True, False, evidence_for(conn, first))
            for number in range(4):
                new_trade(conn, str(number), open_notice=True)
            balances = paper.free_balances(conn)
            messages = []
            complete = threading.Event()

            def sender(message):
                messages.append(message)
                if len(messages) == 6:
                    complete.set()
                return True

            worker = service.PaperLifecycleWorker(path, results_enabled=False)
            with patch.object(paper, "send_telegram_notice", side_effect=sender):
                worker.start()
                try:
                    self.assertTrue(complete.wait(3), "outbox restante esperou 60 segundos")
                finally:
                    worker.stop()
            self.assertEqual(sum("APOSTA VIRTUAL" in text for text in messages), 5)
            opening = next(i for i, text in enumerate(messages) if "APOSTA VIRTUAL #1" in text)
            closing = next(i for i, text in enumerate(messages) if "RESULTADO VIRTUAL CONFIRMADO" in text)
            self.assertLess(opening, closing)
            self.assertEqual(paper.free_balances(conn), balances)
            self.assertFalse(service.PaperLifecycleWorker._notices_due(conn))
            conn.close()

    def test_stop_shares_one_timeout_between_both_threads(self):
        joins = []
        class SlowThread:
            def __init__(self, name):
                self.name = name
            def join(self, timeout):
                joins.append((self.name, timeout))
            def is_alive(self):
                return True
        worker = service.PaperLifecycleWorker("unused.sqlite3")
        worker.thread = SlowThread("results")
        worker.notifier_thread = SlowThread("notices")
        with patch.object(service.time, "monotonic", side_effect=[100.0, 100.0, 108.0]):
            worker.stop(timeout=15)
        self.assertEqual(joins, [("results", 15.0), ("notices", 7.0)])
        self.assertTrue(worker.stop_event.is_set())
        self.assertTrue(worker.notice_event.is_set())

    def test_main_low_disk_exits_with_failure_before_opening_databases(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(monitor.os.environ, {"TRAFLUHA_STORAGE_MAINTENANCE": "0", "EUR_PER_USD": "0"}), \
             patch.object(monitor, "DB", str(Path(directory) / "monitor.sqlite3")), \
             patch.object(monitor, "PAPER_V12_ENABLED", False), \
             patch.object(monitor.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), \
             patch.object(monitor, "open_database") as open_database:
            with self.assertRaises(SystemExit) as failure:
                monitor.main(once=True)
            self.assertNotEqual(failure.exception.code, 0)
            open_database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
