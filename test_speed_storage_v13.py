"""Testes offline para a V13. Sem rede, apostas ou dados de producao."""
import tempfile
import threading
import time
from pathlib import Path
import monitor_v11 as monitor

def test_parallel():
    barrier = threading.Barrier(2, timeout=2)
    old_poly, old_sx = monitor.bot.poly_book, monitor.bot.sx_book
    try:
        def poly(asset):
            barrier.wait()
            return [(0.4, 100)], time.monotonic()
        def sx(market, target):
            barrier.wait()
            return [(0.5, 100)], time.monotonic()
        monitor.bot.poly_book, monitor.bot.sx_book = poly, sx
        pair = {"poly_asset":"test", "sx":{"hash":"test"}, "sx_target":0}
        a, b = monitor.fetch_books(pair)
        assert a[0] == [(0.4,100)] and b[0] == [(0.5,100)]
    finally:
        monitor.bot.poly_book, monitor.bot.sx_book = old_poly, old_sx

def test_retention():
    with tempfile.TemporaryDirectory() as td:
        conn = monitor.open_database(str(Path(td) / "monitor.sqlite3"))
        previous = monitor.OBSERVATION_MAX_ROWS
        try:
            monitor.OBSERVATION_MAX_ROWS = 2
            row = ("x", "tennis", "A vs B", "PRE_JOGO", "p", "s", "A", "B", "test")
            with conn:
                conn.executemany("INSERT INTO observations(observed_at,pair_key,sport,event,status,poly_market,sx_market,poly_outcome,sx_outcome,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 [(t, *row) for t in ["2000-01-01T00:00:00+00:00", monitor.utc_now(), monitor.utc_now(), monitor.utc_now()]])
                conn.execute("INSERT INTO alerts VALUES(?,?,?)", ("keep", 1, 0.6))
            assert monitor.prune_observations(conn) == 2
            assert conn.execute("SELECT id FROM observations ORDER BY id").fetchall() == [(3,), (4,)]
            assert conn.execute("SELECT pair_key FROM alerts").fetchall() == [("keep",)]
        finally:
            monitor.OBSERVATION_MAX_ROWS = previous
            conn.close()

if __name__ == "__main__":
    test_parallel()
    test_retention()
    print("V13 TESTS OK: parallel books, retention, alert preservation")
