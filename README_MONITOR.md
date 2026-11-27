# Monitor V11 — instruções

> **Versão atual:** [Trafulha Paper V15](README_V15.md).
> Este documento descreve a primeira versão; a retenção, WebSockets e
> ciclo de vida, direto confirmado, banca, custos e cobertura estão documentados na V15.

O monitor_v11.py é um **monitor só de leitura** para a Polymarket e SX Bet.

- Guarda cada observação na base de dados SQLite; mantém até 30 dias.
- Atualiza os catálogos a cada 10 minutos, por defeito.
- Procura preços continuamente por REST, por lotes e com limites de pedidos.
- Envia um **resumo Telegram a cada 6 horas** (por defeito) com confrontos distintos analisados, odds válidas, melhor margem estimada e sinais potenciais. O horário da última tentativa fica no SQLite e é mantido após reinício.
- **Não usa WebSockets nesta primeira versão**: não pode prometer reação imediata a uma mudança de odd.
- Não cria ordens, não chama book_paper, não altera carteiras virtuais nem aposta dinheiro real.
- Qualquer sinal no Telegram é meramente hipotético: não confirma equivalência das regras de liquidação, taxa real ou execução de ambas as apostas.

## Testar pelo GitHub

GitHub > Actions > **Testar Monitor Continuo V11** > **Run workflow**.
O teste executa o --self-test offline, seguido de --once (uma passagem pelas APIs).
A base SQLite de exemplo aparece nos **Artifacts** durante três dias.

O GitHub Actions **não é** o processo 24/7: termina depois do teste.

## Executar continuamente num servidor (por exemplo Railway)

1. Criar um serviço ligado ao repositório GitHub e escolher o branch main.
2. Definir python monitor_v11.py como **Start Command** (ou usar railway.toml).
3. Configurar **uma única réplica**, para não duplicar a recolha de dados e os alertas.
4. Adicionar um Volume persistente montado em /data.
5. Configurar as variáveis no serviço:
   - MONITOR_DB=/data/monitor_v11.sqlite3
   - SX_API_KEY (apenas leitura, necessária se forem usados outros fluxos da SX no futuro)
   - TELEGRAM_TOKEN e TELEGRAM_CHAT_ID
   - EUR_PER_USD com cotação realista e atualizada de **1 USD em euros**
   - Opcional: MONITOR_INTERVAL_SECONDS=20, MONITOR_PAIRS_PER_PASS=15,
     MONITOR_CATALOG_SECONDS=600, MONITOR_SUMMARY_SECONDS=21600 (6 horas)
6. Iniciar o serviço e consultar os logs.
7. No primeiro arranque após a atualização, o Telegram recebe "MONITOR V11 · ÚLTIMAS 6H". Os seguintes relatórios são enviados de 6 em 6 horas, mesmo depois de reinícios normais (sem repetir o relatório a cada deploy).

Se EUR_PER_USD não estiver definido, o programa continua a recolher preços,
mas não dimensiona posições em euros nem envia sinais de stake hipotético.

## Comandos locais

    pip install -r requirements.txt
    python monitor_v11.py --self-test
    python monitor_v11.py --once
    python monitor_v11.py

O histórico é um SQLite, não dados de carteira. A coluna hypothetical_profit_eur
é uma estimativa condicional **não liquidada**. Nunca usar os valores como prova
de rentabilidade sem comparar as regras e execução dos dois contratos.

## Próxima etapa

Validar mercado a mercado as regras de liquidação (incluindo prolongamento,
retiradas no ténis, abandonos, adiamentos e voids), taxas reais, conversão USDC/EUR,
latência e risco de execução de apenas uma das pernas. Só depois experimentar
paper trading com duas carteiras virtuais efetivamente debitadas e liquidadas.
