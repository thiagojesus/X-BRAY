import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import type { SeriesConfig } from '../charts/TimeSeriesChart'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { useFetch } from '../hooks/useFetch'
import type { IndicatorUnit, RawSeriesPoint, SeriesValue } from '../types/indicators'
import { formatIndicatorValue } from '../utils/format'
import { lastValidSeriesPoint, mergeSeriesByDate, parseFiniteSeriesValue } from '../utils/series'

type FiscalStatus = 'ok' | 'partial' | 'unavailable'
type SignConvention = 'debt_positive' | 'surplus_positive' | 'cost_positive'

type FiscalMetadata = {
  readonly code: number
  readonly unit: IndicatorUnit
  readonly frequency: string
  readonly freshness: 'ok' | 'stale'
  readonly sign_convention: SignConvention
}

type FiscalResponse = {
  readonly source: string
  readonly last_updated: string | null
  readonly status: FiscalStatus
  readonly data: Readonly<Record<string, readonly RawSeriesPoint[]>>
  readonly metadata: Readonly<Record<string, FiscalMetadata>>
  readonly unavailable: readonly string[]
}

type FiscalSeries = SeriesConfig & {
  readonly description: string
}

const DEBT_SERIES: FiscalSeries[] = [
  { key: 'dbgg_pib', name: 'DBGG (% do PIB)', color: '#ff6b6b', format: 'pct', description: 'Dívida Bruta do Governo Geral em relação ao PIB.' },
  { key: 'dlsp_pib', name: 'DLSP (% do PIB)', color: '#ffa502', format: 'pct', description: 'Dívida Líquida do Setor Público em relação ao PIB.' },
]

const BALANCE_SERIES: FiscalSeries[] = [
  { key: 'saldo_primario_12m_pib', name: 'Saldo primário 12m', color: '#4ecdc4', format: 'pct', description: 'Resultado primário acumulado em 12 meses, sem juros, em relação ao PIB.' },
  { key: 'saldo_nominal_12m_pib', name: 'Saldo nominal 12m', color: '#a29bfe', format: 'pct', description: 'Resultado nominal acumulado em 12 meses, incluindo juros, em relação ao PIB.' },
]

const INTEREST_SERIES: FiscalSeries[] = [
  { key: 'juros_nominais_12m_pib', name: 'Juros nominais 12m', color: '#fd79a8', format: 'pct', description: 'Juros nominais acumulados em 12 meses como custo positivo em relação ao PIB.' },
]

const ALL_SERIES = [...DEBT_SERIES, ...BALANCE_SERIES.slice(0, 1), ...INTEREST_SERIES, ...BALANCE_SERIES.slice(1)]

function formatBalance(value: number): string {
  const magnitude = formatIndicatorValue(Math.abs(value), 'pct_gdp') ?? ''
  if (value > 0) return `Superávit · +${magnitude}`
  if (value < 0) return `Déficit · −${magnitude}`
  return `Equilíbrio · ${magnitude}`
}

function formatKpiValue(key: string, value: SeriesValue): string {
  const parsed = parseFiniteSeriesValue(value)
  if (parsed === null) return ''
  if (key === 'saldo_primario_12m_pib' || key === 'saldo_nominal_12m_pib') {
    return formatBalance(parsed)
  }
  const formatted = formatIndicatorValue(parsed, 'pct_gdp') ?? ''
  return key === 'juros_nominais_12m_pib' ? `Custo · ${formatted}` : formatted
}

function cardColor(series: FiscalSeries, value: SeriesValue): string {
  if (!BALANCE_SERIES.some(item => item.key === series.key)) return series.color
  const parsed = parseFiniteSeriesValue(value)
  if (parsed === null || parsed === 0) return '#ffd166'
  return parsed > 0 ? '#4ecdc4' : '#ff6b6b'
}

function Fiscal() {
  const { data, loading, error, isStale } = useFetch<FiscalResponse>('/api/fiscal')

  if (loading) {
    return <div className="page"><DataQualityNotice dataset="fiscal" loading /></div>
  }
  if (error && !data) {
    return <div className="page"><DataQualityNotice dataset="fiscal" error={error} /></div>
  }
  if (!data?.data) {
    return <div className="page"><DataQualityNotice dataset="fiscal" error="Sem dados" /></div>
  }

  const merged = mergeSeriesByDate(data.data)
  const quality = {
    status: data.status,
    last_updated: data.last_updated,
    metadata: data.metadata,
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="fiscal" quality={quality} isStale={isStale} />
      <div className="info-box">
        <p><strong>Fonte:</strong> {data.source}. Todos os indicadores são mensais e medidos em % do PIB.</p>
        <p><strong>DBGG</strong> é a Dívida Bruta do Governo Geral; <strong>DLSP</strong> é a Dívida Líquida do Setor Público.</p>
        <p><strong>Convenção NFSP:</strong> superávit aparece positivo, déficit negativo e juros nominais permanecem positivos como custo.</p>
      </div>
      <div className="kpi-row">
        {ALL_SERIES.map(series => {
          const latest = lastValidSeriesPoint(data.data[series.key])
          return (
            <KpiCard
              key={series.key}
              name={series.name}
              value={latest ? formatKpiValue(series.key, latest.valor) : ''}
              date={latest?.data ?? ''}
              color={cardColor(series, latest?.valor)}
              description={series.description}
            />
          )
        })}
      </div>
      <TimeSeriesChart data={merged} series={DEBT_SERIES} title="DBGG × DLSP" yLabel="% do PIB" defaultWindow="5Y" />
      <TimeSeriesChart data={merged} series={BALANCE_SERIES} title="Saldos primário × nominal" yLabel="% do PIB" defaultWindow="5Y" />
      <TimeSeriesChart data={merged} series={INTEREST_SERIES} title="Juros nominais" yLabel="% do PIB" defaultWindow="5Y" />
    </div>
  )
}

export default Fiscal
