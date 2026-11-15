"""Diagnóstico V11: apenas GET públicos/autenticados de leitura + aviso Telegram.

Nunca chama main(), book_paper(), settle() ou endpoints de colocação de ordens.
Zero correspondências não é sucesso de arbitragem: é um diagnóstico incompleto.
"""

import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trafulha_arbitragem as bot

# Amostra limitada: não representa necessariamente todos os mercados.
bot.MAX_POLY_SERIES = 24
bot.MAX_EVENTS_PER_SERIES = 100
bot.MAX_SX_PAGES = 12
MAX_PAIRS = 6
MAX_FALLBACK_BOOKS = 6


def send_report(message):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("INFO: Telegram não configurado neste workflow.")
        return
    import requests
    try:
        response = requests.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": chat, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise ValueError("API Telegram devolveu ok=false")
        print("✅ Resumo de diagnóstico enviado ao Telegram")
    except Exception as exc:
        print("⚠️ Telegram: falha ao enviar relatório (" + type(exc).__name__ + ")")


def percentage_quote(poly_levels, sx_levels):
    """Margem ilustrativa para payout igual, com buffers V11.
    Só se aplica caso os dois resultados tenham regras de liquidação iguais.
    Não é uma garantia de execução nem cria operações.
    """
    if not poly_levels or not sx_levels:
        return None
    available = min(sum(x[1] for x in poly_levels),
                    sum(x[1] for x in sx_levels), 10.0)
    if available < 1:
        return None
    pc = bot.cost_for_payout(poly_levels, available)
    sc = bot.cost_for_payout(sx_levels, available)
    if pc is None or sc is None:
        return None
    buffer = pc * bot.POLY_FEE_BUFFER + sc * bot.SX_FEE_BUFFER
    total = pc + sc + buffer
    return 100 * (available - total) / total if total > 0 else None


def readable_error(exc):
    """Evita imprimir URLs ou cabeçalhos de pedidos nos logs."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" HTTP {code}" if code else "")


def main():
    if not os.getenv("SX_API_KEY"):
        raise SystemExit("❌ Falta o GitHub Secret SX_API_KEY")
    errors = []
    poly, sx, pairs = [], [], []
    try:
        print("▶ A consultar desportos e mercados Polymarket...")
        poly = bot.discover_poly()
    except Exception as exc:
        errors.append("Polymarket: " + readable_error(exc))
    try:
        print("▶ A consultar mercados SX Bet...")
        sx = bot.discover_sx()
    except Exception as exc:
        errors.append("SX Bet: " + readable_error(exc))

    print(f"📊 Polymarket: {len(poly)} mercados elegíveis na amostra")
    print(f"📊 SX Bet: {len(sx)} mercados ativos na amostra")
    if poly:
        print("   Desportos Poly:", dict(collections.Counter(m["sport"] for m in poly).most_common(8)))
    if sx:
        print("   Desportos SX:", dict(collections.Counter(m["sport"] for m in sx).most_common(8)))

    if poly and sx:
        try:
            pairs = bot.pair_markets(poly, sx)
        except Exception as exc:
            errors.append("Matching: " + readable_error(exc))
    print(f"🔎 Pares candidatos (cada direção conta): {len(pairs)}")
    print("   Matching preliminar: as regras de liquidação ainda exigem validação.")

    poly_ok = sx_ok = price_tests = potential_positive = 0
    examples = []
    seen_poly, seen_sx = set(), set()
    for pair in pairs[:MAX_PAIRS]:
        try:
            pa = pair["poly_asset"]
            sh = pair["sx"]["hash"]
            poly_levels, _ = bot.poly_book(pa)
            sx_levels, _ = bot.sx_book(sh, pair["sx_target"])
            seen_poly.add(pa)
            seen_sx.add(sh)
            poly_ok += 1
            sx_ok += 1
            price_tests += int(bool(poly_levels and sx_levels))
            roi = percentage_quote(poly_levels, sx_levels)
            if roi is not None:
                if roi > 0:
                    potential_positive += 1
                if len(examples) < 3:
                    examples.append(f"  {pair['sx']['teams'][0]} vs {pair['sx']['teams'][1]}: margem indicativa {roi:+.2f}%")
        except Exception as exc:
            print("⚠️ Livro de par candidato:", readable_error(exc))

    # Sem matching, testar as rotas de livros separadamente (sem fingir uma arbitragem).
    if not pairs:
        for market in poly[:MAX_FALLBACK_BOOKS]:
            try:
                token = market["assets"][0]
                bot.poly_book(token)
                poly_ok += 1
                break
            except Exception as exc:
                print("⚠️ Livro Polymarket:", readable_error(exc))
        for market in sx[:MAX_FALLBACK_BOOKS]:
            try:
                bot.sx_book(market["hash"], 0)
                sx_ok += 1
                break
            except Exception as exc:
                print("⚠️ Livro SX:", readable_error(exc))

    print(f"📚 Livros acessíveis: Polymarket {poly_ok} | SX Bet {sx_ok}")
    print(f"💹 Pares com liquidez em ambos os lados: {price_tests}")
    print(f"📈 Margens indicativas positivas na amostra: {potential_positive}")
    for line in examples:
        print(line)

    if errors:
        conclusion = "❌ Falha na recolha: " + "; ".join(errors)
    elif not poly or not sx:
        conclusion = "⚠️ Catálogo incompleto; rever filtros ou formatos de dados."
    elif not pairs:
        conclusion = "⚠️ APIs respondem, mas não houve jogos cruzados na amostra."
    elif price_tests == 0:
        conclusion = "⚠️ Pares encontrados, mas sem liquidez confirmada nos dois lados."
    else:
        conclusion = "✅ Preços lidos; arbitragem e regras ainda não validadas."
    print(conclusion)
    print("🔒 Só leitura. Nenhuma aposta nem carteira virtual foi alterada.")

    report = (
        "🧪 TESTE V11 · SÓ LEITURA\n"
        f"Poly: {len(poly)} | SX: {len(sx)}\n"
        f"Pares candidatos: {len(pairs)}\n"
        f"Livros: Poly {poly_ok} | SX {sx_ok}\n"
        f"Margens + indicativas: {potential_positive}\n"
        + conclusion + "\n"
        "🔒 Sem apostas"
    )
    send_report(report)

    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## Diagnóstico V11 — Só leitura\n\n")
            f.write("| Verificação | Resultado |\n|---|---|\n")
            for label, value in [
                ("Mercados Poly (amostra)", len(poly)),
                ("Mercados SX (amostra)", len(sx)),
                ("Pares candidatos", len(pairs)),
                ("Livros Poly acessíveis", poly_ok),
                ("Livros SX acessíveis", sx_ok),
                ("Pares com liquidez dos dois lados", price_tests),
                ("Margens indicativas positivas", potential_positive),
            ]:
                f.write(f"| {label} | {value} |\n")
            f.write("\n" + conclusion + "\n\n**Sem apostas reais ou virtuais.**\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
