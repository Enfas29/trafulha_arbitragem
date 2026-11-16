# V12 — Paper Trading e WebSocket (sem apostas reais)

## Estado e limites

O serviço principal continua a executar `python monitor_v11.py` no Railway.
O V12 foi adicionado de forma **opcional**, com as funcionalidades novas
desligadas até os testes serem confirmados.

- Nenhuma função de execução real existe em `paper_v12.py` ou `sx_ws_v12.py`.
- O V12 simula **1.000 € de saldo por plataforma** e um máximo de **100 €
  no total por operação**, incluindo taxa simulada e buffers.
- Os dois débitos são feitos juntos numa transação SQLite; o mesmo confronto
  não pode criar duas operações.
- Exposição virtual total aberta limitada a 500 €.
- O cálculo percorre os níveis do livro, em vez de confiar apenas na melhor odd.
- Se o scanner ficar sem odds válidas durante 30 minutos de tentativas, envia uma mensagem de saúde no Telegram, com intervalo mínimo de uma hora entre avisos.
- A taxa Polymarket utiliza o parâmetro desportivo publicado de 5% aplicado
  a preço × (1 − preço) × shares, acrescido de reserva adversa de 1,25%
  em ambas as pernas. **Não substitui a verificação da taxa do contrato.**
- A SX Bet anuncia 0% de comissão em apostas simples; a reserva adicional
  cobre apenas incerteza de preço/execução, não uma comissão oficial.
- Os pagamentos previstos usam a taxa `EUR_PER_USD` configurada; o câmbio
  real poderá mudar antes da liquidação.
- O lucro é **condicional** a ambas as apostas hipoteticamente preencherem e
  os dois mercados liquidarem de forma compatível. Não é garantido.
- A liquidação de paper é **manual**, apenas após confirmar os resultados e
  as regras nas duas plataformas; anulações/desistências exigem revisão.
- Jogos live continuam analisados pelo scanner, mas as operações virtuais
  após o início ficam desativadas por defeito porque `liveEnabled` não é
  prova suficiente de que as regras/evento sejam compatíveis.

## Testes antes de ativar

No GitHub, abrir **Actions → Testar Robô V11 → Run workflow**.

O workflow inclui agora:
- Testes do motor de matching e catálogo V11.
- Testes do monitor, histórico e relatório de seis horas.
- Testes do PAPER V12 (limites, livro, carteiras, não duplicação, liquidação).
- Testes dos mecanismos WebSocket SX e Polymarket.
- Teste integral com livros sintéticos, saldos e mensagem de liquidação sem duplicação.
- Teste isolado de demonstração de débito, crédito e mensagens Telegram.

Não usar um resultado verde como prova de arbitragem real: são testes
locais, com livros sintéticos.

## Ativar o Paper V12 no Railway (só depois do teste verde)

No serviço `trafulha_arbitragem`, **Variables**:

    PAPER_V12_ENABLED=1
    PAPER_V12_DB=/data/paper_v12.sqlite3
    PAPER_V12_MIN_NET_ROI_PCT=0.5

O limiar anterior era **1,5%**. A nova variável
`PAPER_V12_MIN_NET_ROI_PCT=0.5` ativa um **piloto de 0,5% apenas
no Paper Trading**. É uma percentagem: não uses `0.005` nem `50`.

- Com a variável ausente, o padrão continua **1,5%**.
- A faixa permitida é de 0,5% a 15%; valores inválidos causam erro no arranque.
- Mantemos 100 € virtuais no máximo por operação, 500 € de exposição aberta,
  1.000 € de saldo inicial por plataforma e sem apostas reais.
- As operações virtuais abaixo de 1,5% são identificadas no Telegram como
  **MARGEM BAIXA / EXPERIMENTAL**.
- O ROI é calculado depois de **taxas e reservas estimadas**, mas não prova
  execução ou liquidação equivalente nos dois mercados.
- **Futebol 1X2 e hóquei ficam excluídos** da execução virtual automática;
  a auditoria de odds desses desportos é independente do motor de arbitragem.
