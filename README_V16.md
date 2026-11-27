# Trafulha Paper V16

Atualização de 27/11/2026 para acrescentar futebol e hóquei no gelo às
comparações e operações virtuais. O ponto de entrada continua a ser
`python monitor_v11.py`. Não existem ordens reais, transferências ou depósitos.
As carteiras, o histórico e o pedido de reset já aplicado são preservados.

## O que é comparável

Cada mercado das novas modalidades exige um certificado explícito que guarda
as regras, identidade, resultados possíveis, seleção comprada e âmbito do
jogo. Uma soma de probabilidades aparentemente favorável não basta.

| Modalidade | Polymarket | SX Bet | Âmbito exigido |
| --- | --- | --- | --- |
| Futebol | Proposição Yes/No de vitória de uma equipa ou empate | Tipo 1, proposição e respetivo complemento | 90 minutos e compensação |
| Hóquei no gelo | Vencedor do jogo completo com regras explícitas | Tipo 226 | Inclui prolongamento e desempate por penáltis |

No futebol, `No` numa pergunta sobre a vitória de uma equipa cobre o empate
e a vitória da outra equipa. O código representa os três resultados
possíveis e só junta seleções disjuntas que, em conjunto, os cobrem todos.
Por exemplo, Poly `No` à vitória da equipa A pode combinar com SX “A vence”.
Poly “A vence” com SX “B vence” deixaria o empate descoberto e é recusado.
O mercado de empate também tem o seu complemento próprio.

As mensagens conservam os rótulos oficiais e acrescentam o significado da
seleção: “A não vence (inclui empate) [No]”. Os vários mercados do mesmo jogo
não são contados como jogos diferentes. No máximo uma operação virtual por
jogo de futebol ou hóquei pode consumir banca em cada ciclo de reset.

## Limite da margem apresentada

O certificado demonstra complementaridade **quando os dois mercados resolvem
normalmente**. Um início previsto depois do prazo de cancelamento Poly é
recusado, incluindo quando apenas o horário SX ultrapassa esse prazo. As
regras excecionais das plataformas continuam diferentes:

- Em futebol, os prazos de adiamento e as regras para abandono ou resultados
  administrativos não são iguais. Uma resolução binária por si só não prova
  que houve um resultado normal.
- No hóquei, a SX pode anular e devolver a aposta, enquanto determinados
  mercados Poly resolvem 50–50. Receber metade do valor nominal de uma ação
  não equivale a recuperar o seu preço de compra.

A simulação não apresenta estas margens como lucro garantido. Regras não
reconhecidas ou alteradas, anulações, resultados fracionários e resultados
contraditórios exigem revisão. A liquidação automática exige as duas fontes
oficiais e a confirmação SX de que o jogo terminou (`fixture.status = 3`).
Cancelado, adiado, interrompido ou abandonado passa a revisão; estados em
curso, desconhecidos ou sem cobertura ficam pendentes. Uma operação que
passou a revisão não volta a ser liquidada automaticamente por uma mudança
posterior do estado.

O verificador de regras reconhece os modelos de descrição efetivamente
inspecionados nas APIs oficiais em 27/11/2026. Um novo modelo de descrição
fica excluído até ser analisado, mesmo que mencione os mesmos participantes.

## Pesquisa e cobertura

A descoberta Gamma usa as categorias oficiais de futebol e hóquei, pagina
com limites e distribui as séries por modalidade. Mantém as séries que já
produziram correspondências e roda as vagas de exploração, incluindo quando
há mais modalidades do que vagas. Assim, o grande número de ligas de futebol
não impede que outras modalidades sejam consultadas.

Os novos contratos usam uma janela local de início entre há 8 horas e os
próximos 7 dias. A consulta Gamma usa `end_date_min`/`end_date_max` com margem
de 48 horas; `startDate` não é usado como horário de início do jogo.
Os campos `gameStartTime` e `gameTime` continuam a ser verificados localmente.

O monitor consulta por padrão até 30 páginas SX (100 mercados por página),
incluindo o tipo 1. `MONITOR_SX_PAGES` permite entre 1 e 50 páginas.
Os orçamentos Gamma, limites de pedidos e recuo após HTTP 429 continuam
ativos. Falhas de páginas não substituem o catálogo em uso por uma resposta
parcial apresentada como completa. Os truncamentos por orçamento são
registados explicitamente.

Os logs de descoberta e correspondência passam a incluir contagens por
modalidade. Os nomes `DISCOVERY_V15` e `MATCHING_V15` são mantidos para
compatibilidade com a observabilidade existente. `CATALOGO` diferencia
jogos e direções de comparação.

