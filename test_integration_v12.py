"""Teste integrado PAPER V12: sem internet, sem aposta, sem Telegram real."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import monitor_v11 as monitor
import paper_v12 as paper


def main():
    now = time.time()
    pair = {
        "key": "poly-test:sx-test:0",
        "poly": {
            "id": "poly-test",
            "type": "moneyline",
            "sport": "basketball",
            "start": now + 3600,
        },
        "sx": {
            "hash": "sx-test",
            "sport": "basketball",
            "teams": ("Lakers", "Celtics"),
            "start": now + 3600,
            "live_enabled": False,
        },
        "poly_asset": "poly-asset",
        "sx_target": 1,
        "poly_label": "Lakers",
        "sx_label": "Celtics",
    }

    original_poly = monitor.bot.poly_book
    original_sx = monitor.bot.sx_book
    original_telegram = monitor.telegram
    original_fx = monitor.bot.FX
    sent = []

    try:
        monitor.bot.poly_book = lambda _: ([(0.40, 200.0)], time.monotonic())
        monitor.bot.sx_book = lambda *_: ([(0.50, 200.0)], time.monotonic())
        monitor.telegram = sent.append
        monitor.bot.FX = 0.91

        with tempfile.TemporaryDirectory() as td:
            monitor_db = monitor.open_database(str(Path(td) / "monitor.sqlite3"))
            paper_db = paper.open_database(str(Path(td) / "paper.sqlite3"))
            try:
                first = monitor.scan_pass(
                    monitor_db, [pair], 0, 0.91,
                    paper_conn=paper_db,
                    hot_sx_hashes={"sx-test"},
                    hot_poly_assets={"poly-asset"},
                )
                assert first["priced"] == 1, first
                stats = paper.paper_summary(paper_db)
                assert stats["open_trades"] == 1, stats
                assert 0 < stats["exposure_eur"] <= 100, stats
                assert stats["poly_free_eur"] < 1000
                assert stats["sx_free_eur"] < 1000
                assert any("PAPER V12" in msg for msg in sent), sent

                second = monitor.scan_pass(
                    monitor_db, [pair], 0, 0.91, paper_conn=paper_db
                )
                assert second["priced"] == 1
                assert paper.paper_summary(paper_db)["open_trades"] == 1
                assert paper_db.execute(
                    "SELECT COUNT(*) FROM paper_v12_trades"
                ).fetchone()[0] == 1

                monitor.send_summary(
                    monitor_db, 2, paper_conn=paper_db,
                    now_epoch=time.time()
                )
                assert any("Paper: 1 abertas" in msg for msg in sent)
                assert any("Só leitura" in msg for msg in sent)
                assert monitor.summary(monitor_db)["unique"] == 1

                # Falhas de dados: só uma notificação após 30 minutos e
                # nunca repetir a cada ciclo. A recuperação desarma o aviso.
                assert monitor.update_health(
                    monitor_db, checked=1, priced=1, now_epoch=1000
                ) is None
                assert monitor.update_health(
                    monitor_db, checked=1, priced=0, now_epoch=2000
                ) is None
                assert monitor.update_health(
                    monitor_db, checked=1, priced=0, now_epoch=3001
                ) is not None
                assert monitor.update_health(
                    monitor_db, checked=1, priced=0, now_epoch=3002
                ) is None
                assert monitor.update_health(
                    monitor_db, checked=1, priced=1, now_epoch=3003
                ) is None

                trade_id = paper_db.execute(
                    "SELECT id FROM paper_v12_trades"
                ).fetchone()[0]
                paper.flag_exception(
                    paper_db, trade_id,
                    "Resultado anulado: necessita verificação manual"
                )
                assert paper.paper_summary(paper_db)["requires_review"] == 1
                try:
                    paper.settle_normal(
                        paper_db, trade_id, "poly", verified=True
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        "Uma operação com exceção não pode ser liquidada normalmente"
                    )
            finally:
                monitor_db.close()
                paper_db.close()
    finally:
        monitor.bot.poly_book = original_poly
        monitor.bot.sx_book = original_sx
        monitor.telegram = original_telegram
        monitor.bot.FX = original_fx

    print("✅ PAPER V12 integrado: preços, duas carteiras, 100€, dedup, Telegram, exceções OK")


if __name__ == "__main__":
    main()
