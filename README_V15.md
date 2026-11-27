# Trafulha Paper V15

Histórico da V15. Futebol e hóquei foram acrescentados na [V16](README_V16.md).

Versão de 27/11/2026. Mantém o ponto de entrada `python monitor_v11.py`,
as duas carteiras virtuais e a política de banca V14. Não cria ordens nem
movimenta dinheiro real. Esta atualização **não pede nem repete um reset**.

## Custos oficiais e retorno líquido

O worker `FeeProvider` consulta a taxa de lucro da conta SX por
`GET /user/fees-v3`. `takerPayoutFee: null` significa zero oficial; um campo
ausente, um erro ou uma resposta expirada não significam zero.

A descoberta preserva `feesEnabled` e `feeSchedule` de cada mercado Poly.
Quando faltam, o worker consulta o mercado completo na Gamma. A curva
desportiva validada é `shares × rate × price × (1-price)`, expoente 1.
Outras curvas ficam pendentes de um adaptador, com motivo explícito.

As taxas são atualizadas em segundo plano a cada 300 s, com validade máxima
de 900 s e repetição de falhas a cada 30 s. O scanner só consulta a memória.
Uma nova operação exige custos verificados e ainda válidos, incluindo no
momento do commit SQLite. O mesmo contexto acompanha ambas as leituras.

O cálculo conserva a reserva adversa de 1,25% por perna, separada das taxas.
A comissão SX incide no lucro da sua aposta vencedora. A entrada guarda os
dois créditos líquidos possíveis e calcula o ROI pelo menor retorno.
O fecho usa os valores congelados na entrada, não as taxas de um dia posterior.

O schema Paper 16 é aditivo e cria backup antes da migração. Mantém os saldos,
operações antigas, arquivos, notificações e identificador do reset V14.
Operações antigas sem contexto V15 preservam o seu cálculo original.

## Leituras paralelas sem concorrência nas carteiras

Até três pares podem consultar os livros em paralelo. Não há uma fila
ilimitada de tarefas: o pool só aceita trabalho quando tem capacidade.
Os resultados pertencem a um despacho e geração de catálogo; não são uma
cache de preços e nunca são reutilizados. Uma atualização do catálogo
invalida os resultados pendentes da geração anterior.

A confirmação, os débitos virtuais e a outbox continuam no scanner. Mantêm-se
a segunda leitura após 250 ms, a idade máxima de 2 s, os limites de variação,
os atrasos oficiais LIVE, a verificação de identidade e a reserva de banca.
Um 429 impõe recuo também às segundas leituras, incluindo quando o recuo
começa enquanto outro par está em análise.

O máximo de arranques continua em 10 pares/s por padrão. Subscrever mais
mercados não garante reação inferior a 1 s em todos: é necessário medir
a fila e a cobertura, especialmente quando vários jogos mudam ao mesmo tempo.

## Cobertura e descoberta

Os feeds recebem o catálogo completo. Os padrões são 128 mercados SX e
256 resultados Poly, suficientes para o catálogo observado antes desta
atualização (44–45 jogos). Limites são configuráveis e explicitamente
registados. Identificadores excedentes rodam nas atualizações; mudar apenas
a ordem dos mesmos identificadores não provoca reconexão.

`coverage()` descreve identificadores selecionados, não uma confirmação de
subscrição pela plataforma. Os avisos de subscrição e eventos recebidos
devem ser usados para verificar que os feeds estão realmente ativos.

A Gamma passa a paginar, com orçamento e rotação de séries. Séries que já
produziram correspondências têm prioridade; outras séries compatíveis
também são exploradas. A SX regista se ficou um `nextKey` por consultar.
Uma falha de uma página não substitui o catálogo completo em utilização
por uma resposta parcial. Truncamentos por orçamento são identificados.

Continuam a ser exigidas identidade e equivalência dos mercados de vencedor.
Futebol, hóquei, totais, handicaps e outros contratos ainda não validados
continuam fora das entradas automáticas. Ampliar essas famílias é um trabalho
separado de regras de resultado, prolongamento, empate e anulação.

## Diagnóstico individual e Telegram

O monitor guarda amostras de decisões em `decision_audit_v15`:
jogo, mercados/lados, fase, hora, margem inicial, montante, custos, margem
final, preços, até oito níveis por livro e motivo observado. A amostragem
normal é 60 s por par/motivo/fase; positivos usam 15 s. Mudanças de motivo
ou fase e operações registadas recebem tratamento próprio.

Os dados ficam limitados a 12 h/10.000 amostras, preservando positivos antes
de negativos quando o limite é atingido. Cada detalhe usa uma lista explícita
de campos permitidos, sem chaves, cabeçalhos ou mensagens HTTP arbitrárias.
Uma falha do diagnóstico não reverte nem repete um débito já confirmado.

O resumo Telegram distingue amostras de diagnóstico de operações e indica
dados anteriores/posteriores ao último reset. A última margem positiva
inclui jogo, hora de Lisboa e motivo. Os máximos históricos das odds não
significam uma oportunidade disponível no momento do resumo.

## Configuração e observabilidade

| Variável | Padrão | Limite local |
| --- | ---: | ---: |
| `MONITOR_BOOK_WORKERS` | 3 | 1–4 |
| `MONITOR_SX_WS_MARKETS` | 128 | 512 |
| `MONITOR_POLY_WS_ASSETS` | 256 | 512 |
| `MONITOR_POLY_PAGES_PER_SERIES` | 4 | 10 |
| `MONITOR_POLY_REQUEST_BUDGET` | 140 | 400 |

Logs: `PAPER_CUSTOS_SX`, `PAPER_CUSTOS`, `PAPER_DECISAO`, `PAPER_AUDIT`,
`DISCOVERY_V15`, `MATCHING_V15`, `WS_COVERAGE_V15`, `WS_COBERTURA`,
`LATENCY_V13` (nome mantido), `PAPER_LIVE_FILA`, `PAPER_ESTADO`.

Os novos testes cobrem taxas por resultado, cache/expiração, migração,
liquidação idempotente, auditoria, retenção, paginação, feeds, leituras
paralelas, geração antiga, recheck e preservação das carteiras. O workflow
GitHub Actions mantém também as regressões V12/V13/V14.

## Fontes de integração

- https://docs.sx.bet/developers/fees
- https://docs.sx.bet/developers/betting-delays
- https://docs.sx.bet/developers/realtime-reference
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/market-data/market-details
- https://docs.polymarket.com/market-data/realtime-data

Histórico e política de banca: [V14 e V13](README_V13.md).
