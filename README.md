# X-BRAY — Raio-X do Macro Brasileiro

Dashboard interativo de indicadores econômicos brasileiros com dados do BCB, IBGE SIDRA, ANBIMA e Tesouro Direto.

## Indicadores

| Seção | Fonte | Séries |
|-------|-------|--------|
| Taxas de Juros | BCB SGS | Selic Meta, Selic Efetiva, CDI, TR |
| Inflação | BCB SGS | IPCA, IPCA 12m, INPC, IGP-M, IGP-DI |
| IPCA Decomposição | BCB SGS | 8 grupos, 4 naturezas, 3 core, 4 preços |
| Atividade Econômica | BCB SGS + IBGE SIDRA | PIB, IBC-Br, desemprego PNAD, indústria, varejo ampliado, serviços, massa de rendimento real e subutilização |
| Fiscal | BCB SGS | DBGG/PIB, DLSP/PIB, resultado primário, juros nominais e resultado nominal em 12 meses |
| Crédito e famílias | BCB SGS | Inadimplência PF, endividamento, comprometimento de renda, ICC PF e taxa média PF |
| Câmbio | BCB SGS | PTAX Compra/Venda USD, EUR/BRL |
| Setor Externo | BCB SGS | Conta corrente/PIB, IDP/PIB, IDP mensal, balança comercial, investimento em carteira e reservas internacionais |
| Títulos Públicos | ANBIMA XLS | IMA Histórico |
| Tesouro Direto | Tesouro Transparente | Prefixado e IPCA+ |
| Expectativas FOCUS | BCB OData | IPCA, Selic, PIB, Câmbio, IGP-M |
| Complementares | BCB SGS | IC-Br e agregados monetários M0/M1/M2 |

Na interface, Complementares exibe IC-Br, M0, M1 e M2 sem duplicar a base monetária ou as reservas. A série legada Base Monetária permanece disponível em `/api/complementares/base-monetaria`, e reservas continuam disponíveis em `/api/complementares/reservas`, exclusivamente para compatibilidade da API. A apresentação de reservas foi migrada para `/setor-externo`.

A apresentação de resultado primário foi migrada de Atividade Econômica para `/fiscal`. A série SGS legada `resultado_primario=4639` permanece no catálogo, no armazenamento e em `/api/atividade` exclusivamente para compatibilidade da API; a página Fiscal usa a série auditável 5793 em 12 meses como percentual do PIB.

## Stack

- **Backend:** Python + FastAPI + httpx + APScheduler
- **Frontend:** React + TypeScript + Vite + Recharts
- **Fontes de dados:** BCB SGS API, BCB FOCUS OData, IBGE SIDRA, ANBIMA XLS, Tesouro Direto

## Setup

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Acessar http://localhost:5173

## Features

- Atualização diária automática (06:00 BRT)
- Refresh manual disponível somente no ambiente local de desenvolvimento
- Cache local com TTL de 23 horas
- Navegação por seções
- KPI cards com último valor de cada série
- Gráficos interativos com Recharts

## Qualidade e atualização dos dados

Os endpoints GET legados mantêm seus formatos originais. A qualidade complementar fica em `/api/data-quality/{dataset}`: `ok` indica todas as séries obrigatórias utilizáveis, `partial` mantém as séries válidas quando outra fonte falha e `unavailable` indica ausência total de dados utilizáveis. Séries vencidas pelo prazo de freshness continuam visíveis com aviso e data da última observação.

`GET /api/fiscal` lê exclusivamente o cache SGS e expõe status, indisponibilidades e metadados de código, unidade, frequência, freshness e `sign_convention` por série. Os códigos 5793 e 5727 preservam os valores brutos no armazenamento e têm o sinal invertido somente na resposta, para que superávit seja positivo; o código 5760 permanece positivo como custo de juros. `POST /api/fiscal/refresh` usa a mesma autenticação Bearer dos demais refreshes.

`GET /api/credito` lê exclusivamente o cache SGS e mantém cada série em seu próprio período de referência. O envelope informa unidade, período, deadline e freshness por série; valores anuais de ICC e taxa média PF são expostos sem nova anualização. `POST /api/credito/refresh` usa a autenticação Bearer compartilhada.

