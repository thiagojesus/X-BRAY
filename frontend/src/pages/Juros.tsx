import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { KpiCard } from '../components/KpiCard'

const META_SERIES = [
  { key: 'selic_meta', name: 'Selic Meta', color: '#ff6b6b', format: 'pct' as const, desc: 'Meta para a taxa Selic definida pelo COPOM — principal instrumento de política monetária do Banco Central.' },
]

const MERCADO_SERIES = [
  { key: 'selic_efetiva', name: 'Selic Efetiva', color: '#ffa502', format: 'pct' as const, desc: 'Taxa média das operações overnight com lastro em títulos públicos — reflete o custo real de captação bancário.' },
  { key: 'cdi', name: 'CDI', color: '#4ecdc4', format: 'pct' as const, desc: 'Certificado de Depósito Interbancário — referência para rendimentos de CDBs, LCIs, LCAs e fundos.' },
  { key: 'tr', name: 'TR', color: '#a29bfe', format: 'pct' as const, desc: 'Taxa Referencial — usada como indexador em financiamentos imobiliários (SBPE) e Poupança.' },
]

const ALL_SERIES = [...META_SERIES, ...MERCADO_SERIES]

function Juros() {
  const { data, loading, error } = useFetch<any>('/api/juros')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  const merged: Record<string, any>[] = []
  const allDates = new Set<string>()

  for (const [name, points] of Object.entries(data.data)) {
    if (Array.isArray(points)) {
      for (const p of points) allDates.add(p.data)
    }
  }

  const sortedDates = Array.from(allDates).sort((a, b) => {
    const [da, ma, ya] = a.split('/').map(Number)
    const [db, mb, yb] = b.split('/').map(Number)
    return ya * 10000 + ma * 100 + da - (yb * 10000 + mb * 100 + db)
  })

  for (const date of sortedDates) {
    const row: Record<string, any> = { date }
    for (const [name, points] of Object.entries(data.data)) {
      if (Array.isArray(points)) {
        const match = points.find((p: any) => p.data === date)
        if (match) {
          const v = parseFloat(match.valor.replace(',', '.'))
          if (name === 'selic_meta') {
            row[name] = v
          } else if (name === 'selic_efetiva' || name === 'cdi') {
            row[name] = ((1 + v / 100) ** 252 - 1) * 100
          } else if (name === 'tr') {
            row[name] = ((1 + v / 100) ** 12 - 1) * 100
          } else {
            row[name] = v
          }
        }
      }
    }
    merged.push(row)
  }

  const last = (key: string) => {
    const pts = data.data[key]
    return Array.isArray(pts) && pts.length ? pts[pts.length - 1] : null
  }

  const formatRate = (key: string, val: string) => {
    const v = parseFloat(val.replace(',', '.'))
    if (key === 'selic_meta') return `${v.toFixed(1)}% a.a.`
    if (key === 'selic_efetiva' || key === 'cdi') {
      const annual = ((1 + v / 100) ** 252 - 1) * 100
      return `${annual.toFixed(1)}% a.a.`
    }
    if (key === 'tr') {
      const annual = ((1 + v / 100) ** 12 - 1) * 100
      return `${annual.toFixed(1)}% a.a.`
    }
    return `${v}%`
  }

  return (
    <div className="page">
      <div className="kpi-row">
        {ALL_SERIES.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? formatRate(s.key, l.valor) : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <TimeSeriesChart
        data={merged}
        series={META_SERIES}
        title="Taxa Meta Selic"
        yLabel="% a.a."
      />
      <TimeSeriesChart
        data={merged}
        series={MERCADO_SERIES}
        title="Taxas de Mercado (anualizadas)"
        yLabel="% a.a."
      />
    </div>
  )
}

export default Juros
