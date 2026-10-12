import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { YieldCurveChart, type CurvasDiData } from '../charts/YieldCurveChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { COPOM_DATES } from '../utils/copom'
import type { RawSeriesPoint, SeriesTransforms } from '../types/indicators'
import { lastValidSeriesPoint, mergeSeriesByDate } from '../utils/series'

type InterestRateResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

const annualize252 = (value: number): number => ((1 + value / 100) ** 252 - 1) * 100
const annualize12 = (value: number): number => ((1 + value / 100) ** 12 - 1) * 100

const RATE_TRANSFORMS = {
  selic_efetiva: annualize252,
  cdi: annualize252,
  tr: annualize12,
} satisfies SeriesTransforms

const META_SERIES = [
  { key: 'selic_meta', name: 'Selic Meta', color: '#ff6b6b', format: 'pct' as const, desc: 'Meta para a taxa Selic definida pelo COPOM — principal instrumento de política monetária do Banco Central.' },
]

const MERCADO_SERIES = [
  { key: 'selic_efetiva', name: 'Selic Efetiva', color: '#ffa502', format: 'pct' as const, desc: 'Taxa média das operações overnight com lastro em títulos públicos — reflete o custo real de captação bancário.' },
  { key: 'cdi', name: 'CDI', color: '#4ecdc4', format: 'pct' as const, desc: 'Certificado de Depósito Interbancário — referência para rendimentos de CDBs, LCIs, LCAs e fundos.' },
  { key: 'tr', name: 'TR', color: '#a29bfe', format: 'pct' as const, desc: 'Taxa Referencial — usada como indexador em financiamentos imobiliários (SBPE) e Poupança.' },
]

const ALL_SERIES = [...META_SERIES, ...MERCADO_SERIES]

function isCopomDate(dateStr: string): boolean {
  return COPOM_DATES.includes(dateStr)
}

function Juros() {
  const { data, loading, error, isStale } = useFetch<InterestRateResponse>('/api/juros')
  const di = useFetch<CurvasDiData>('/api/curvas-di?days=30')

  if (loading) return <Loading />
  if (error && !data) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />
  const interestRateData = data.data

  const merged = mergeSeriesByDate(interestRateData, RATE_TRANSFORMS)

  const copomData = merged.filter(row => isCopomDate(row.date))

  const last = (key: string) => {
    return lastValidSeriesPoint(interestRateData[key])
  }

  const formatRate = (key: string, v: number) => {
    if (key === 'selic_meta') return `${v.toFixed(1)}% a.a.`
    if (key === 'selic_efetiva' || key === 'cdi') {
      const annual = annualize252(v)
      return `${annual.toFixed(1)}% a.a.`
    }
    if (key === 'tr') {
      const annual = annualize12(v)
      return `${annual.toFixed(1)}% a.a.`
    }
    return `${v}%`
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="juros" isStale={isStale} />
      <div className="kpi-row">
        {ALL_SERIES.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? formatRate(s.key, l.value) : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <TimeSeriesChart
        data={copomData}
        series={META_SERIES}
        title="Taxa Meta Selic (reuniões COPOM)"
        yLabel="% a.a."
      />
      <TimeSeriesChart
        data={merged}
        series={MERCADO_SERIES}
        title="Taxas de Mercado (anualizadas)"
        yLabel="% a.a."
      />
      <YieldCurveChart data={di.data} />
    </div>
  )
}

export default Juros
