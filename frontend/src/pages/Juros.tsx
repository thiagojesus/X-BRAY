import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { YieldCurveChart, type CurvasDiData } from '../charts/YieldCurveChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { COPOM_DATES } from '../utils/copom'
import type { RawSeriesPoint, SeriesTransforms } from '../types/indicators'
import { lastValidSeriesPoint, mergeSeriesByDate, parseFiniteSeriesValue, parseSeriesDate } from '../utils/series'

type InterestRateResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

const annualize252 = (value: number): number => ((1 + value / 100) ** 252 - 1) * 100
const RATE_TRANSFORMS = {
  selic_efetiva: annualize252,
  cdi: annualize252,
} satisfies SeriesTransforms

const META_SERIES = [
  { key: 'selic_meta', name: 'Selic Meta', color: '#ff6b6b', format: 'pct' as const, desc: 'Meta para a taxa Selic definida pelo COPOM — principal instrumento de política monetária do Banco Central.' },
]

const MERCADO_SERIES = [
  { key: 'selic_efetiva', name: 'Selic Efetiva', color: '#ffa502', format: 'pct' as const, desc: 'Taxa média das operações overnight com lastro em títulos públicos — reflete o custo real de captação bancário.' },
  { key: 'cdi', name: 'CDI', color: '#4ecdc4', format: 'pct' as const, desc: 'Certificado de Depósito Interbancário — referência para rendimentos de CDBs, LCIs, LCAs e fundos.' },
]

const ALL_SERIES = [...META_SERIES, ...MERCADO_SERIES, { key: 'tr', name: 'TR', color: '#a29bfe', format: 'pct' as const, desc: 'Taxa Referencial do BCB; cada observação preserva seu período oficial.' }]

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
  const trRows = interestRateData.tr ?? []
  const latestTrStart = trRows.reduce((latest, row) => {
    const rowDate = parseSeriesDate(row.data)?.sortKey ?? Number.NEGATIVE_INFINITY
    const latestDate = parseSeriesDate(latest)?.sortKey ?? Number.NEGATIVE_INFINITY
    return rowDate > latestDate ? row.data : latest
  }, '')
  const latestTrPeriods = trRows.filter(row => row.data === latestTrStart)

  const copomData = merged.filter(row => isCopomDate(row.date))

  const last = (key: string) => {
    if (key === 'tr') {
      if (latestTrPeriods.length !== 1) return null
      const period = latestTrPeriods[0]
      const value = parseFiniteSeriesValue(period.valor)
      if (!period.dataFim || value === null) return null
      return { data: `${period.data}–${period.dataFim}`, value }
    }
    return lastValidSeriesPoint(interestRateData[key])
  }

  const formatRate = (key: string, v: number) => {
    if (key === 'selic_meta') return `${v.toFixed(1)}% a.a.`
    if (key === 'selic_efetiva' || key === 'cdi') {
      const annual = annualize252(v)
      return `${annual.toFixed(1)}% a.a.`
    }
    if (key === 'tr') return `${v.toFixed(4)}% no período`
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
      <section aria-labelledby="tr-periods-title" className="card">
        <h2 id="tr-periods-title">TR — períodos oficiais mais recentes</h2>
        {latestTrPeriods.length ? (
          <ul>
            {latestTrPeriods.map(row => (
              <li key={`${row.data}:${row.dataFim ?? 'unknown'}`}>
                {row.data} a {row.dataFim || 'fim não informado'}: {(parseFiniteSeriesValue(row.valor)?.toFixed(4) ?? '—')}% no período
              </li>
            ))}
          </ul>
        ) : <p>Sem períodos TR com identidade completa disponíveis.</p>}
      </section>
      <YieldCurveChart data={di.data} />
    </div>
  )
}

export default Juros
