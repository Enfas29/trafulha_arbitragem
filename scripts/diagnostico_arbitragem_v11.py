"""Diagnóstico V11: apenas GET públicos/autenticados de leitura + aviso Telegram.

Nunca chama main(), book_paper(), settle() ou endpoints de colocação de ordens.
Zero correspondências não é sucesso de arbitragem: é um diagnóstico incompleto.
"""

import collections
import statistics
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trafulha_arbitragem as bot

# Amostra limitada: não representa necessariamente todos os mercados.
bot.MAX_POLY_SERIES = 60
bot.MAX_EVENTS_PER_SERIES = 150
bot.MAX_SX_PAGES = 12
MAX_PAIRS = 40
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


def percentage_quote(poly_levels, sx_levels, poly_buffer=None, sx_buffer=None):
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
    if poly_buffer is None:
        poly_buffer = bot.POLY_FEE_BUFFER
    if sx_buffer is None:
        sx_buffer = bot.SX_FEE_BUFFER
    buffer = pc * poly_buffer + sc * sx_buffer
    total = pc + sc + buffer
    return 100 * (available - total) / total if total > 0 else None


def readable_error(exc):
    """Evita imprimir URLs ou cabeçalhos de pedidos nos logs."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" HTTP {code}" if code else "")



def diagnose_matching(poly, sx):
    """Explica porque mercados não emparelham, sem alterar os filtros de segurança."""
    counts = collections.Counter()
    families_poly = collections.Counter(p["sport"] for p in poly)
    families_sx = collections.Counter(s["sport"] for s in sx)
    types_sx = collections.Counter((s["sport"], s["type"]) for s in sx)
    common = sorted(set(families_poly) & set(families_sx))
    near = []

    for s in sx:
        a, b = s["teams"]
        named = [
            p for p in poly
            if (bot.same_team(a, p["teams"][0]) and bot.same_team(b, p["teams"][1]))
            or (bot.same_team(a, p["teams"][1]) and bot.same_team(b, p["teams"][0]))
        ]
        if not named:
            counts["Sem os dois nomes correspondentes"] += 1
            continue

        same_sport = [p for p in named if p["sport"] == s["sport"]]
        if not same_sport:
            counts["Desporto identificado de forma diferente"] += 1
            near.append(("DESPORTO", s, named[0]))
            continue

        same_time = [p for p in same_sport if bot.match_time(p["start"], s["start"])]
        if not same_time:
            counts["Horários demasiado afastados"] += 1
            near.append(("HORÁRIO", s, same_sport[0]))
            continue

        if s["sport"] in bot.EXCLUDED_SETTLEMENT_SPORTS:
            counts["Excluído por regras de liquidação"] += 1
            near.append(("LIQUIDAÇÃO", s, same_time[0]))
            continue

        expected_type = 226 if s["sport"] in {"basketball", "american_football"} else 52
        if s["type"] != expected_type:
            counts["Tipo de mercado SX não admitido"] += 1
            near.append(("TIPO SX", s, same_time[0]))
            continue

        if not (bot.same_team(s["outcomes"][0], a) and bot.same_team(s["outcomes"][1], b)):
            counts["Outcomes SX não identificados"] += 1
            near.append(("OUTCOMES", s, same_time[0]))
            continue

        counts["Possível par a investigar manualmente"] += 1
        near.append(("REVER MARKET TYPE POLY", s, same_time[0]))

    print("\n========== PORQUE NÃO HÁ MATCH ==========")
    print("Desportos Poly:", dict(families_poly.most_common(12)))
    print("Desportos SX:", dict(families_sx.most_common(12)))
    print("Desportos presentes nos dois:", ", ".join(common) if common else "(nenhum)")
    print("Atenção: estes nomes são categorias; partidas comuns dependem de datas/outcomes.")
    print("Tipos SX por desporto:", dict(types_sx.most_common(12)))
    for label, count in counts.most_common():
        print(f"  {label}: {count}")
    print("Exemplos públicos de mercados Poly:")
    for p in poly[:6]:
        print(f"  [{p['sport']}] {p['teams'][0]} vs {p['teams'][1]}")
    print("Exemplos públicos de mercados SX:")
    for s in sx[:6]:
        print(f"  [{s['sport']}, tipo {s['type']}] {s['teams'][0]} vs {s['teams'][1]}")
    if near:
        print("Pares de nomes iguais que ficaram bloqueados:")
        for reason, s, p in near[:8]:
            gap_h = abs(s["start"] - p["start"]) / 3600
            print(f"  {reason}: {s['teams'][0]} vs {s['teams'][1]} "
                  f"| SX={s['sport']} tipo={s['type']} "
                  f"| Poly={p['sport']} | diferença horário={gap_h:.1f}h")
    print("==========================================\n")
    return counts


def audit_candidate_uniqueness(poly, sx, pairs):
    """Audita correspondências ambíguas, sem alterar o matching."""
    by_sport = collections.defaultdict(list)
    for p in poly:
        by_sport[p["sport"]].append(p)

    unique_count = multi_count = eligible_count = 0
    ambiguous = []
    for m in sx:
        if m["sport"] in bot.EXCLUDED_SETTLEMENT_SPORTS:
            continue
        expected_type = 226 if m["sport"] in {"basketball", "american_football"} else 52
        if m["type"] != expected_type:
            continue
        a, b = m["teams"]
        candidates = []
        for p in by_sport.get(m["sport"], ()):
            if not bot.match_time(m["start"], p["start"]):
                continue
            x, y = p["teams"]
            if not ((bot.same_team(a, x) and bot.same_team(b, y))
                    or (bot.same_team(a, y) and bot.same_team(b, x))):
                continue
            candidates.append(p)
        if candidates:
            eligible_count += 1
        if len(candidates) == 1:
            unique_count += 1
        elif len(candidates) > 1:
            multi_count += 1
            if len(ambiguous) < 6:
                ambiguous.append((m, candidates))

    actual = sorted({(p["poly"]["id"], p["sx"]["hash"]) for p in pairs})
    print(f"✔ Eventos Poly/SX aceites (únicos): {len(actual)}")
    print(f"🔎 SX com candidatos de mesmo desporto, nomes e hora: {eligible_count}")
    print(f"   Apenas 1 mercado Polymarket correspondente: {unique_count}")
    print(f"   Mais de 1 mercado Polymarket correspondente: {multi_count}")
    for m, candidates in ambiguous:
        kinds = sorted({str(p.get("type")) for p in candidates})
        print(f"   AMBÍGUO: {m['teams'][0]} vs {m['teams'][1]} "
              f"({m['sport']}) | {len(candidates)} mercados Poly | "
              f"tipos Poly: {', '.join(kinds[:5])}")
    print("🔒 Mercados ambíguos NÃO são convertidos em pares automaticamente.")
    return len(actual), eligible_count, unique_count, multi_count


def select_price_sample(pairs, limit):
    """Amostra de desportos equilibrada; testa ambos os lados do mesmo jogo."""
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for pair in pairs:
        groups[pair["sx"]["sport"]][pair["sx"]["hash"]].append(pair)

    by_sport = {sport: list(events.values()) for sport, events in groups.items()}
    selected = []
    while len(selected) < limit and any(by_sport.values()):
        for sport in sorted(by_sport):
            if not by_sport[sport]:
                continue
            event_pairs = by_sport[sport].pop(0)
            selected.extend(event_pairs[:limit - len(selected)])
            if len(selected) >= limit:
                break
    return selected


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
    print("🔐 Filtro Polymarket: apenas sportsMarketType=moneyline")
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
    reasons = diagnose_matching(poly, sx)
    unique_pairs, named_time, unique_potential, ambiguous_count = audit_candidate_uniqueness(poly, sx, pairs)

    # Cobertura: quantos mercados SX têm ao menos um evento Poly
    # do mesmo desporto e com início a menos de 3 horas.
    poly_by_sport = collections.defaultdict(list)
    for p in poly:
        poly_by_sport[p["sport"]].append(p)
    time_overlap = [
        m for m in sx
        if any(bot.match_time(p["start"], m["start"])
               for p in poly_by_sport.get(m["sport"], ()))
    ]
    print(f"🕒 Mercados SX com jogos Poly do mesmo desporto e janela horária: {len(time_overlap)}")
    for m in time_overlap[:6]:
        print(f"  SX candidato a verificar nomes: [{m['sport']}] "
              f"{m['teams'][0]} vs {m['teams'][1]}")
    print("   Matching preliminar: as regras de liquidação ainda exigem validação.")

    selected_pairs = select_price_sample(pairs, MAX_PAIRS)
    print(f"🎯 Preços: {len(selected_pairs)} direções selecionadas "
          f"de {len(pairs)} possíveis (amostra equilibrada por desporto)")
    print("   A estimativa usa payout comum de até 10 USDC, não stake de 100 €.")
    poly_ok = sx_ok = price_tests = potential_positive = priced_count = 0
    examples = []
    roi_samples = []
    gross_samples = []
    gross_positive = 0
    seen_poly, seen_sx = set(), set()
    for pair in selected_pairs:
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
            gross = percentage_quote(poly_levels, sx_levels, 0.0, 0.0)
            if roi is not None and gross is not None:
                gross_samples.append(gross)
                if gross > 0:
                    gross_positive += 1
                priced_count += 1
                roi_samples.append((roi, pair["sx"]["sport"],
                                    pair["sx"]["teams"][0], pair["sx"]["teams"][1],
                                    pair["poly_label"], pair["sx_label"]))
                if roi > 0:
                    potential_positive += 1
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
    print(f"📈 Margens indicativas positivas após buffers: {potential_positive}")
    print(f"🧮 Margens brutas positivas (sem qualquer taxa ou custo): {gross_positive}")
    print(f"    Buffers V11: Poly {bot.POLY_FEE_BUFFER*100:.1f}% e SX {bot.SX_FEE_BUFFER*100:.1f}% do custo de cada aposta; não são taxas oficiais.")
    if gross_samples:
        print(f"    Melhor margem bruta: {max(gross_samples):+.2f}% | mediana: {statistics.median(gross_samples):+.2f}%")
    print("    Não usar margem bruta como autorização para apostar.")
    if roi_samples:
        rois = [entry[0] for entry in roi_samples]
        print("📉 Margens calculáveis (indicativas): "
              f"{priced_count}/{len(selected_pairs)} | "
              f"mín={min(rois):+.2f}% | mediana={statistics.median(rois):+.2f}% "
              f"| máx={max(rois):+.2f}%")
        print("Melhores 5 margens na amostra (não são oportunidades validadas):")
        for roi, sport, a, b, poly_name, sx_name in sorted(
                roi_samples, reverse=True)[:5]:
            print(f"  {roi:+.2f}% | [{sport}] {a} vs {b} "
                  f"| Poly:{poly_name} | SX:{sx_name}")
    else:
        print("⚠️ Não foi possível calcular margens com a profundidade disponível.")

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
        f"Confrontos aceites: {unique_pairs}\n"
        f"Preços avaliados: {priced_count}/{len(selected_pairs)}\n"
        f"Brutas +: {gross_positive} | Após buffers +: {potential_positive}\n"
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
            f.write("> Apenas mercados **moneyline** Polymarket; "
                    "outros mercados são excluídos por segurança.\n\n")
            f.write("| Verificação | Resultado |\n|---|---|\n")
            for label, value in [
                ("Mercados Poly (amostra)", len(poly)),
                ("Filtro Poly", "sportsMarketType=moneyline"),
                ("Mercados SX (amostra)", len(sx)),
                ("Pares candidatos", len(pairs)),
                ("Confrontos distintos aceites", unique_pairs),
                ("Mercados SX candidatos por nome/desporto/hora", named_time),
                ("SX com múltiplos mercados Poly possíveis", ambiguous_count),
                ("SX com mesmo desporto e janela horária", len(time_overlap)),
                ("Livros Poly acessíveis", poly_ok),
                ("Livros SX acessíveis", sx_ok),
                ("Direções de aposta selecionadas para preço", len(selected_pairs)),
                ("Pares com liquidez dos dois lados", price_tests),
                ("Margens efetivamente calculadas", priced_count),
                ("Margens indicativas positivas após buffers", potential_positive),
                ("Margens brutas positivas antes de custos", gross_positive),
                ("Melhor margem bruta sem custos", f"{max(gross_samples):+.2f}%" if gross_samples else "N/D"),
                ("Melhor margem indicativa", f"{max([r[0] for r in roi_samples]):+.2f}%" if roi_samples else "N/D"),
                ("Mediana das margens", f"{statistics.median([r[0] for r in roi_samples]):+.2f}%" if roi_samples else "N/D"),
            ]:
                f.write(f"| {label} | {value} |\n")
            f.write("\n**Atenção:** margens brutas ignoram TODAS as taxas, buffers, "
                    "slippage e atrasos; não são oportunidades seguras.\n\n")
            f.write(f"Buffers de diagnóstico: Poly {bot.POLY_FEE_BUFFER*100:.1f}% + "
                    f"SX {bot.SX_FEE_BUFFER*100:.1f}% sobre o custo de cada perna; "
                    "não representam a tabela oficial de taxas.\n\n")
            f.write("\nAmostra de preços com payout comum de até 10 USDC. "
                    "Margens apenas indicativas; liquidação e execução não validadas.\n")
            if roi_samples:
                f.write("\n### Melhores margens observadas na amostra\n\n")
                for roi, sport, a, b, poly_name, sx_name in sorted(
                    roi_samples, reverse=True
                )[:6]:
                    f.write(f"- {roi:+.2f}% — {sport}: {a} vs {b} "
                            f"(Poly: {poly_name}; SX: {sx_name})\n")
            f.write("\n### Principais razões de exclusão\n\n")
            for reason, count in reasons.most_common(6):
                f.write(f"- {reason}: {count}\n")
            f.write("\n### Desportos mais comuns\n\n")
            f.write("- Poly: " + ", ".join(
                f"{k} ({v})" for k, v in collections.Counter(
                    m["sport"] for m in poly).most_common(8)
            ) + "\n")
            f.write("- SX: " + ", ".join(
                f"{k} ({v})" for k, v in collections.Counter(
                    m["sport"] for m in sx).most_common(8)
            ) + "\n")
            f.write("\n" + conclusion + "\n\n**Sem apostas reais ou virtuais.**\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
