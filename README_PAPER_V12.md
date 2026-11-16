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

## Auditoria de vários jogos com odds reais — incluindo futebol

Em **Railway → trafulha_arbitragem → Console**, depois de
confirmar o último teste verde no GitHub Actions:

    python auditoria_jogos_v12.py --sport soccer --count 3

Para **repetir exatamente** Manchester City vs Fulham, mesmo que já
tenha sido enviado noutra execução:

    python auditoria_jogos_v12.py --sport soccer --match "Manchester City vs Fulham" --recheck --count 1

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
