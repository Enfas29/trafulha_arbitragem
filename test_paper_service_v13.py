"""Testes offline: isolamento do worker, falhas de rede e disco insuficiente."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monitor_v11 as monitor
import paper_service_v13 as service
import paper_v12 as paper


class WorkerTests(unittest.TestCase):
    def test_worker_uses_its_own_connection_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.sqlite3")
            entered = threading.Event()
            observed = []

            def process(conn, limit):
                observed.append((threading.get_ident(), paper.free_balances(conn), limit))
                worker.stop_event.set()
                entered.set()
                return {"checked": 0, "settled": 0}

            worker = service.PaperLifecycleWorker(path, batch_size=5, settlement_pass=process)
            with patch.object(paper, "retry_pending_settlement_notices", return_value=0):
                worker.start()
                first_thread = worker.thread
                worker.start()
                self.assertIs(worker.thread, first_thread)
                self.assertTrue(entered.wait(3), "worker não iniciou")
                worker.stop()
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(len(observed), 1)
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1], {"poly": 100000, "sx": 100000})
            self.assertEqual(observed[0][2], 5)

    def test_source_failure_does_not_skip_saved_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = service.PaperLifecycleWorker(str(Path(directory) / "paper.sqlite3"))

            def failed(conn, limit):
                worker.stop_event.set()
                raise TimeoutError("simulado, sem rede")

            worker.settlement_pass = failed
            with patch.object(paper, "retry_pending_settlement_notices", return_value=1) as notices:
                worker.start()
                worker.thread.join(3)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(notices.call_count, 1)
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
            notices.assert_not_called()

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
