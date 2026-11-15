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
- Teste integral com livros sintéticos e simulação sem APIs ou apostas.

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

O Telegram só envia a mensagem **🧪 PAPER V12 · NÃO REAL** quando houver
uma operação simulada que ultrapasse os filtros de ROI e liquidez.
Os relatórios de seis horas também passam a mostrar saldos virtuais.

Para consultar saldos na consola do próprio serviço:

    python paper_v12.py --summary

Para bloquear um evento atípico e exigir revisão:

    python paper_v12.py --flag-exception 1 --reason "desistencia antes de completar jogo"

Para liquidar manualmente APENAS depois de confirmar resultado e regras
**nas duas plataformas**:

    python paper_v12.py --settle-id 1 --winner poly --verified-both-rules

`--winner sx` está igualmente disponível. Não existe liquidação
automática nem tratamento automático de eventos void, empate ou desistência.

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
