# Trafulha Paper V13.2

Versão reativa de 27/11/2026, sobre a V13.1 integrada a partir de `main`
e da candidata `v13-speed-storage`. O ponto de entrada continua a ser
`python monitor_v11.py` por compatibilidade com o serviço Railway.

## Funcionamento

O processo consulta dados públicos de Polymarket e SX Bet e mantém uma
simulação com carteiras virtuais. Não contém execução de ordens, chaves de
carteira ou transferências. Os montantes das mensagens são fictícios.

- Lê os dois livros em paralelo, com sessões HTTP por thread.
- Recolhe novos eventos entre direções, em vez de terminar um lote inteiro.
  Prioriza os sinais mais antigos e reserva uma quota de rotação para os
  restantes jogos. O primeiro timestamp pendente nunca é substituído por
  um mais recente para fazer a latência parecer menor.
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
- Um notificador com ligação SQLite própria envia as mensagens da outbox;
  Telegram lento não bloqueia o scanner nem a verificação de resultados.
  Resumos periódicos e descoberta de catálogo também correm em background.
  O catálogo em uso só é substituído por um novo snapshot completo.

Um timeout depois de o Telegram aceitar uma mensagem pode originar um
aviso repetido. A contabilidade é idempotente: não duplica o crédito.

## Rapidez e validações na V13.2

O caminho normal deixou de fazer a consulta duplicada `FAST_PREVIEW`.
A primeira análise e a avaliação Paper usam a mesma leitura REST fresca.
Uma entrada elegível continua a exigir espera de **250 ms**, uma segunda
leitura independente de **ambas** as plataformas, preço/margem estáveis,
liquidez, saldo e limites de exposição. Preços antigos ou com timestamps
futuros são rejeitados. Se o jogo começar durante a confirmação, essa
autorização de pré-jogo é rejeitada; a próxima leitura reavalia a fase.
O prazo monotónico das duas cotações e a fase de pré-jogo são verificados
novamente dentro da transação SQLite, após obter o lock e antes do commit;
esperar pelo lock não pode transformar uma cotação velha numa entrada válida.

O scanner despacha uma direção de cada vez; as duas consultas HTTP são
paralelas, mas os débitos de novas operações continuam no mesmo scanner.
Por padrão há no máximo 10 arranques de pares por segundo e pelo menos
250 ms entre a conclusão e uma nova consulta da mesma direção. A segunda
confirmação acrescenta pedidos apenas quando a entrada é elegível. HTTP429
ativa recuo global de pelo menos 2 segundos e respeita `Retry-After` em
segundos ou data HTTP; nunca se reutiliza um preço anterior
para esconder uma falha. O timeout HTTP dos livros passa a 2 segundos
(intervalo configurável 1–7s; não é um SLA absoluto de duração do pedido).

`LATENCY_V13` publica a cada 30 segundos as durações individuais de Poly e
SX, espera em fila, consulta conjunta e decisão (`n`, `p50_ms`, `p95_ms`,
`max_ms`). Inclui erros/HTTP429 e descartes da fila. As métricas de decisão
começam na receção local do WS, antes da fila; não medem a transmissão
anterior ao robô nem preenchimento de ordens reais. Leituras com erro
também entram na duração de tentativa, com contadores de erro separados.

`WS_LATENCY` conserva os máximos por grupos de até `MONITOR_PAIRS_PER_PASS`
análises (ou cinco segundos), para comparação com o monitor anterior.
A mudança de agendamento e composição dos grupos exige cuidado ao comparar
quantis: uma mediana de máximos por grupo não é uma mediana por evento.
Não existe promessa de decisão em menos de um segundo em todos os casos,
nem execução real ou garantia de lucro/ausência de erros.

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
O notificador continua ativo quando `PAPER_RESULTS_ENABLED=0`.
Opções de rapidez: `MONITOR_WS_KEY_INTERVAL_SECONDS=0.25`,
`MONITOR_MAX_PAIRS_PER_SECOND=10`, `MONITOR_BOOK_TIMEOUT_SECONDS=2`.
`MONITOR_WS_PRIORITY_MAX` define a quota de eventos prioritários antes de
dar lugar à rotação vencida. `MONITOR_INTERVAL_SECONDS` dividido por
`MONITOR_PAIRS_PER_PASS` define a cadência de rotação reservada sob carga WS.
Quando não há sinais elegíveis, a rotação usa a capacidade livre, respeitando
o limite global de pedidos. `coverage` nos logs informa direções visitadas,
por visitar e a maior idade desde o último despacho; não é idade de preço.
Priorizar sinais pode espaçar a rotação dos restantes mercados sob uma
tempestade contínua; a quota evita starvation e a cobertura torna-o visível.
Os antigos
`MONITOR_FAST_PREVIEW_ENABLED` e `MONITOR_WS_REACT_GAP_SECONDS` já não
controlam o caminho reativo normal; não se acrescenta a pausa global de 1s.
O estado de `PAPER_V12_LIVE_UNVERIFIED` é preservado; o agendamento do jogo
por si só não comprova que já está ao vivo.

A suite GitHub Actions valida Python 3.11, migração, concorrência,
idempotência, resultados oficiais com dados fictícios, recuperação de
envio, classificação, armazenamento e isolamento das chaves. As versões
das dependências são fixadas em `requirements.txt`; `tzdata` garante os
fusos portugueses no contentor.

Logs úteis: `TRAFULHA`, `PAPER_SCHEMA`, `PAPER_ESTADO`, `CATALOGO`,
`PAPER_DIAGNOSTICO`, `PAPER_API_CHECK`, `PAPER_RESULTADOS`, `PAPER_FECHO`,
`PAPER_AVISOS`, `LATENCY_V13` e `WS_LATENCY`.
O resumo Telegram distingue jogos analisados, comparações possíveis e
operações virtuais. Mais jogos não implica uma operação elegível diária.

## Fontes dos contratos de consulta

- [Polymarket Data API v2 — OpenAPI](https://data-api.polymarket.com/v2/openapi.json)
- [Polymarket — detalhes de mercados](https://docs.polymarket.com/market-data/market-details)
- [SX Bet — mercados](https://docs.sx.bet/developers/markets-and-events)
- [SX Bet — Find Markets](https://docs.sx.bet/api-reference/get-markets-find)
- [SX Bet — referência oficial de outcomes](https://github.com/sx-bet/sx-bet-api-docs/blob/main/source/includes/_markets.md)
- [Railway — regiões e migração de volumes](https://docs.railway.com/deployments/regions)
