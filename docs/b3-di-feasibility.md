# Viabilidade técnica: boletim B3 para curva DI

**Conclusão (20/10/2026): compatibilidade de layout, viabilidade de dados ainda condicional.** O parser atual procura `AdjstdQtTax`, campo que o layout oficial associa à cotação de ajuste para contratos futuros/opções. Isso torna o boletim tecnicamente promissor para DI, mas não prova que o campo represente uma taxa DI anual em `% a.a.`, nem confirma cobertura, histórico acessível ou permissão de uso. Não há mudança de coleta ou runtime nesta avaliação.

## Evidência do layout e correspondência atual

Fonte examinada: [Catálogo de mensagens B3, versão 1.3](https://www.b3.com.br/data/files/16/70/29/9C/6219D710C8F297D7AC094EA8/Catalogo_precos_v1.3.pdf), datado de 07/10/2021 e ainda associado pela [página de layouts dos boletins](https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/historico/boletins-diarios/pesquisa-por-pregao/layout-dos-arquivos/). A versão consultada é evidência do documento, não confirmação de que o schema segue vigente em 2026.

| B3 (PDF) | Evidência | Uso no parser `backend/datafetchers/b3_di.py` | Avaliação |
|---|---|---|---|
| `TradDt/Dt` | p. 30–33; data de negociação obrigatória, formato ISO | Lê em `PricRpt/TradDt/Dt` e salva como `trade_date` | Compatível em campo e formato declarado. |
| `SctyId/TckrSymb` | p. 30–33; ticker obrigatório | Lê em `PricRpt/SctyId/TckrSymb`, filtra prefixo `DI1` e decodifica vencimento | Compatível em campo; calendário e regra de vencimento usados pelo decoder não foram validados contra especificação oficial. |
| `FinInstrmAttrbts/AdjstdQtTax` | p. 33, item 5.22; opcional `[0..1]`, descrito como “Cotação ajuste (fut) e opc c/ ajuste (em taxa)” | Converte texto em `float` e expõe como `rate` | Melhor correspondência encontrada para taxa de ajuste DI, mas o catálogo não define aqui unidade `% a.a.` ou equivalência a uma taxa DI. Campo opcional: ausência não é erro de schema. |
| `FinInstrmAttrbts/AdjstdQt` | p. 33, item 5.21; opcional, ajuste de futuro/opção | Não usado | Não assumir que seja a mesma medida de `AdjstdQtTax`. |
| `FinInstrmAttrbts/AdjstdQtStin` | p. 33, item 5.23; opcional, situação do ajuste do dia | Não usado | Os códigos/situações precisam de definição oficial antes de interpretar ou filtrar. |
| `FinInstrmAttrbts/PrvsAdjstdQtTax` | p. 33, item 5.25; ajuste do dia anterior | Não usado | Não é substituto silencioso para o ajuste do pregão corrente. |

O catálogo relaciona a mensagem `bvmf.217.01` ao schema XSD 1.1 usado por 086/186/187 (p. 3) e associa 187 a `PriceReport` (p. 9). O parser usa namespace `urn:bvmf.217.01.xsd` e percorre `PricRpt`. O formato simplificado R2 não marca `AdjstdQtTax` como obrigatório: presença no catálogo não garante presença em cada registro/arquivo.

O código atual ainda deduz maturidade de `DI1` com códigos mensais e primeiro dia útil calculado localmente. Isso não foi validado pelo catálogo examinado. Também não foi validada a aderência de `decode_maturity` a calendário oficial B3. Nenhuma conclusão sobre validade/cobertura de contratos deve depender dessa dedução sem confrontá-la com especificação oficial.

## Limite da conclusão

O documento estabelece compatibilidade nominal dos campos e associa `AdjstdQtTax` a cotação de ajuste “em taxa”. Não estabelece, na evidência consultada, unidade `% a.a.`, convenção de anualização, taxa utilizável diretamente na fórmula de Fisher, definição oficial de vencimento ou cobertura histórica para 24 datas. Não se deve inferir taxa a partir de `LastPric`/`TradAvrgPric`, converter PU sem convenção oficial de DU, nem tratar o nome do produto como prova de inadequação. **Veredito técnico:** promissor e compatível em estrutura, sujeito a validação documental e amostras autorizadas; ainda não habilita backfill.

## Próximas verificações (sem coleta automatizada)

1. Obter pelo canal permitido uma licença/autorização aplicável e amostras de boletins, sem contornar controles nem automatizar a página de pesquisa histórica.
2. Confirmar com a B3/provedor autorizado a versão vigente do layout, significado de `AdjstdQtTax`, unidade e convenção de taxa, códigos de situação de ajuste, calendário e regra de vencimento, cobertura de contratos DI e disponibilidade de ao menos 24 pregões distintos no período pretendido.
3. Em amostras autorizadas de pregões distintos, verificar presença e unicidade por data/ticker, unidade `% a.a.`, vencimento e compatibilidade da taxa com a curva DI e com o cutoff de cada observação. Registrar lacunas e revisões; não contar duplicatas ou revisões do mesmo pregão como datas distintas.
4. Só então comparar amostras com o método já documentado: alvos de 365 e 1.825 dias, tolerância inclusiva de ±90 dias, mesmo pregão e desempates existentes por distância, maturidade anterior e símbolo. Não alterar pareamento nem criar fallback silencioso nesta investigação.

A página oficial de [pesquisa por pregão](https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/historico/boletins-diarios/pesquisa-por-pregao/pesquisa-por-pregao/) declara que os dados são propriedade exclusiva da B3 e veda coleta automatizada, inclusive para fins de IA. Esta avaliação não é parecer jurídico nem afirma que todo canal/produto B3 seja proibido: acesso público, por si só, não comprova autorização para armazenar, processar ou exibir dados. O acesso manual a amostras também depende de autorização aplicável. Nenhum dado de mercado foi baixado para esta investigação; apenas o catálogo técnico público foi consultado.

**Estado:** documentação de viabilidade; parser, coleta, backfill e runtime não foram alterados. Até completar as verificações e obter autorização apropriada, o histórico retrospectivo DI permanece pendente.