- Alterar a percentagem não altera operações já abertas nem reinicia carteiras.

Para verificar o novo limiar, consulta **Railway → Deploy Logs** e procura
`PAPER V12 ATIVO: ROI mínimo 0.50%`.

Em **Railway → Console**, confirma o funcionamento:

    python paper_v12.py --self-test
    python test_integration_v12.py
    python paper_v12.py --summary
    python paper_v12.py --list-open

`demonstracao_carteiras_v12.py` usa livros completamente fictícios; o monitor
normal consulta as odds reais REST e apenas regista operações virtuais se
encontrar uma oportunidade que passe todos os filtros. Mesmo com 0,5%, pode
não abrir nenhuma operação durante várias horas ou dias.

Conservar as variáveis já configuradas:

    MONITOR_DB=/data/monitor_v11.sqlite3
    EUR_PER_USD=valor vigente de 1 USD em euros
    SX_API_KEY=chave apenas de leitura
    TELEGRAM_TOKEN=...
    TELEGRAM_CHAT_ID=...

Ao fazer deploy, a base nova é criada com 1.000 € virtuais na Polymarket
e 1.000 € virtuais na SX Bet. Não apagues `paper_v12.sqlite3`, pois isso
reiniciaria artificialmente os saldos.

O Telegram só envia uma mensagem **🧪 APOSTA VIRTUAL** quando uma
operação simulada ultrapassar os filtros de ROI e liquidez. A mensagem
mostra **equipa, odd, valor debitado e saldo disponível de cada carteira
logo após a abertura**. Não altera as regras de admissão de oportunidades.
Os relatórios de seis horas mostram também saldos livres, valor reservado
em operações abertas e resultado realizado.

Para consultar saldos na consola do próprio serviço:

    python paper_v12.py --summary

Para bloquear um evento atípico e exigir revisão:

    python paper_v12.py --flag-exception 1 --reason "desistencia antes de completar jogo"

Para listar os IDs das operações abertas:

    python paper_v12.py --list-open

Para liquidar manualmente APENAS depois de confirmar resultado e regras
**nas duas plataformas**:

    python paper_v12.py --settle-id 1 --winner poly --verified-both-rules

A liquidação devolve o prémio virtual **à carteira da plataforma vencedora**
(e não à outra); a outra aposta fica perdida. Atualiza os dois saldos na base
persistente e gera **uma nova mensagem de resultado** no Telegram, com a equipa
vencedora, montante creditado, lucro/prejuízo do conjunto e saldos atualizados.
O `--winner sx` serve para o caso oposto.

Se o Telegram falhar após a liquidação, os saldos continuam corretos no
SQLite; para tentar novamente **sem creditar outra vez**:

    python paper_v12.py --notify-settlement-id 1

A notificação usa uma tabela de idempotência para não repetir o aviso
quando tiver sido confirmadamente enviado.

⚠️ **A verificação dos resultados ainda é manual.** O código não sabe
automaticamente se a partida terminou, se foi interrompida ou como as duas
bolsas a liquidaram. Nunca será enviada uma mensagem de vitória confirmada
só porque uma odd baixou ou porque a hora prevista terminou.

`--winner sx` está igualmente disponível. Não existe liquidação
automática nem tratamento automático de eventos void, empate ou desistência.

### Ver duas mensagens de demonstração sem esperar por arbitragem

Na **Console do Railway**:

    python demonstracao_carteiras_v12.py --send-telegram

Isto cria uma operação e uma liquidação com dados **inteiramente fictícios**
num SQLite **temporário**, gera duas mensagens (abertura e resultado)
rotuladas como DEMONSTRAÇÃO e envia ambas ao Telegram.

**Não acede a mercados reais, não gasta dinheiro real, não modifica
`/data/paper_v12.sqlite3`, nem mexe nas carteiras virtuais verdadeiras
do monitor.** Serve apenas para comprovar visualmente a movimentação.

Para executar somente testes locais, sem mandar nada ao Telegram:

    python demonstracao_carteiras_v12.py --self-test


## Aceleração por WebSocket — opcional

