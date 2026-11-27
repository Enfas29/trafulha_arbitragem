# Trafulha Paper V13.1

Versão integrada em 27/11/2026, a partir de `main` e da candidata
`v13-speed-storage`. O ponto de entrada continua a ser
`python monitor_v11.py` por compatibilidade com o serviço Railway.

## Funcionamento

O processo consulta dados públicos de Polymarket e SX Bet e mantém uma
simulação com carteiras virtuais. Não contém execução de ordens, chaves de
carteira ou transferências. Os montantes das mensagens são fictícios.

- Lê os dois livros em paralelo, com sessões HTTP por thread.
- Usa eventos WebSocket como avisos de atualização; os preços são sempre
  confirmados por novas consultas REST. Não reutiliza uma cotação WS como
  execução confirmada.
- Preserva a data/hora do jogo e os identificadores das seleções no ledger.
- Envia a abertura com jogo, hora de Portugal, seleção por plataforma,
  montantes e saldos virtuais antes/depois.
- Um worker separado verifica resultados oficiais a cada 60 segundos,
  em lotes de até cinco operações. Cada passagem tem um orçamento de
  45 segundos; o scanner continua a trabalhar em paralelo.
- Liquida apenas quando ambas as fontes confirmam os resultados e a
  identidade dos mercados e seleções coincide. A resolução oficial pode
  acontecer depois do fim do jogo.
- Calcula cada perna independentemente. Se os contratos resolverem de
  forma diferente, a simulação pode ter perda ou dois pagamentos; não
  transforma isso num lucro garantido.
- Guarda resultado, créditos fictícios e aviso de fecho na mesma transação.
  Os avisos são retentados e mantêm os saldos do instante do fecho. Uma
  abertura pendente é entregue antes do respetivo resultado.

Um timeout depois de o Telegram aceitar uma mensagem pode originar um
aviso repetido. A contabilidade é idempotente: não duplica o crédito.

## Resultados e casos que ficam pendentes

Polymarket: Gamma confirma o mercado e a seleção; a Data API v2 confirma
`resolved=true` e um payout on-chain `final_price` exatamente 0 ou 1.
Mercados `version=v2` usam `positionIds`; os legados usam `clobTokenIds`.
O token confirma a condição e os aliases explicitamente declarados.

SX Bet: `/markets/find` deve apresentar `reportedDate` e `outcome` válido,
com correspondência exata de mercado e seleção. `INACTIVE` sozinho não
é um resultado. `outcome=0`, payouts fracionários, anulações e identidades
contraditórias exigem revisão, sem crédito automático.

Falhas de rede, HTTP404 e metadados ainda incompletos deixam a operação
pendente. Os registos antigos em `REVIEW_REQUIRED` ou com margem histórica
igual ou superior a 10% continuam excluídos do fecho automático. Não há
reconstituição de preços ou fills que nunca foram observados.

## Preservação dos dados

Mantêm-se os mesmos caminhos `MONITOR_DB` e `PAPER_V12_DB` no volume
persistente `/data`. Carteiras existentes não são reiniciadas.

A primeira abertura de uma base antiga faz um backup consistente SQLite
em `/data/backups`, incluindo o WAL, verifica a integridade e só depois
acrescenta as colunas/tabelas V13. A migração compara carteiras e número de
operações antes/depois e faz rollback se houver divergência. O backup não
é repetido quando a base já está migrada.

As observações técnicas têm retenção de 12 horas e alvo de 25.000 linhas.
Esta limpeza não apaga carteiras, operações, resultados nem notificações.
É mantida uma reserva de 32 MiB. Espaço insuficiente bloqueia a migração
ou interrompe o monitor com um erro explícito, em vez de terminar como se
estivesse saudável. A capacidade do volume existente continua em 500 MB.

## Região e APIs

Região do servidor e endpoint da API são coisas diferentes. A configuração
existente `iad` foi mantida; não foram adicionados proxies nem espelhos.
Os testes históricos do projeto em 26/11/2026 compararam Virgínia e
Amesterdão: mediana conjunta 102,5 ms e 128 ms, respetivamente. Não são
medições desta versão nem uma garantia de latência futura.

A SX já usa os endpoints V3 de livro e WebSocket. Bases de consulta:

| Fonte | Base |
| --- | --- |
| Polymarket metadados | `https://gamma-api.polymarket.com` |
| Polymarket livros | `https://clob.polymarket.com` |
| Polymarket resultados | `https://data-api.polymarket.com/v2/tokens` |
| SX dados e resultados | `https://api.sx.bet` |

No arranque, `PAPER_API_CHECK` faz um diagnóstico público de metadados e
resultados a partir da região do serviço. Não cria operações nem altera
carteiras. Um diagnóstico pendente não é convertido num resultado.

## Configuração e verificação

As chaves existentes continuam nas variáveis Railway. Não colocar segredos
no repositório. `PAPER_V12_ENABLED=1` ativa apenas simulação e o limiar
acordado é `PAPER_V12_MIN_NET_ROI_PCT=0.5`. O máximo continua a ser 100 €
virtuais no total por operação, com 1.000 € iniciais por plataforma apenas
na criação de uma base nova. O câmbio é o valor configurado, e o pagamento
de cada operação usa a referência guardada na abertura.

Novas variáveis opcionais: `PAPER_RESULTS_ENABLED=1`,
`PAPER_RESULTS_INTERVAL_SECONDS=60`, `PAPER_RESULTS_BATCH_SIZE=5`.
O estado de `PAPER_V12_LIVE_UNVERIFIED` é preservado; o agendamento do jogo
por si só não comprova que já está ao vivo.

A suite GitHub Actions valida Python 3.11, migração, concorrência,
idempotência, resultados oficiais com dados fictícios, recuperação de
envio, classificação, armazenamento e isolamento das chaves. As versões
das dependências são fixadas em `requirements.txt`; `tzdata` garante os
fusos portugueses no contentor.

Logs úteis: `TRAFULHA`, `PAPER_SCHEMA`, `PAPER_ESTADO`, `CATALOGO`,
`PAPER_DIAGNOSTICO`, `PAPER_API_CHECK`, `PAPER_RESULTADOS` e `PAPER_FECHO`.
O resumo Telegram distingue jogos analisados, comparações possíveis e
operações virtuais. Mais jogos não implica uma operação elegível diária.

## Fontes dos contratos de consulta

- [Polymarket Data API v2 — OpenAPI](https://data-api.polymarket.com/v2/openapi.json)
- [Polymarket — detalhes de mercados](https://docs.polymarket.com/market-data/market-details)
- [SX Bet — mercados](https://docs.sx.bet/developers/markets-and-events)
- [SX Bet — Find Markets](https://docs.sx.bet/api-reference/get-markets-find)
- [SX Bet — referência oficial de outcomes](https://github.com/sx-bet/sx-bet-api-docs/blob/main/source/includes/_markets.md)
- [Railway — regiões e migração de volumes](https://docs.railway.com/deployments/regions)
