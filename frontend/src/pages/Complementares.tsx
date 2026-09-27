import { Link } from 'react-router-dom'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { ErrorDisplay, Loading } from '../components/Status'
import { useFetch } from '../hooks/useFetch'
import type { RawSeriesPoint, SeriesValue } from '../types/indicators'
import { lastValidSeriesPoint, mergeSeriesByDate, parseFiniteSeriesValue } from '../utils/series'

type ComplementaryResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

const SERIES = [
  { key: 'ic_commodities', name: 'IC-Br Commodities', color: '#a29bfe', format: 'idx' as const, desc: 'Índice de Commodities Brasil do BCB — acompanha os preços, em reais, de commodities relevantes para a economia brasileira e para a análise dos termos de troca.' },
]

const AGREGADOS = [
  { key: 'm0', name: 'M0 (R$ mi)', color: '#ffa502', format: 'brl' as const, desc: 'M0 — base monetária: papel-moeda emitido mais reservas bancárias.' },
  { key: 'm1', name: 'M1 (R$ mi)', color: '#4ecdc4', format: 'brl' as const, desc: 'M1 — meios de pagamento restritos: papel-moeda em poder do público mais depósitos à vista.' },
  { key: 'm2', name: 'M2 (R$ mi)', color: '#a29bfe', format: 'brl' as const, desc: 'M2 — meios de pagamento ampliados: M1 mais depósitos de poupança e títulos emitidos por instituições depositárias.' },
]

const ALL_SERIES = [...SERIES, ...AGREGADOS]

function Complementares() {
  const { data, loading, error, isStale } = useFetch<ComplementaryResponse>('/api/complementares')

  if (loading) return <Loading />
  if (error && !data) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />
  const complementaryData = data.data

  const merged = mergeSeriesByDate(complementaryData)

  const hasData = merged.length > 0 && merged.some(row =>
    ALL_SERIES.some(s => row[s.key] != null)
  )

  if (!hasData) {
    return (
      <div className="page">
        <div className="info-box">
          <p><strong>Fonte:</strong> BCB SGS</p>
          <p>Sem dados disponíveis para os indicadores complementares solicitados.</p>
          <p><Link to="/setor-externo">Reservas agora em Setor Externo</Link></p>
        </div>
      </div>
    )
  }

  const last = (key: string) => {
    return lastValidSeriesPoint(complementaryData[key])
  }

  const formatVal = (key: string, val: SeriesValue) => {
    const num = parseFiniteSeriesValue(val)
    if (num === null) return ''
    const s = ALL_SERIES.find(x => x.key === key)
    if (s?.format === 'brl') {
      return `R$ ${num.toLocaleString('pt-BR', { maximumFractionDigits: 0 })}`
    }
    return num.toFixed(1)
  }

  const compactYTick = (val: number) => {
    if (Math.abs(val) >= 1e9) return `${(val / 1e9).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} bi`
    if (Math.abs(val) >= 1e6) return `${(val / 1e6).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mi`
    if (Math.abs(val) >= 1e3) return `${(val / 1e3).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} mil`
    return val.toLocaleString('pt-BR')
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="complementares" isStale={isStale} />
      <div className="kpi-row">
        {ALL_SERIES.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? formatVal(s.key, l.valor) : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <div className="info-box">
        <p><Link to="/setor-externo">Reservas agora em Setor Externo</Link></p>
      </div>
      <TimeSeriesChart
        data={merged}
        series={AGREGADOS}
        title="Agregados Monetários (M0, M1, M2)"
        yLabel="R$ milhões"
        yTickFormatter={compactYTick}
      />
      <TimeSeriesChart
        data={merged}
        series={[SERIES[0]]}
        title="IC-Br Commodities"
      />
    </div>
  )
}

export default Complementares