A SX Bet V3 expõe canais `orderbook_v3:{marketHash}` e a
Polymarket dispõe de um canal `market` por token. Os adaptadores
`sx_ws_v12.py` e `poly_ws_v12.py` dão apenas um sinal para refazer
a leitura REST; **não usam os preços recebidos pelo WebSocket para
simular fills e não executam apostas**.

    SX_WS_V12_ENABLED=1
    POLY_WS_V12_ENABLED=1

    MONITOR_OUTAGE_SECONDS=1800
    MONITOR_OUTAGE_REPEAT_SECONDS=3600

Ambos são **experimentais** e estão desligados por defeito.
Testar primeiro cada integração isoladamente no Railway. Em caso de
falha de subscrição, o REST continua ativo. São acompanhados no máximo
12 mercados SX e 24 tokens Polymarket simultaneamente, enquanto os
restantes continuam em rotação REST. A frequência depende de rede,
limites da API e créditos do Railway: não é execução instantânea.

## Mensagens antigas «PAPER TRADING V10.0 ONLINE»

O V12 não envia essa mensagem. Se continuar a recebê-la, há outro processo
antigo ativo em algum serviço/servidor/conta que usa o mesmo bot Telegram.
Localizar o serviço antigo (por exemplo Render, outra app Railway, GitHub
Actions recorrente ou PC local) e parar **apenas esse processo**. O V12 não
consegue desligar processos externos de forma segura por si.

## Segurança das credenciais

Foi corrigido um problema anterior: a chave da SX Bet estava num
cabeçalho HTTP global e podia acompanhar pedidos à Polymarket.
O novo `get_json` inclui `x-sx-api-key` exclusivamente quando o URL
é de `https://api.sx.bet/`. O teste `test_security_v12.py` verifica isso
sem fazer pedidos de rede.

**Boa prática:** revogar a antiga chave SX de apenas leitura e criar outra,
atualizando os Secrets do GitHub e as Variables do Railway. Não enviar
a chave em mensagens, screenshots ou commits. Não colocar chaves privadas
de carteira no projeto.


## Testar UMA aposta hipotética com odds reais, mesmo sem margem positiva

No Railway → serviço `trafulha_arbitragem` → **Console**, executar:

    python teste_odds_reais_v12.py

O comando recolhe novos catálogos Polymarket e SX Bet, encontra um confronto
com resultados opostos e consulta os livros de ordens REST das duas plataformas.
Se houver liquidez suficiente e as leituras estiverem próximas no tempo,
envia **uma única mensagem** para Telegram com:

- Dois blocos separados: **1️⃣ POLYMARKET** e **2️⃣ SX BET**;
  cada bloco mostra claramente equipa, odd da API e valor virtual;
- Odds arredondadas a **duas casas e com vírgula decimal** para leitura,
  por exemplo 1,09. A amostra SQLite mantém o valor numérico original;
- Valor total do ensaio até 10 €; resultado condicional, incluindo perdas;
- Saldo atual das **duas carteiras virtuais**, apenas lido na base V12 sem
  efetuar débitos. Se a base ainda não existir, indica saldo indisponível;
- Aviso **MARGEM NEGATIVA — NÃO APOSTAR**, quando aplicável;
- Horas de início de cada plataforma quando diferirem, e hora da leitura;
- Texto simples na consola e versão Telegram com títulos a negrito.

O ensaio NÃO depende do limiar de +1,5% da carteira principal e **não coloca
ordens**, não altera os 1.000 € virtuais de cada plataforma e não apresenta
fills fictícios como realizados. É uma cotação de aposta de ensaio, não uma
transação executada. Guarda a amostra separada no volume persistente:

    /data/odds_test_v12.sqlite3

Se os jogos emparelhados não tiverem livros suficientes, o programa comunica
que não encontrou amostra; não cria odds inventadas. É um comando único,
não um loop, não altera o scanner ativo.

Teste sem API/Telegram:

    python teste_odds_reais_v12.py --self-test