A página `/credito` apresenta cinco KPIs e compara comprometimento×inadimplência, Selic×taxa média PF e endividamento×comprometimento. Cada comparação seleciona a última observação válida de cada série no mês, preserva meses ausentes como lacunas desconectadas e mostra no tooltip as datas de referência individuais. O resumo compara os dois últimos valores válidos de cada série; a cadeia Selic→crédito→orçamento→inadimplência→consumo é descrita somente como mecanismo econômico geral, sem inferência causal.

`GET /api/setor-externo` lê exclusivamente o cache SGS e expõe as seis séries mensais com código, título oficial, unidade, período de referência, deadline e freshness. Conta corrente e IDP acumulados em 12 meses usam `% do PIB`; os fluxos mensais e o estoque de reservas usam US$ milhões. Os sinais publicados pelo BCB são preservados, inclusive valores negativos. `POST /api/setor-externo/refresh` usa a autenticação Bearer compartilhada. A série de reservas SGS 3546 passa a ser atualizada por Setor Externo, enquanto `/api/complementares` e `/api/complementares/reservas` continuam lendo o mesmo cache para compatibilidade.

A página `/setor-externo` apresenta cinco KPIs e quatro gráficos separados por unidade e frequência. O insight de cobertura divide o IDP acumulado em 12 meses pelo módulo do déficit em conta corrente somente quando a conta corrente é estritamente negativa e ambos os valores são finitos; razão maior ou igual a 1 indica cobertura, razão menor que 1 indica não cobertura. Superávit, zero ou dado ausente não geram conclusão. A PTAX permanece na página independente `/cambio`.

`GET /api/atividade/sidra` é estritamente cache-only e expõe cinco cortes nacionais versionados: PIM indústria (tabela 8888, variável 12607, classificação 544, categoria 129314), PMC varejo ampliado (8881/7170/11046/56736), PMS serviços (5906/7168/11046/56726), massa de rendimento real (6392/6293) e taxa de subutilização (6441/4118). O envelope retorna `ok` com os cinco conjuntos, `partial` com qualquer subconjunto utilizável e o erro tipado 503 `dataset_unavailable` quando o cache está vazio. `POST /api/atividade/sidra/refresh` reutiliza a autenticação Bearer compartilhada; falhas SIDRA não removem o conteúdo SGS de `/api/atividade`.

As observações SIDRA são append-only e preservam revisões por `available_at`, `first_seen_at` e `payload_hash`; consultas correntes e históricas não sobrescrevem vintages anteriores. Quando o provedor não informa a data oficial de publicação, `source_published_at` permanece nulo e `available_at` usa `first_seen_at`. Séries de trimestre móvel são apresentadas como `Trimestre móvel encerrado em ...`.

## Metodologia dos indicadores derivados

O motor em `backend/derived/` calcula resultados por funções puras com `as_of` UTC injetado. Cada entrada registra chave, valor bruto sem arredondamento, unidade, período de referência, fonte e `available_at`; entradas posteriores ao corte tornam o resultado explicitamente indisponível. A fase de derivados roda sequencialmente após as persistências utilizáveis do refresh, enquanto `GET /api/derivados` lê somente snapshots já persistidos e nunca consulta provedores.

O indicador `juro_real_ex_ante_1y` usa exclusivamente o conjunto Focus `ExpectativasMercadoInflacao12Meses`, filtrado literalmente por `Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0`, armazenado sob a chave distinta `IPCA_12M_S_BASE0`. Não há fallback para expectativas anuais do Focus. O `calculation_cutoff` ou `as_of` define o que já era conhecido, enquanto `reference_date` é a menor entre a última data elegível de negociação DI e a última `Data` elegível do Focus. A curva DI seleciona a maturidade-alvo em `reference_date + 365 dias`, aceita distância inclusiva de até 90 dias e desempata por menor distância, maturidade anterior e símbolo. O cálculo usa, sem arredondar as entradas, a fórmula de Fisher `((1 + DI/100) / (1 + IPCA/100) - 1) * 100`; qualquer arredondamento é apenas de apresentação.

