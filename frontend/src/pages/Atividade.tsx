import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import type { RawSeriesPoint, SeriesValue } from '../types/indicators'
import { formatIndicatorValue } from '../utils/format'
import { lastValidSeriesPoint, mergeSeriesByDate, parseFiniteSeriesValue } from '../utils/series'

type ActivityResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

function Atividade() {
  const { data, loading, error, isStale } = useFetch<ActivityResponse>('/api/atividade')

  if (loading) return <Loading />
  if (error && !data) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />
  const activityData = data.data

  const merged = mergeSeriesByDate(activityData)

  const series = [
    { key: 'pib', name: 'PIB (R$)', color: '#ff6b6b', format: 'brl' as const, desc: 'Produto Interno Bruto nominal anual — soma de todos os bens e serviços finais produzidos no país, em valores correntes.' },
    { key: 'ibc_br', name: 'IBC-Br (índice)', color: '#4ecdc4', format: 'idx' as const, desc: 'Índice de Atividade Econômica do Banco Central — proxy mensal do PIB com alta frequência, base 100 em 2002.' },
    { key: 'desemprego', name: 'Desemprego (%)', color: '#ffa502', format: 'pct' as const, desc: 'Taxa de desemprego da PNAD Contínua (IBGE) — percentual da população economicamente ativa sem emprego e buscando trabalho.' },
  ]

  const formatValue = (key: string, value: SeriesValue) => {
    const num = parseFiniteSeriesValue(value)
    if (num === null) return ''
    if (key === 'pib') {
      if (num >= 1e12) return `R$ ${(num / 1e12).toFixed(2)} tri`
      if (num >= 1e9) return `R$ ${(num / 1e9).toFixed(0)} bi`
      return `R$ ${num.toLocaleString('pt-BR')}`
    }
    if (key === 'ibc_br') {
      return formatIndicatorValue(num, 'index') ?? ''
    }
    return `${num.toFixed(1)}%`
  }

  const last = (key: string) => {
    return lastValidSeriesPoint(activityData[key])
  }

  const ibcDespSeries = [
    { key: 'ibc_br', name: 'IBC-Br (índice)', color: '#4ecdc4', format: 'idx' as const },
    { key: 'desemprego', name: 'Desemprego (%)', color: '#ffa502', yAxisId: 'right' as const, format: 'pct' as const },
  ]

  const pibYTickFormatter = (val: number) => {
    if (val >= 1e12) return `R$ ${(val / 1e12).toFixed(1)} tri`
    if (val >= 1e9) return `R$ ${(val / 1e9).toFixed(0)} bi`
    return `R$ ${val.toLocaleString('pt-BR')}`
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="atividade" isStale={isStale} />
      <div className="kpi-row">
        {series.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? formatValue(s.key, l.valor) : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <TimeSeriesChart data={merged} series={[series[0]]} title="PIB (valores anuais)" yLabel="R$" xAxisFormat="year" defaultWindow="ALL" yTickFormatter={pibYTickFormatter} />
      <TimeSeriesChart data={merged} series={ibcDespSeries} title="IBC-Br vs Desemprego" />
    </div>
  )
}

export default Atividade
