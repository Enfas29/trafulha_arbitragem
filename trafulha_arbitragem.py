
                if enviar_alerta_telegram(mensagem):
                    alertas_enviados += 1
                    break

    return alertas_enviados


# ============================================================
# 8. CICLO PRINCIPAL
# ============================================================

def ciclo_scanner():
    global diagnostico_inicial_feito
    with wallet_lock:
        banca_poly = paper_wallet["poly"]["available"]
        banca_sx = paper_wallet["sx"]["available"]

    logger.info("A iniciar scanner Paper Trading V8.3 (Poly Disp: $%.2f | SX Disp: $%.2f).", banca_poly, banca_sx)

    stats = {
        "jogos_casados": 0,
        "melhor_margem": None
    }

    jogos_poly = obter_jogos_polymarket()
    mercados_sx = obter_mercados_sx()

    mercados_relevantes = [
        sx_m for sx_m in mercados_sx
        if any(jogo_coincide_com_titulo(sx_m["team_one"], sx_m["team_two"], jp["title"]) for jp in jogos_poly)
    ]

    fazer_diag = not diagnostico_inicial_feito
    odds_sx = obter_melhores_odds_sx(mercados_relevantes, log_diagnostico=fazer_diag) if mercados_relevantes else {}

    if fazer_diag:
        diagnostico_inicial_feito = True
        logger.info(
            "Diagnostico Cruzamento | Pares por nome: %s | Mercados SX com odds OBv3: %s",
            len(mercados_relevantes),
            len(odds_sx)
        )

    total_alertas = analisar_cruzamento_web3(jogos_poly, mercados_relevantes, odds_sx, stats)

    melhor_str = f"{stats['melhor_margem']:.2f}%" if stats["melhor_margem"] is not None else "N/A"
    logger.info(
        "Ciclo concluido | Poly Disp: $%.2f | SX Disp: $%.2f | Casados: %d | Melhor margem: %s | Alertas: %d",
        paper_wallet["poly"]["available"],
        paper_wallet["sx"]["available"],
        stats["jogos_casados"],
        melhor_str,
        total_alertas
    )

    with state_lock:
        scanner_status["last_scan"] = datetime.now(timezone.utc).isoformat()
        scanner_status["poly_games_checked"] = len(jogos_poly)
        scanner_status["sx_markets_checked"] = len(mercados_sx)
        scanner_status["name_matched_games"] = len(mercados_relevantes)
        scanner_status["matched_games"] = stats["jogos_casados"]
        scanner_status["best_margin_pct"] = stats["melhor_margem"]
        scanner_status["opportunities"] += total_alertas
        scanner_status["last_error"] = None


def correr_robo_continuo():
    while True:
        try:
            ciclo_scanner()
        except Exception as e:
            logger.exception("Erro geral do scanner Paper Trading.")
            with state_lock:
                scanner_status["last_error"] = str(e)
        time.sleep(SCAN_INTERVAL)


# ============================================================
# 9. SERVIDOR WEB RENDER
# ============================================================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            with state_lock:
                data = dict(scanner_status)
            with wallet_lock:
                data["paper_wallet"] = dict(paper_wallet)
            data["auto_bet"] = AUTO_BET
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            with wallet_lock:
                p_disp = paper_wallet["poly"]["available"]
                p_inv = paper_wallet["poly"]["invested"]
                s_disp = paper_wallet["sx"]["available"]
                s_inv = paper_wallet["sx"]["invested"]
            body = (
                "ROBO ARBITRAGEM PAPER TRADING V8.3 ONLINE (PRICE RECHECK)\n"
                f"Polymarket Disp: ${p_disp:.2f} | Investido: ${p_inv:.2f}\n"
                f"SX Bet Disp: ${s_disp:.2f} | Investido: ${s_inv:.2f}\n"
                "Health / Wallet: /health\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_web():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    logger.info("Servidor ativo na porta %s", port)
    server.serve_forever()
