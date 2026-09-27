import { type SeriesConfig, TimeSeriesChart } from '../charts/TimeSeriesChart'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { useFetch } from '../hooks/useFetch'
import type { IndicatorUnit, RawSeriesPoint } from '../types/indicators'
import { formatIndicatorValue } from '../utils/format'
import { lastValidSeriesPoint, mergeSeriesByMonthLastValue } from '../utils/series'

type ExternalStatus = 'ok' | 'partial' | 'unavailable'

type ExternalMetadata = {
  readonly code: number
  readonly unit: IndicatorUnit
  readonly frequency: string
  readonly reference_period: string | null
  readonly deadline: string | null
  readonly freshness: 'ok' | 'stale'
}

type ExternalResponse = {
  readonly source: string
  readonly last_updated: string | null
  readonly status: ExternalStatus
  readonly data: Readonly<Record<string, readonly RawSeriesPoint[]>>
  readonly metadata: Readonly<Record<string, ExternalMetadata>>
  readonly unavailable: readonly string[]
}

type ExternalKpi = {
  readonly key: string
  readonly name: string
  readonly color: string
  readonly fallbackUnit: IndicatorUnit
  readonly description: string
}

const KPI_SERIES: readonly ExternalKpi[] = [
  { key: 'conta_corrente_12m_pib', name: 'Conta corrente 12m / PIB', color: '#ff6b6b', fallbackUnit: 'pct_gdp', description: 'Saldo em transações correntes acumulado em doze meses em relação ao PIB.' },
  { key: 'idp_12m_pib', name: 'IDP 12m / PIB', color: '#4ecdc4', fallbackUnit: 'pct_gdp', description: 'Investimento Direto no País acumulado em doze meses em relação ao PIB.' },
  { key: 'balanca_comercial_usd', name: 'Balança comercial', color: '#ffa502', fallbackUnit: 'usd_million', description: 'Saldo mensal da balança comercial no balanço de pagamentos.' },
  { key: 'investimento_carteira_usd', name: 'Investimento em carteira', color: '#a29bfe', fallbackUnit: 'usd_million', description: 'Fluxo líquido mensal de investimentos em carteira.' },
  { key: 'reservas_internacionais', name: 'Reservas internacionais', color: '#74b9ff', fallbackUnit: 'usd_million', description: 'Estoque mensal de reservas internacionais do Banco Central.' },
]

const CURRENT_ACCOUNT_SERIES: SeriesConfig[] = [
  { key: 'conta_corrente_12m_pib', name: 'Conta corrente 12m / PIB', color: '#ff6b6b', format: 'pct' },
  { key: 'idp_12m_pib', name: 'IDP 12m / PIB', color: '#4ecdc4', format: 'pct' },
]

const FLOW_SERIES: SeriesConfig[] = [
  { key: 'idp_mensal_usd', name: 'IDP mensal', color: '#4ecdc4', format: 'usd' },
  { key: 'investimento_carteira_usd', name: 'Investimento em carteira', color: '#a29bfe', format: 'usd' },
]

const TRADE_SERIES: SeriesConfig[] = [
  { key: 'balanca_comercial_usd', name: 'Balança comercial', color: '#ffa502', format: 'usd' },
]

const RESERVE_SERIES: SeriesConfig[] = [
  { key: 'reservas_internacionais', name: 'Reservas internacionais', color: '#74b9ff', format: 'usd' },
]

function coverageInsight(currentAccount: number | undefined, directInvestment: number | undefined): string {
  if (currentAccount === undefined || directInvestment === undefined || currentAccount >= 0) {
    return 'Cobertura por IDP indisponível'
  }
  const ratio = directInvestment / Math.abs(currentAccount)
  const formatted = ratio.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `Cobertura estimada por IDP: ${formatted}× — ${ratio >= 1 ? 'coberto' : 'não coberto'}.`
}

function SetorExterno() {
  const external = useFetch<ExternalResponse>('/api/setor-externo')

  if (external.loading) {
    return <div className="page"><DataQualityNotice dataset="setor-externo" loading /></div>
  }
  if (external.error && !external.data) {
    return <div className="page"><DataQualityNotice dataset="setor-externo" error={external.error} /></div>
  }
  if (!external.data?.data) {
    return <div className="page"><DataQualityNotice dataset="setor-externo" error="Sem dados" /></div>
  }

  const response = external.data
  const currentAccount = lastValidSeriesPoint(response.data.conta_corrente_12m_pib)?.value
  const directInvestment = lastValidSeriesPoint(response.data.idp_12m_pib)?.value
  const monthly = mergeSeriesByMonthLastValue(response.data)
  const quality = {
    status: response.status,
    last_updated: response.last_updated,
    metadata: response.metadata,
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="setor-externo" quality={quality} isStale={external.isStale} />
      <div className="info-box">
        <p><strong>Fonte:</strong> {response.source}. Indicadores mensais do setor externo brasileiro.</p>
        <p><strong>Leitura:</strong> {coverageInsight(currentAccount, directInvestment)}</p>
        <p>A cobertura compara IDP acumulado em 12 meses ao módulo do déficit em conta corrente no mesmo período. PTAX permanece em Câmbio.</p>
      </div>
      <div className="kpi-row">
        {KPI_SERIES.map(series => {
          const latest = lastValidSeriesPoint(response.data[series.key])
          const unit = response.metadata[series.key]?.unit ?? series.fallbackUnit
          return (
            <KpiCard
              key={series.key}
              name={series.name}
              value={latest ? formatIndicatorValue(latest.valor, unit) ?? 'Indisponível' : 'Indisponível'}
              date={latest?.data ?? ''}
              color={series.color}
              description={series.description}
            />
          )
        })}
      </div>
      <TimeSeriesChart data={monthly} series={CURRENT_ACCOUNT_SERIES} title="Conta corrente × IDP" yLabel="% do PIB" defaultWindow="5Y" connectNulls={false} showReferenceDates />
      <TimeSeriesChart data={monthly} series={FLOW_SERIES} title="IDP mensal × investimento em carteira" yLabel="US$ milhões" defaultWindow="5Y" connectNulls={false} showReferenceDates />
      <TimeSeriesChart data={monthly} series={TRADE_SERIES} title="Balança comercial mensal" yLabel="US$ milhões" defaultWindow="5Y" connectNulls={false} showReferenceDates />
      <TimeSeriesChart data={monthly} series={RESERVE_SERIES} title="Reservas internacionais" yLabel="US$ milhões" defaultWindow="5Y" connectNulls={false} showReferenceDates />
    </div>
  )
}

export default SetorExterno