Os limites padrão dos feeds passam para 256 mercados SX e 512 resultados
Poly, para cobrir a amostra observada de 177/354 sem omissões. Os máximos
locais continuam a ser 512 por fonte; catálogos maiores usam a rotação e
declaram os identificadores que ficaram de fora. A fila de sinais mantém
no máximo 512 identificadores de comparações, sem guardar cotações antigas.

Mais jogos exigem acompanhar a fila: a quantidade descoberta não equivale
ao número de mercados subscritos em simultâneo nem garante uma reação
instantânea. Mantêm-se os feeds e a rotação V15, até três pares consultados
em paralelo e o limite de dez arranques de pares por segundo por padrão.

## Antes e durante o jogo

As novas modalidades atravessam a mesma confirmação de preços, custos,
liquidez, reserva e frescura já usada nas restantes modalidades.
Em direto, o certificado acompanha o candidato em todas as etapas e é
revalidado contra as duas fontes. Mantêm-se os atrasos oficiais das
plataformas. No futebol, exige-se uma indicação reconhecida de primeira
parte, segunda parte ou intervalo. Períodos desconhecidos, prolongamento
e penáltis não permitem abrir operações de um contrato de 90 minutos.

A operação continua a presumir os dois preenchimentos na simulação.
Não demonstra que duas ordens reais seriam executadas ao mesmo preço ou
sem que uma delas falhasse. O ROI mínimo, a reserva por carteira, a exposição
máxima e as taxas oficiais V14/V15 permanecem em vigor.

## Persistência e validação

O schema SQLite passa de 16 para 17 com backup e migração aditiva. Acrescenta
o certificado congelado e o identificador do jogo. A restrição de uma
operação por jogo é verificada dentro da transação que debita as duas
carteiras e também por um índice único. Operações arquivadas por um futuro
reset explícito não bloqueiam o novo ciclo; repetir o pedido de reset já
aplicado continua sem efeito.

Os testes novos cobrem as três situações de futebol, complementos Yes/No,
troca de ordem dos participantes, prolongamento, campos adulterados,
paginação e rotação, preservação de identidade em direto, resultados
oficiais, persistência da revisão e duas tentativas concorrentes no mesmo
jogo. A migração é testada com saldos diferentes dos iniciais, operações,
notificações e arquivo de reset já existentes. O workflow mantém as
regressões V12 a V15.

## Fontes de integração

- [Tipos de mercado SX](https://docs.sx.bet/api-reference/market-types)
- [Regras gerais SX](https://help.sx.bet/en/articles/3613418-general-betting-rules)
- [Estados de jogos SX](https://docs.sx.bet/api-reference/fixture-statuses)
- [Consulta de estado SX](https://docs.sx.bet/api-reference/get-fixture-status)
- [Listagem e filtros de eventos Gamma](https://docs.polymarket.com/api-reference/events/list-events)
- [Detalhes de mercados Polymarket](https://docs.polymarket.com/market-data/market-details)
- [Resolução Polymarket](https://docs.polymarket.com/concepts/resolution)

Regras concretas inspecionadas: mercados Gamma `7202037`, `7202038`
(futebol), `6474596` (NHL) e `7360020` (Liiga).
Custos, feeds e diagnóstico anteriores: [V15](README_V15.md).

## V16.0.1 — prioridade após medir o catálogo maior

A primeira implantação encontrou 113 jogos e 370 direções. Todos couberam
nos feeds, mas tratar os sinais pela mesma ordem elevou a fila para cerca
de 20 segundos. A V16.0.1 dá prioridade aos jogos na janela de direto e,
de seguida, aos que começam nas próximas duas horas. Jogos declarados
terminados ou sem horário válido ficam em fundo, assim como os já iniciados
que não permitem direto na SX.
Os horários servem apenas para ordenar consultas: não confirmam direto
nem permitem dispensar qualquer regra de entrada.

As provas LIVE prontas conservam a prioridade que já tinham. A quota de
rotação continua ativa e visita o catálogo em ordem circular, mesmo que
os mercados de fundo recebam sinais constantes. O limite de pedidos,
os tempos de frescura e os timestamps originais não são alterados.

Os logs mantêm a latência global e acrescentam `priority_decisions` para
`active_window`, `starts_soon` e `background`, com contagens, mediana e p95
da fila e da decisão. As expirações também são separadas por prioridade.
Uma melhoria no grupo urgente não deve ser apresentada como redução da
latência de todo o catálogo: as consultas de fundo terão uma cadência menor.
