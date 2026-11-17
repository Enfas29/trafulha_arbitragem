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
    original_notice = paper.send_telegram_notice
    original_fx = monitor.bot.FX
    sent = []

    try:
        monitor.bot.poly_book = lambda _: ([(0.40, 200.0)], time.monotonic())
        monitor.bot.sx_book = lambda *_: ([(0.50, 200.0)], time.monotonic())
        monitor.telegram = sent.append
        paper.send_telegram_notice = lambda msg: sent.append(msg) or True
        monitor.bot.FX = 0.91

        with tempfile.TemporaryDirectory() as td:
            monitor_db = monitor.open_database(str(Path(td) / "monitor.sqlite3"))
            paper_db = paper.open_database(str(Path(td) / "paper.sqlite3"))
            try:
                first = monitor.scan_pass(
                    monitor_db, [pair], 0, 0.91,
                    paper_conn=paper_db,
                )
                assert first["priced"] == 1, first
                stats = paper.paper_summary(paper_db)
                assert stats["open_trades"] == 1, stats
                assert 0 < stats["exposure_eur"] <= 100, stats
                assert stats["poly_free_eur"] < 1000
                assert stats["sx_free_eur"] < 1000
                assert any("APOSTA VIRTUAL" in msg for msg in sent), sent
                assert any("Carteira Polymarket" in msg for msg in sent), sent
                assert any("Carteira SX Bet" in msg for msg in sent), sent
                assert any("AGUARDA LIQUIDAÇÃO" in msg for msg in sent), sent
                assert paper_db.execute(
                    "SELECT sent_epoch IS NOT NULL FROM paper_v12_open_notifications "
                    "WHERE trade_id=1"
                ).fetchone()[0] == 1
                assert sum("APOSTA VIRTUAL #1" in msg for msg in sent) == 1
                assert "odds indicadas".lower() in " ".join(sent).lower()

                # Odds revalidadas duas vezes no mercado original.
                # Mudança súbita de 4 cêntimos não pode abrir outra operação.
                unstable_pair = {
                    **pair,
                    "key": "poly-unstable:sx-unstable:0",
                    "poly": {**pair["poly"], "id": "poly-unstable"},
                    "sx": {**pair["sx"], "hash": "sx-unstable"},
                }
                calls = {"poly": 0}
                def jumping_poly(_):
                    calls["poly"] += 1
                    value = .40 if calls["poly"] % 2 else .44
                    return [(value, 200.)], time.monotonic()
                monitor.bot.poly_book = jumping_poly
                rechecked = monitor.scan_pass(
                    monitor_db, [unstable_pair], 0, .91,
                    paper_conn=paper_db
                )
                assert rechecked["priced"] == 1
                assert paper.paper_summary(paper_db)["open_trades"] == 1
                monitor.bot.poly_book = lambda _: (
                    [(0.40, 200.)], time.monotonic()
                )


                second = monitor.scan_pass(
                    monitor_db, [pair], 0, 0.91, paper_conn=paper_db
                )
                assert second["priced"] == 1
                assert paper.paper_summary(paper_db)["open_trades"] == 1
                assert paper_db.execute(
                    "SELECT COUNT(*) FROM paper_v12_trades"
                ).fetchone()[0] == 1
                assert paper.send_open_once(paper_db, 1)
                assert sum("APOSTA VIRTUAL #1" in msg for msg in sent) == 1

                monitor.send_summary(
                    monitor_db, 2, paper_conn=paper_db,
                    now_epoch=time.time()
                )
                assert any("Paper: 1 abertas" in msg for msg in sent)
                assert any("Só leitura" in msg for msg in sent)
                assert monitor.summary(monitor_db)["unique"] == 2

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
                # Outro confronto: uma operação virtual ganha e envia
                # UMA mensagem Telegram, sem voltar a creditar em duplicado.
                second_pair = {
                    **pair,
                    "poly": {**pair["poly"], "id": "poly-test-2"},
                    "sx": {**pair["sx"], "hash": "sx-test-2"},
                }
                quote = paper.select_shadow_quote(
                    [(0.40, 200.0)], [(0.50, 200.0)], 0.91,
                    *(
                        paper.free_balances(paper_db)[k]
                        for k in ("poly", "sx")
                    ),
                )
                assert quote is not None
                second_id = paper.book_shadow_trade(
                    paper_db, second_pair, quote, "PRE_JOGO"
                )
                assert second_id is not None
                poly_prior = paper.free_balances(paper_db)["poly"]
                payout = quote.payout_eur_cents
                profit = paper.settle_normal(
                    paper_db, second_id, "poly", verified=True
                )
                assert paper.free_balances(paper_db)["poly"] == poly_prior + payout
                assert profit > 0
                assert "Venceu: Lakers" in paper.format_settlement_message(
                    paper_db, second_id
                )
                original_notice = paper.send_telegram_notice
                notice_list = []
                try:
                    def fake_notice(message):
                        notice_list.append(message)
                        return True
                    paper.send_telegram_notice = fake_notice
                    assert paper.send_settlement_once(paper_db, second_id)
                    assert paper.send_settlement_once(paper_db, second_id)
                    assert len(notice_list) == 1
                    assert "RESULTADO VIRTUAL CONFIRMADO" in notice_list[0]
                    assert "Carteira Polymarket" in notice_list[0]
                finally:
                    paper.send_telegram_notice = original_notice
                # A segunda liquidação da mesma operação tem de falhar.
                try:
                    paper.settle_normal(
                        paper_db, second_id, "poly", verified=True
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError("Duplo crédito na carteira virtual")

            finally:
                monitor_db.close()
                paper_db.close()
    finally:
        monitor.bot.poly_book = original_poly
        monitor.bot.sx_book = original_sx
        monitor.telegram = original_telegram
        paper.send_telegram_notice = original_notice
        monitor.bot.FX = original_fx

    print("✅ PAPER V12 integrado: preços, duas carteiras, 100€, dedup, Telegram, exceções OK")


if __name__ == "__main__":
    main()
