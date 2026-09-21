# X-BRAY — Raio-X do Macro Brasileiro

Dashboard interativo de indicadores econômicos brasileiros com dados do BCB, ANBIMA e Tesouro Direto.

## Indicadores

| Seção | Fonte | Séries |
|-------|-------|--------|
| Taxas de Juros | BCB SGS | Selic Meta, Selic Efetiva, CDI, TR |
| Inflação | BCB SGS | IPCA, IPCA 12m, INPC, IGP-M, IGP-DI |
| IPCA Decomposição | BCB SGS | 8 grupos, 4 naturezas, 3 core, 4 preços |
| Atividade Econômica | BCB SGS | PIB, IBC-Br, Desemprego PNAD |
| Fiscal | BCB SGS | DBGG/PIB, DLSP/PIB, resultado primário, juros nominais e resultado nominal em 12 meses |
| Câmbio | BCB SGS | PTAX Compra/Venda USD, EUR/BRL |
| Títulos Públicos | ANBIMA XLS | IMA Histórico |
| Tesouro Direto | Tesouro Transparente | Prefixado e IPCA+ |
| Expectativas FOCUS | BCB OData | IPCA, Selic, PIB, Câmbio, IGP-M |
| Complementares | BCB SGS | Reservas, IC-Br, agregados monetários M0/M1/M2 |

Na interface, Complementares exibe M0, M1 e M2 sem duplicar a base monetária. A série legada Base Monetária permanece disponível em `/api/complementares/base-monetaria` para compatibilidade da API.

A apresentação de resultado primário foi migrada de Atividade Econômica para `/fiscal`. A série SGS legada `resultado_primario=4639` permanece no catálogo, no armazenamento e em `/api/atividade` exclusivamente para compatibilidade da API; a página Fiscal usa a série auditável 5793 em 12 meses como percentual do PIB.

## Stack

- **Backend:** Python + FastAPI + httpx + APScheduler
- **Frontend:** React + TypeScript + Vite + Recharts
- **Fontes de dados:** BCB SGS API, BCB FOCUS OData, ANBIMA XLS, Tesouro Direto

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

O estado cache-only das tarefas de atualização fica em `/api/refresh-status`. Fora de `XBRAY_ENV=local` ou `test`, todos os POSTs de refresh exigem `Authorization: Bearer` com o segredo servidor `XBRAY_REFRESH_TOKEN`; esse segredo nunca é enviado ao frontend.

A liberação para produção permanece bloqueada até `XBRAY_REFRESH_TOKEN` estar configurado no backend e no secret do workflow, com aprovação explícita. O workflow produtivo existente não é alterado automaticamente.

## Licença

MIT