As entradas só participam quando `available_at <= calculation_cutoff`. Na falta de timestamp oficial de publicação ou revisão, `first_seen_at` é usado de forma conservadora, sem backfill histórico em massa sem timestamps oficiais específicos de revisão. O percentil histórico considera snapshots imutáveis do mesmo método, conserva no máximo o snapshot mais recente conhecido por `reference_date`, usa janela de até dez anos e exige ao menos 24 datas elegíveis distintas. Com menos de 24 datas, o valor pode permanecer disponível, mas percentil e classificação são nulos, com `insufficient_history`. O endpoint cache-only `GET /api/derivados/juro-real-ex-ante-1y` expõe versão metodológica `1.0.0`, fórmula e proveniência das entradas, e não aciona provedores no navegador nem em GET; sem snapshot, retorna indisponibilidade tipada. Trata-se de uma medida derivada, não de previsão nem de estimativa causal.

Snapshots são append-only e identificados por indicador, `as_of`, versão metodológica e hash canônico das entradas. Repetir o mesmo cálculo é idempotente; revisão de entrada cria outra linha sem atualizar ou apagar a anterior. O payload preserva fórmula, versão, entradas e motivo de indisponibilidade e rejeita NaN, Infinity e timestamps sem fuso.

Os cinco indicadores adicionais também usam metodologia `1.0.0`, entradas elegíveis somente quando `available_at <= as_of` e snapshots independentes: `cobertura_externa` é `idp_12m_pib / abs(conta_corrente_12m_pib)`, em razão (a interface apresenta `razão × 100%`), apenas com conta corrente negativa e ambos os valores do mesmo mês; `variacao_saldo_primario_12m` é o saldo primário acumulado em 12 meses, em `% do PIB`, menos o valor do mesmo mês do ano anterior, em p.p.; `spread_credito_pf_selic` é a taxa média anual de crédito PF menos a última Selic-meta anual do mesmo mês, em p.p.; `distancia_focus_meta` é a mediana do Focus IPCA 12 meses menos o centro da meta vigente na data de referência, em p.p.; e `inclinacao_di_5y_1y` é a taxa DI do horizonte de cinco anos menos a de um ano, em p.p., na mesma data de negociação. Ausência de par mensal, conta corrente não negativa, meta não observada no corte ou curva sem contratos elegíveis produz indisponibilidade tipada, sem substituição silenciosa de período ou insumo.

