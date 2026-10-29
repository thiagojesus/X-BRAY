import { useRef } from 'react'
import { TimeSeriesChart, type SeriesConfig } from '../charts/TimeSeriesChart'
import type { CreditModality, CreditModalityKey, CreditResponse } from '../types/credit'
import { mergeCreditExposureHistory } from '../utils/creditExposureCharts'

type CreditMetadata = CreditResponse['metadata'][string]

type ModalityConfig = {
  readonly key: CreditModalityKey
  readonly code: number
  readonly name: string
  readonly color: string
}

const MODALITIES: readonly ModalityConfig[] = [
  { key: 'carteira_pf_cheque_especial', code: 20573, name: 'Cheque especial', color: '#80cbc4' },
  {
    key: 'carteira_pf_credito_pessoal_nao_consignado',
    code: 20574,
    name: 'Crédito pessoal não consignado',
    color: '#90caf9',
  },
  { key: 'carteira_pf_cartao_rotativo', code: 20587, name: 'Cartão rotativo', color: '#ce93d8' },
  { key: 'carteira_pf_cartao_parcelado', code: 20588, name: 'Cartão parcelado com juros', color: '#ffcc80' },
]

const STOCK_SERIES: readonly SeriesConfig[] = MODALITIES.map(item => ({
  key: item.key,
  name: item.name,
  color: item.color,
  format: 'brl_million',
}))

const YOY_SERIES: readonly SeriesConfig[] = MODALITIES.map(item => ({
  key: item.key,
  name: item.name,
  color: item.color,
  format: 'pct',
}))

function formatMillions(value: number): string {
  return `R$ ${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mi`
}

function formatPercent(value: number): string {
  return `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`
}

function formatMonth(reference: string | null): string {
  if (reference === null) return 'sem referência'
  const match = /^(\d{4})-(\d{2})-\d{2}$/.exec(reference)
  return match === null ? reference : `${match[2]}/${match[1]}`
}

function reasonText(reason: string | null): string {
  switch (reason) {
    case 'no_valid_observations': return 'Sem observações válidas em cache.'
    case 'invalid_current': return 'A observação mais recente não é válida para cálculo.'
    case 'missing_current': return 'Sem saldo válido neste mês.'
    case 'missing_base': return 'Mês-base esperado indisponível.'
    case 'zero_base': return 'Variação indefinida: saldo do mês-base igual a zero.'
    case 'invalid_base': return 'Mês-base contém observação inválida.'
    case 'invalid_result': return 'Variação não finita; cálculo indisponível.'
    default: return 'Indicador indisponível.'
  }
}

function latestText(modality: CreditModality | null | undefined): string {
  const value = modality?.latest.value
  return value === null || value === undefined ? 'Indisponível' : formatMillions(value)
}

function growthCell(
  metric: CreditModality['mom'] | null | undefined,
): { readonly value: string; readonly details: string } {
  if (metric === null || metric === undefined || metric.value_pct === null) {
    const reason = metric?.reason ?? null
    const base = metric?.base_reference
    return {
      value: 'Indisponível',
      details: `${reasonText(reason)}${base ? ` Mês-base esperado: ${formatMonth(base)}.` : ''}`,
    }
  }
  return {
    value: formatPercent(metric.value_pct),
    details: `Mês-base esperado: ${formatMonth(metric.base_reference)}${metric.base_observation_reference === null
      ? '.'
      : `; observação usada: ${formatMonth(metric.base_observation_reference)}.`}`,
  }
}

function sourceLink(modality: CreditModality | null | undefined, code: number) {
  const href = modality?.source_url
  if (!href) return <span>Metadados SGS indisponíveis</span>
  return (
    <a href={href} target="_blank" rel="noreferrer" aria-label={`Abrir dados oficiais do SGS, série ${code}`}>
      Fonte SGS {code}
    </a>
  )
}

function ModalityRow({
  item,
  modality,
  metadata,
}: {
  readonly item: ModalityConfig
  readonly modality: CreditModality | null | undefined
  readonly metadata: CreditMetadata | undefined
}) {
  const mom = growthCell(modality?.mom)
  const yoy = growthCell(modality?.yoy)
  const unavailable = modality === null || modality === undefined || modality.unavailable
    || modality.latest.value === null
  const stale = metadata?.freshness === 'stale'
  const latestReference = modality?.latest.reference ?? metadata?.reference_period ?? null
  const officialName = item.code === 20574
    ? 'Crédito pessoal não consignado total'
    : item.code === 20588
      ? 'Cartão parcelado (operações com juros)'
      : modality?.official_label ?? item.name
  return (
    <tr>
      <th scope="row">
        <span className="credit-modality-name">{item.name}</span>
        <span className="credit-modality-code">SGS {item.code}</span>
        <span className="credit-modality-definition">{officialName}</span>
        <span className="credit-source-link">{sourceLink(modality, item.code)}</span>
      </th>
      <td>
        <strong>{unavailable ? 'Indisponível' : latestText(modality)}</strong>
        <span className="credit-cell-detail">Ref. {formatMonth(latestReference)}</span>
        {stale && <span className="credit-stale" role="status">Referência possivelmente desatualizada.</span>}
        {unavailable && (
          <span className="credit-cell-detail">
            {modality === undefined
              ? 'Modalidade ausente nesta resposta.'
              : modality === null
                ? 'Modalidade indisponível.'
                : reasonText(modality.latest.reason)}
          </span>
        )}
      </td>
      <td>
        <strong>{mom.value}</strong>
        <span className="credit-cell-detail">{mom.details}</span>
      </td>
      <td>
        <strong>{yoy.value}</strong>
        <span className="credit-cell-detail">{yoy.details}</span>
      </td>
    </tr>
  )
}

