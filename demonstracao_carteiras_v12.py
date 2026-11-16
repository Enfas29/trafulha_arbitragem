"""Demonstração de carteiras V12: DUAS mensagens, dinheiro e jogo fictícios.

Executar no Railway:
    python demonstracao_carteiras_v12.py --send-telegram

Sem --send-telegram: só imprime no ecrã, sem mensagens externas.
Dados sintéticos em TemporaryDirectory: NÃO toca em /data/paper_v12.sqlite3.
"""
from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

import paper_v12 as paper


def simulate():
    with tempfile.TemporaryDirectory(prefix="paper_demo_v12_") as directory:
        db = paper.open_database(str(Path(directory) / "demo.sqlite3"))
        try:
            now = time.time()
            pair = {
                "poly": {
                    "id": "EXEMPLO_NAO_REAL_POLY",
                    "type": "moneyline",
                    "sport": "basketball",
                    "start": now + 3600,
                },
                "sx": {
                    "hash": "EXEMPLO_NAO_REAL_SX",
                    "sport": "basketball",
                    "start": now + 3600,
                    "teams": ("Equipa Azul (EXEMPLO)", "Equipa Vermelha (EXEMPLO)"),
                },
                "poly_label": "Equipa Azul (EXEMPLO)",
                "sx_label": "Equipa Vermelha (EXEMPLO)",
            }
            # Livros sintéticos que ilustram uma oportunidade positiva.
            poly_book = [(0.43, 300.0)]
            sx_book = [(0.50, 300.0)]
            quote = paper.select_shadow_quote(
                poly_book, sx_book, 0.91, 100_000, 100_000
            )
            assert quote is not None, "Livros de demonstração não geraram quote"
            ident = paper.book_shadow_trade(
                db, pair, quote, "PRE_JOGO", now_epoch=int(now)
            )
            assert ident is not None, "Não foi possível registar operação demo"
            before = paper.format_open_message(
                db, ident, odds_poly=1 / 0.43, odds_sx=1 / 0.50,
                demo=True
            )
            assert paper.paper_summary(db)["open_trades"] == 1
            # Vencedor fictício. Este passo é apenas prova da transição da base.
            earned = paper.settle_normal(
                db, ident, "poly", verified=True
            )
            after = paper.format_settlement_message(db, ident, demo=True)
            totals = paper.paper_summary(db)
            assert earned == quote.profit_eur
            assert totals["open_trades"] == 0 and totals["settled"] == 1
            assert totals["realized_eur"] == earned
            assert totals["poly_free_eur"] > 1000
            assert totals["sx_free_eur"] < 1000
            return before, after
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description="Demonstração de carteiras (sem apostas)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--send-telegram", action="store_true",
                        help="Envia duas mensagens marcadas como FICTÍCIAS")
    args = parser.parse_args()
    first, second = simulate()
    assert "DEMONSTRAÇÃO FICTÍCIA" in first
    assert "RESULTADO DEMONSTRATIVO" in second
    assert "Saldo" not in first or "Carteira Polymarket" in first
    assert "Resultado líquido" in second
    if args.self_test:
        print("✅ DEMO V12: débito, crédito, saldos e mensagens (sem rede) OK")
        return
    print(first + "\n\n" + "=" * 42 + "\n\n" + second)
    print("\n⚠️ Demonstração fictícia: não usa cotações reais nem a carteira principal.")
    if args.send_telegram:
        first_ok = paper.send_telegram_notice(first)
        second_ok = paper.send_telegram_notice(second)
        print(
            "✅ Duas mensagens demonstrativas enviadas."
            if first_ok and second_ok
            else "⚠️ Uma ou ambas as mensagens Telegram não foram confirmadas."
        )


if __name__ == "__main__":
    main()