A tabela versionada de metas usa a fonte histórica oficial do [BCB](https://www.bcb.gov.br/controleinflacao/historicometas): centros anuais de 4,50% em 2015–2018, 4,25% em 2019, 4,00% em 2020, 3,75% em 2021, 3,50% em 2022, 3,25% em 2023 e 3,00% em 2024; desde 2025-01-01, centro contínuo de 3,00% e banda de tolerância de ±1,50 p.p. Cada regra aplicável é observada no ledger local antes da captura de `as_of`; sem timestamp oficial de publicação, sua disponibilidade é o `first_seen_at` persistido, não a data efetiva da regra. Não há backfill histórico inferido. As curvas DI usam alvos de 365 e 1.825 dias, tolerância inclusiva de ±90 dias e desempate por distância, vencimento anterior e símbolo.

O refresh calcula os seis indicadores em sequência, após concluir as fontes e observar as regras de meta; todos recebem o mesmo cutoff UTC e ciclo. Uma falha de cálculo ou persistência em um indicador não impede as tentativas dos demais. `GET /api/derivados` retorna os snapshots disponíveis do cache, inclusive resultados indisponíveis individuais; nos cinco indicadores adicionais, `unavailable_reason` repete o código tipado de `reason` para explicitar o contrato sem remover o campo existente. Sem snapshots, o contrato é exatamente `{"status":"unavailable","data":{}}`. O GET não atualiza fontes nem observa regras. Regras de meta revisadas são selecionadas da revisão persistida mais recente que estava disponível no cutoff; a vigência efetiva continua distinta da disponibilidade local por `first_seen_at`. A página `/derivados` apresenta os cinco novos cartões separadamente, com valor, unidade, data-base, versão, proveniência e motivos de indisponibilidade; cobertura e saldo não constituem afirmações causais.

Percentis usam até dez anos encerrados em `as_of`, no mínimo 24 observações finitas e midrank empírico `100 * (count_less + 0.5 * count_equal) / n`. As faixas são `[0,20)`, `[20,40)`, `[40,60)`, `[60,80)` e `[80,100]`; arredondamento pertence somente à apresentação. Dados anteriores ao ledger sem `available_at` confiável não são reconstruídos para backtests.

## Macro Score Beta

O Macro Score `0.1.0-beta` é um snapshot mensal cache-only. `GET /api/macro-score` devolve o último snapshot de produção e `GET /api/macro-score/history?start=YYYY-MM&end=YYYY-MM` devolve o último snapshot de cada mês no intervalo. `as_of` é o cutoff UTC de informação; `calculated_at` é a conclusão do cálculo; `score_month` usa a data de `as_of` em `America/Sao_Paulo`. Revisões são append-only e histórico de produção começa no primeiro ciclo capturado após a implantação; fixtures golden são apenas testes e nunca criam linhas de produção.

O inventário versionado contém 18 componentes, organizados em seis dimensões com peso unitário e direção explícita. Cada componente considera somente vintages com `available_at <= as_of`, referência até o mês do score e freshness compatível com sua frequência; entradas ausentes, futuras ou vencidas permanecem ausentes, nunca são convertidas em zero. O histórico elegível é limitado aos 120 meses conhecidos no corte, exige 24 observações, winsoriza nos quantis lineares tipo 7 de 2% e 98%, calcula midrank empírico e inverte componentes `lower_is_better` por `100 - percentile`, sem arredondamento intermediário.

Uma dimensão só recebe score com pelo menos dois terços dos componentes válidos; o score geral exige cinco das seis dimensões e é a média simples das dimensões disponíveis. As classes são `[0,20) muito_fraco`, `[20,40) fraco`, `[40,60) neutro`, `[60,80) forte` e `[80,100] muito_forte`. O payload expõe valores, unidade, referência, disponibilidade, fórmula, direção, peso, percentil, score, motivo de ausência e proveniência dos inputs. O indicador é descritivo e não constitui previsão, recomendação ou afirmação causal.

O resumo determinístico é uma capability separada e permanece desativado por padrão: `XBRAY_ENABLE_MACRO_SUMMARY` deve continuar `false`. `GET /api/macro-score` informa `capabilities.macro_summary`; somente quando ela for `true` a interface solicita `GET /api/macro-score/summary`. Com a capability desligada, o endpoint retorna 404 `summary_disabled`; sem dois ciclos de produção equivalentes, retorna 409 `insufficient_equivalent_cycles`. O resumo compara os snapshots de meses distintos, apresenta os scores e variações persistidos e não atribui causalidade, prevê nem recomenda. Não habilitar a capability até haver dois ciclos reais capturados após XBR-601 e um novo OK explícito do usuário; fixtures e ciclos de teste não satisfazem esse requisito.

Proveniência oficial BCB SGS: [23079](https://api.bcb.gov.br/dados/serie/bcdata.sgs.23079/dados), [23080](https://api.bcb.gov.br/dados/serie/bcdata.sgs.23080/dados), [22885](https://api.bcb.gov.br/dados/serie/bcdata.sgs.22885/dados), [22707](https://api.bcb.gov.br/dados/serie/bcdata.sgs.22707/dados), [22905](https://api.bcb.gov.br/dados/serie/bcdata.sgs.22905/dados) e [3546](https://api.bcb.gov.br/dados/serie/bcdata.sgs.3546/dados). Proveniência oficial dos novos cortes: [IBGE SIDRA](https://sidra.ibge.gov.br/) e [API SIDRA](https://apisidra.ibge.gov.br/).

O estado cache-only das tarefas de atualização fica em `/api/refresh-status`. Fora de `XBRAY_ENV=local` ou `test`, todos os POSTs de refresh exigem `Authorization: Bearer` com o segredo servidor `XBRAY_REFRESH_TOKEN`; esse segredo nunca é enviado ao frontend.

A liberação para produção permanece bloqueada até `XBRAY_REFRESH_TOKEN` estar configurado no backend e no secret do workflow, com aprovação explícita. O workflow produtivo existente não é alterado automaticamente.

## Changelog

- `0.1.0-beta`: primeira metodologia versionada do Macro Score, com snapshots mensais imutáveis, cutoff sem look-ahead, cobertura explícita e histórico forward-captured.

## Licença

MIT