**Ao comparar com as páginas das plataformas:** as duas odds não precisam
de ser iguais porque são preços para resultados **opostos**. E a melhor
odd pode diferir da odd efetiva ao consumir vários níveis de liquidez.
As odds observadas são um instante e podem mudar no site segundos depois.

## V12: prioridade WebSocket e cache curta de eventos (sem cache de odds)

A atualização liga a fila `ws_priority_v12.py` ao scanner contínuo:

- Cada notificação WS guarda apenas `id de mercado/token` e um instante
  **monotónico**, nunca o preço.
- Eventos repetidos do mesmo mercado são coalescidos e têm validade
  limitada a 20 segundos. Sinais antigos são descartados.
- Até **4 direções sinalizadas** são verificadas antes dos restantes
  candidatos em cada passagem. Há sempre posições reservadas para a
  rotação REST, para não deixar os outros mercados esquecidos.
- Ambas as plataformas são sempre consultadas novamente via REST; nenhuma
  odd em cache pode desencadear apostas virtuais.
- `WS_LATENCY` nos Deploy Logs mede os intervalos
  `evento_ate_inicio_ms_max`, `REST_ms_max`,
  `evento_ate_validacao_ms_max` e `evento_ate_decisao_ms_max`.
  O evento medido é o **instante em que o robô recebe a mensagem WS**,
  não o instante original da alteração no mercado.
- `SCAN` continua a mostrar total de direções, preços, duração da
  passagem e quantas verificações foram acionadas por sinais WS.
- Variáveis opcionais: `MONITOR_WS_PRIORITY_MAX=4` (limite máximo de
  6) e `MONITOR_WS_EVENT_TTL_SECONDS=20` (5 a 60 segundos).
  **Não é necessário alterá-las** para ativar esta melhoria.
- As subscrições continuam limitadas aos mercados observados nas fontes;
  o conjunto inteiro não está garantidamente monitorizado por WS.
- Sem execução real, sem simulação automática em jogo ao vivo por defeito,
  e sem promessas de execução em menos de dez segundos.

No Railway, após um deployment bem-sucedido, consultar os logs:
`WS PRIORITY:`, `WS_LATENCY |` e `SCAN |`.

Autotestes offline (não requerem tokens):

    python ws_priority_v12.py
    python poly_ws_v12.py
    python sx_ws_v12.py
    python monitor_v11.py --self-test

**Não abrir uma segunda cópia do monitor no Console se o serviço já estiver
a correr**; isso duplica pedidos às APIs e pode partilhar a mesma SQLite.

## Reação rápida às mudanças de odds — testes de latência

O `monitor_v11.py` foi ajustado para reduzir a latência de **deteção**:
- O monitor já reage a alterações WebSocket em mercados subscritos, mas
  antes impunha uma espera mínima artificial de 8 segundos após sinais.
  Agora verifica os eventos em intervalos de ~100 ms na espera e mantém
  um intervalo mínimo de 2 segundos entre ciclos acionados por WebSocket.
- `MONITOR_WS_REACT_GAP_SECONDS` permite configurar o intervalo de
  proteção (de 1 a 10 segundos, por defeito `2.0`). Não é necessário
  adicionar esta variável para beneficiar da alteração.
- As linhas `SCAN` mostram `duracao_ms`,
  `maior_leitura_ms` e `sinais_WS_verificados`, para medir
  o tempo consumido pela análise e por pedidos REST.
- Isto só acelera os **mercados subscritos**. Atualmente, a Polymarket
  subscreve até 24 tokens e a SX Bet até 12 mercados, dos candidatos
  priorizados. O monitor continua a percorrer os restantes em rotação;
  NÃO existe garantia de reação inferior a 10 segundos em todos os jogos.
- A leitura REST continua obrigatória para confirmar os livros; não
  assumir que a odd do evento WebSocket ainda é executável.
- **Execução real permanece desativada**. Em jogos iniciados, o V12
  faz diagnóstico quando autorizado pelas condições do feed mas a abertura
  de operações fictícias em modo `LIVE_NAO_CONFIRMADO` continua
  desativada por defeito. Não ligar `PAPER_V12_LIVE_UNVERIFIED`
  sem verificar estado do jogo e equivalência de liquidação.