function CreditExposureSection({
  modalities,
  metadata,
}: {
  readonly modalities: CreditResponse['modalities']
  readonly metadata: CreditResponse['metadata']
}) {
  const tableScrollRef = useRef<HTMLDivElement>(null)
  const hasModalities = modalities !== undefined
    && MODALITIES.some(item => modalities[item.key] !== undefined && modalities[item.key] !== null)
  const stockRows = mergeCreditExposureHistory(modalities, 'value')
  const yoyRows = mergeCreditExposureHistory(modalities, 'yoy_pct')
  const stockFormatter = (value: number) => formatMillions(value)

  return (
    <section className="credit-exposure-section" aria-labelledby="credit-exposure-title">
      <h2 id="credit-exposure-title">Qualidade e estresse do crédito PF</h2>
      <p className="credit-exposure-intro">Modalidades selecionadas — exposição nominal de crédito livre para pessoas físicas.</p>
      {!hasModalities ? (
        <p className="credit-exposure-unavailable" role="status">
          Dados das modalidades de crédito não vieram nesta resposta. Os indicadores e gráficos de crédito disponíveis continuam exibidos.
        </p>
      ) : (
        <>
          <div className="credit-table-scroll-controls">
            <button type="button" aria-label="Rolar tabela para a esquerda" onClick={() => tableScrollRef.current?.scrollBy({ left: -240 })}>←</button>
            <button type="button" aria-label="Rolar tabela para a direita" onClick={() => tableScrollRef.current?.scrollBy({ left: 240 })}>→</button>
          </div>
          <div className="credit-exposure-table-wrap" ref={tableScrollRef}>
            <table className="credit-exposure-table">
              <caption>Saldos nominais e variações mensais e anuais por modalidade</caption>
              <thead>
                <tr>
                  <th scope="col">Modalidade e fonte</th>
                  <th scope="col">Saldo (R$ milhões)</th>
                  <th scope="col">Variação mensal</th>
                  <th scope="col">Variação em 12 meses</th>
                </tr>
              </thead>
              <tbody>
                {MODALITIES.map(item => (
                  <ModalityRow
                    key={item.key}
                    item={item}
                    modality={modalities?.[item.key]}
                    metadata={metadata[item.key]}
                  />
                ))}
              </tbody>
              </table>
          </div>
          <p className="credit-table-scroll-hint">Em telas estreitas, deslize a tabela horizontalmente para ver todas as colunas.</p>
          <p className="credit-exposure-method">
            Saldos nominais ao fim do mês; crédito livre PF em modalidades selecionadas, não exaustivas e sem soma entre modalidades. Crescimento do saldo, isoladamente, não comprova deterioração da qualidade.
            {' '}Cheque especial é crédito automático vinculado à conta; crédito pessoal não consignado não tem desconto em folha. Cartão parcelado inclui operações com juros, não parcelamento lojista sem juros em compras à vista.
          </p>
          <p className="credit-exposure-method">
            As medidas de inadimplência, custo e capacidade de serviço da dívida exibidas acima têm universos e referências próprios. Uma referência de julho em endividamento e uma de agosto em saldo não são o mesmo mês nem devem ser combinadas como tal. As séries SGS registram revisão integral em fevereiro de 2017; isso não identifica quais observações mudaram nem prova uma quebra posterior.
          </p>
          <p className="credit-exposure-method">
            Hipótese de cenário, não previsão: condições fiscais podem afetar juros e inflação, mas também renda e emprego; o efeito líquido sobre o crédito PF não é determinado por estes saldos.
          </p>
          <TimeSeriesChart
            data={stockRows}
            series={STOCK_SERIES}
            title="Saldos nominais das modalidades selecionadas"
            yLabel="R$ milhões"
            yTickFormatter={stockFormatter}
            defaultWindow="5Y"
            connectNulls={false}
            showReferenceDates
          />
          <TimeSeriesChart
            data={yoyRows}
            series={YOY_SERIES}
            title="Variação dos saldos em 12 meses por modalidade"
            yLabel="Variação em 12 meses (%)"
            yTickFormatter={value => `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`}
            defaultWindow="5Y"
            connectNulls={false}
            showReferenceDates
          />
        </>
      )}
    </section>
  )
}

export default CreditExposureSection
