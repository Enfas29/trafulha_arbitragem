# Trafulha Paper V14

> Versão atual: [Trafulha Paper V15](README_V15.md), com custos oficiais,
> auditoria de decisões e mais cobertura. A política de banca abaixo mantém-se.

Versão de 27/11/2026, com banca proporcional e reset explícito sobre a
V13.3 de análise reativa e direto confirmado. O ponto de entrada continua a ser
`python monitor_v11.py` por compatibilidade com o serviço Railway.

## Banca proporcional e reset na V14

O montante de cada operação virtual é calculado a partir das duas carteiras,
do dinheiro já comprometido, da profundidade dos livros e da margem líquida.
Foram retirados os tetos monetários fixos de 100 € por operação, 500 € de
exposição aberta e 1.000 € de payout. Mantêm-se limites proporcionais para
deixar capital disponível em cada plataforma.

| Regra inicial | Percentagem | Com 1.000 € em cada carteira |
| --- | ---: | ---: |
| Reserva mínima por carteira | 30% | 300 € em cada plataforma |
| Máximo total de uma operação | 20% do capital conjunto | 400 € entre as duas plataformas |
| Exposição conjunta máxima para novas entradas | 70% | 1.400 € no total |

Capital contabilístico é saldo livre mais o custo das operações ainda
abertas/em revisão. Não inclui lucros previstos nem valor de mercado
inventado. Reservas são arredondadas para cima; limites para baixo. A
carteira mais limitada pode reduzir a operação abaixo do máximo total.
Depois de cada resultado, os limites acompanham o novo capital. Se perdas
deixarem a exposição acima da percentagem permitida, novas entradas ficam
bloqueadas até existir capacidade; o robô não inventa uma saída das posições.

O robô pode escolher uma operação menor quando os níveis mais fundos do
livro fazem desaparecer a margem. Cada cotação final volta a verificar
custos, arredondamento, liquidez e ROI. O orçamento é recalculado dentro do
lock de escrita SQLite, antes dos dois débitos, e fica guardado com a operação
em `bankroll_evidence_json` (schema15). Duas entradas concorrentes não podem
reutilizar a mesma reserva. A mensagem mostra o teto disponível nessa entrada
e as reservas das duas carteiras.

Configuração, em percentagens:

```text
PAPER_BANKROLL_RESERVE_PCT=30
PAPER_BANKROLL_MAX_OPERATION_PCT=20
PAPER_BANKROLL_MAX_EXPOSURE_PCT=70
```

A configuração é validada no arranque. A exposição máxima não pode exceder
100% menos a reserva. Uma alteração exige reiniciar o processo; nenhuma destas
variáveis ativa dinheiro real. O limiar de margem continua independente.

### Reset explícito das carteiras virtuais

`PAPER_RESET_REQUEST_ID` identifica um pedido único autorizado para repor
**1.000 € fictícios em cada carteira**. Sem identificador não há reset.
O monitor executa-o no arranque, antes dos workers de análise/resultados/avisos.
O modo `--once` não aplica pedidos de reset.

Antes de alterar saldos, cria e verifica um backup SQLite que inclui o WAL.
Sob o mesmo lock, guarda as linhas originais em `paper_v14_reset_trades`,
arquiva as operações anteriores como `RESET_ARCHIVED`, liberta as suas chaves
de deduplicação e repõe ambos os saldos numa única transação. Qualquer falha
faz rollback. Aberturas antigas e resultados antigos não podem enviar novos
avisos nem creditar a nova banca. Os outboxes históricos permanecem intactos;
não são marcados artificialmente como enviados.

O pedido concluído fica em `paper_v14_resets`. Deixar o mesmo identificador
configurado ou reiniciar o Railway **não volta a repor dinheiro**, mesmo depois
de novas operações ou ganhos. Só um identificador novo pede outro reset. O
histórico anterior e o backup mantêm-se disponíveis, mas não entram nas
contagens nem no resultado realizado do ciclo novo.

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

## Jogos em direto na V13.3

`PAPER_LIVE_ENABLED=1` ativa entradas **exclusivamente virtuais** durante
jogos confirmados em direto. É o valor padrão; exige também o Paper ativo
e câmbio válido. Passar a hora de início ou receber `liveEnabled=true` do
SX não prova que o jogo esteja em curso.

Uma candidata deve ter margem, profundidade e saldo elegíveis. Depois:

1. O robô consulta em paralelo o evento Gamma, o mercado SX, o estado
   oficial do jogo SX e os metadados atuais dos atrasos SX. Exige Poly
   `live=true`/`ended=false`, mercado ativo e a aceitar ordens, SX
   `status=ACTIVE`/`liveEnabled=true`, sem resultado, e fixture `status=2`.
2. Compara exatamente IDs, seleções, tokens, tipo e participantes com o
   catálogo. O protocolo Polymarket deve ser `v1` ou `v2` explícito; a
   versão determina `clobTokenIds` ou `positionIds`. Valores desconhecidos
   ou contraditórios bloqueiam a candidata.
3. Lê `secondsDelay` do mercado Poly e `bettingDelay` dos metadados SX.
   Na SX, após `gameTime`, a prioridade é liga, modalidade, valor padrão;
   antes usa o atraso de pré-jogo. Não converte um atraso ausente em zero.