- Não usar lucro bruto estimado como garantia: uma execução de duas
  pernas pode falhar ou preencher a preços diferentes.

Para verificar a instalação:

    python monitor_v11.py --self-test

Depois, no Railway **Deploy Logs**, procurar `WS FAST` e `SCAN`.
O alvo é medir consistentemente tempos de deteção, não prometer
fills abaixo de 10 segundos sem API de ordens, autenticação e simulação
de execução validadas.

## Auditoria de vários jogos com odds reais — incluindo futebol

Em **Railway → trafulha_arbitragem → Console**, depois de
confirmar o último teste verde no GitHub Actions:

    python auditoria_jogos_v12.py --sport soccer --count 3

Para **repetir exatamente** Manchester City vs Fulham, mesmo que já
tenha sido enviado noutra execução:

    python auditoria_jogos_v12.py --sport soccer --match "Manchester City vs Fulham" --recheck --count 1

Para investigar especificamente uma **diferença entre o site e o robô**,
executar com `--explain-prices`:

    python auditoria_jogos_v12.py --sport soccer --match "Chelsea vs Leeds United" --recheck --count 1 --explain-prices

Se o relatório de Newcastle vs Arsenal marcar "empate não identificado":

    python auditoria_jogos_v12.py --sport soccer --match "Newcastle United vs Arsenal" --recheck --count 1 --explain-prices

A auditoria reconhece também `groupItemTitle` com
`Draw (Equipa A vs Equipa B)`, mas apenas quando ambos os nomes coincidem
com o confronto. Se ainda faltar empate, imprime na **consola**
`GAMMA: PORQUE FALTA O EMPATE?` com tipo, rótulo, pergunta, mercado
e número de tokens, para rever a causa sem inventar odds.


O modo de diagnóstico mostra os identificadores de token e de mercado,
as horas de leitura UTC e:
- **Polymarket:** melhor ASK (custo de comprar agora), BID (preço a que
  poderias vender), último negócio, preço Gamma e exibição do site
  **estimada** a partir do livro. Segundo o Help Center, a Polymarket
  usa o midpoint entre BID e ASK ou o último negócio em spreads > 10¢.
- **SX Bet:** odd efetiva taker do livro e odd do endpoint público
  `/orders/odds/best`, quando devolvida e interpretável.
- A auditoria só aceita SX em 1X2 quando encontra `Equipa / Not Equipa`
  ou `Tie / Not Tie`. Não aceita pares `Equipa A / Equipa B`,
  handicaps, mercados 2-way ou resultados contraditórios.
- A SX Bet usa **type=1** para o 1X2 desdobrado em contratos
  `Equipa/Not Equipa` e `Tie/Not Tie`. **type=52 não é o 1X2**:
  o filtro anterior excluía as três linhas corretas e devolvia zero.
  A consulta agora filtra `type=1` e apresenta contadores de rejeição
  (desporto, horário, estado, equipas, resultado) se não houver confrontos.
  O scanner contínuo V12 mantém futebol EXCLUÍDO das operações virtuais,
  até as regras de 1X2 serem validadas.
- Os tokens Poly com classificações ambíguas são excluídos; nunca
  escolhe uma odd arbitrária para casa, empate ou fora.
- A publicação para o Telegram em modo só texto **omite `parse_mode`**
  (em vez de enviar `null`) e divide mensagens longas em partes
  inferiores a 4096 caracteres, conforme a Bot API oficial. Só confirmar
  envio quando a API responder com sucesso. Se persistir HTTP 400,
  investigar o código de erro antes de assumir uma causa.

O modo detalhado acrescenta texto técnico ao Telegram e pode ser extenso.
É uma auditoria pontual, não modifica as carteiras virtuais e não
ativa apostas. Evitar comparar com screenshots obtidas noutro minuto:
as cotações variam entre instantes.

Referências:
- https://help.polymarket.com/en/articles/13364488-how-are-prices-calculated
- https://docs.polymarket.com/market-data/prices-order-books
- https://blog.sx.bet/developers/tutorials/fetch-live-odds-5-minutes/
- https://help.sx.bet/en/articles/5090654-soccer-betting-rules

O mapeamento da auditoria é deliberadamente conservador: se uma pergunta
da Gamma sobre "draw" contiver o nome da equipa visitante, isso **não**
autoriza atribuir o preço do empate à equipa. Se os rótulos contradisserem
a pergunta, o mercado é descartado e assinalado como não identificado.
Para comparar com a página da Polymarket, usar os mesmos três desfechos
(1 / X / 2) e uma hora de leitura próxima. Não confiar em arbitragem
de futebol enquanto os três preços e regras não estiverem verificados.

O comando tenta encontrar até 3 jogos de FUTEBOL diferentes com mercados
nas duas plataformas, e envia uma mensagem por jogo com os preços de compra
dos desfechos **vitória casa / empate / vitória fora**, quando esses
desfechos são identificáveis em cada livro. Mostra "sem cotação" quando
não há preço: nunca inventa um resultado nem uma odd.

Para testar outras modalidades (apostas opostas e até 10 € hipotéticos):

    python auditoria_jogos_v12.py --sport tennis --count 3
    python auditoria_jogos_v12.py --sport basketball --count 3
    python auditoria_jogos_v12.py --sport american_football --count 3

- O teste é **pontual**, não modifica `PAPER_V12_DB`, não realiza
  apostas reais, nem debita carteiras virtuais.
- Faz comparações de mercados com **nomes das duas equipas e hora próxima**,
  não assume que mercados com nomes parecidos têm regras iguais.
- Em futebol, **NÃO CALCULA ARBITRAGEM de duas pernas**, porque o empate
  é um terceiro resultado e "Não equipa A" não é equivalente a "Equipa B".
  Nem todos os 1X2 são executáveis pela mesma seleção em ambas as plataformas.
- Nos outros desportos, mantém os valores hipotéticos/odds do
  `teste_odds_reais_v12.py` (inclui margens negativas).
- O histórico independente `/data/odds_auditoria_v12.sqlite3` marca
  jogos já enviados para preferir confrontos diferentes noutras execuções.
- Se não houver jogos cruzados/odds, o comando diz isso na consola e **não
  manda dados fictícios**.
- `--count` aceita entre 1 e 5 para limitar o número de pedidos e mensagens.

Teste sem APIs nem Telegram:

    python auditoria_jogos_v12.py --self-test

Os horários, odds e regras de liquidação têm de ser comparados em cada
plataforma; odds são instantâneas e podem mudar antes de abrires as páginas.

## Por validar antes de qualquer execução com dinheiro real

1. Regras de liquidação em cenários excecionais: desistências no ténis,
   partidas adiadas/suspensas, empates e limites de tempo na NFL/NBA.
2. Taxas efetivas por contrato e preços executáveis no instante da ordem.
3. Latência, cancelamentos e rejeições. Não há atomicidade entre plataformas:
   uma perna pode ser aceite e a outra recusada.
4. Câmbio USD/USDC para EUR e custos de conversão/transferência.
5. Períodos suficientemente longos de simulação, com comparação de sinais,
   fills plausíveis, exceções e perdas, sem escolher apenas os melhores casos.

Não armazenar chaves privadas de carteira no Railway para esta versão.

## Fontes para revisão manual

- Polymarket — taxas: https://help.polymarket.com/en/articles/13364478-trading-fees
- SX Bet — API/WebSocket: https://blog.sx.bet/developers/tutorials/building-on-sx-bet-api-websocket-mcp/
- SX Bet — regras de ténis: https://help.sx.bet/en/articles/5090684-tennis-betting-rules
- SX Bet — regras gerais: https://help.sx.bet/en/articles/3613418-general-betting-rules

Uma diferença entre a odd apresentada no browser e a odd do Telegram não
prova necessariamente erro: são observações em instantes diferentes. O teste
indica que o valor vem do livro REST; confirmar ambos no mesmo instante
antes de confiar no preço. A odd média para consumir vários níveis de
liquidez pode diferir da melhor odd exibida.