4. Espera pelo maior dos dois atrasos numa fila limitada em segundo plano.
   O scanner continua nos outros jogos. Se o atraso exigido aumentar na
   revalidação, prolonga a espera. Não reserva nem debita carteiras nesta fase.
5. Depois do prazo, repete as quatro consultas oficiais. Só então volta
   aos livros, mantém a segunda confirmação independente após 250 ms e
   reavalia margem, liquidez, saldos e exposição antes da transação.

A prova de fase dura no máximo três segundos, contados **antes** de iniciar
as consultas. O prazo das cotações continua em dois segundos. Ambos são
verificados no ledger antes da transação, após obter o lock e antes do
commit; uma espera no SQLite não torna uma prova antiga válida. A prova
fica guardada com a operação e o aviso na mesma transação. A migração para
schema15 faz backup e preserva operações, carteiras e avisos anteriores,
exceto quando existe um pedido explícito de reset conforme descrito acima.

Um preço que desaparece, uma fonte inconclusiva, uma suspensão, um mercado
fechado ou uma prova expirada descartam a candidata. O limite é uma
operação por par de mercados no ciclo atual, partilhado entre pré-jogo e direto.
Mantêm-se os filtros de mercados
binários de vencedor com regras compatíveis; não se acrescentam futebol
1X2, totais ou handicaps sem uma validação própria dessas regras.

O aviso identifica `EM DIRETO confirmado`, o marcador/período disponibilizado
pelo Polymarket e os atrasos considerados. É uma hipótese depois de
revalidar, não uma simulação de cada preenchimento real: as duas ordens
reais teriam riscos separados de atraso, suspensão e execução parcial.
A subida da odd de uma favorita a perder não constitui arbitragem por si;
o conjunto de seleções opostas ainda tem de passar os custos e o limiar.

`PAPER_LIVE_API` faz uma pequena consulta de diagnóstico por atualização
do catálogo, sem poder abrir operações. `PAPER_LIVE_FILA` regista fases,
limites e razões de rejeição. Eventos `LIVE` na fila conservam o instante
inicial da candidata: a duração inclui a espera oficial, não apenas o
tempo de processamento. Uma prova pronta ganha prioridade por até três
segundos, sem renovar a sua validade original no ledger.

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
persistente `/data`. Carteiras existentes só são reiniciadas quando existe
um novo pedido explícito `PAPER_RESET_REQUEST_ID`; um simples deploy não as repõe.

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
acordado é `PAPER_V12_MIN_NET_ROI_PCT=0.5`. Os montantes seguem a política
proporcional V14, com 1.000 € iniciais por plataforma na criação de uma base
nova ou num reset explicitamente pedido. O câmbio é o valor configurado, e o pagamento
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
`PAPER_V12_LIVE_UNVERIFIED` fica obsoleto e não autoriza novas entradas,
mesmo quando vale `1`. Operações antigas com essa fase permanecem no
histórico. O modo `--once` não espera atrasos LIVE nem abre entradas LIVE.

A suite GitHub Actions valida Python 3.11, migração, concorrência,
idempotência, resultados oficiais com dados fictícios, recuperação de
envio, classificação, armazenamento e isolamento das chaves. As versões
das dependências são fixadas em `requirements.txt`; `tzdata` garante os
fusos portugueses no contentor.

Logs úteis: `TRAFULHA`, `PAPER_SCHEMA`, `PAPER_RESET`, `PAPER_BANCA`, `PAPER_ESTADO`, `CATALOGO`,
`PAPER_DIAGNOSTICO`, `PAPER_API_CHECK`, `PAPER_RESULTADOS`, `PAPER_FECHO`,
`PAPER_AVISOS`, `LATENCY_V13` e `WS_LATENCY`.
Diagnóstico de direto: `PAPER_LIVE`, `PAPER_LIVE_API`, `PAPER_LIVE_FILA`
e `PAPER_LIVE_ENTRADA`.
O resumo Telegram distingue jogos analisados, comparações possíveis e
operações virtuais. Mais jogos não implica uma operação elegível diária.

## Fontes dos contratos de consulta

- [Polymarket Data API v2 — OpenAPI](https://data-api.polymarket.com/v2/openapi.json)
- [Polymarket — detalhes de mercados](https://docs.polymarket.com/market-data/market-details)
- [SX Bet — mercados](https://docs.sx.bet/developers/markets-and-events)
- [SX Bet — Find Markets](https://docs.sx.bet/api-reference/get-markets-find)
- [Polymarket — evento e estado de direto](https://docs.polymarket.com/api-reference/events/get-event-by-id)
- [SX Bet — estado oficial do jogo](https://docs.sx.bet/api-reference/get-fixture-status)
- [SX Bet — códigos de estado](https://docs.sx.bet/api-reference/fixture-statuses)
- [SX Bet — atrasos de apostas](https://docs.sx.bet/developers/betting-delays)
- [SX Bet — metadados e atrasos atuais](https://docs.sx.bet/api-reference/get-metadata-obv3)
- [SX Bet — referência oficial de outcomes](https://github.com/sx-bet/sx-bet-api-docs/blob/main/source/includes/_markets.md)
- [Railway — regiões e migração de volumes](https://docs.railway.com/deployments/regions)
