import { useFetch } from '../hooks/useFetch'
import { parseSgsData } from '../utils/parse'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'

const SERIES = [
  { key: 'selic_meta', name: 'Selic Meta', color: '#ff6b6b' },
  { key: 'cdi', name: 'CDI', color: '#4ecdc4' },
  { key: 'selic_efetiva', name: 'Selic Efetiva', color: '#ffa502' },
  { key: 'tr', name: 'TR', color: '#a29bfe' },
]

const COLORS: Record<string, string> = {
  selic_meta: '#ff6b6b',
  cdi: '#4ecdc4',
  selic_efetiva: '#ffa502',
  tr: '#a29bfe',
}

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
        if (match) row[name] = parseFloat(match.valor.replace(',', '.'))
      }
    }
    merged.push(row)
  }

  return (
    <div className="page">
      <div className="kpi-row">
        {SERIES.map(s => {
          const points = data.data[s.key]
          const last = Array.isArray(points) && points.length > 0 ? points[points.length - 1] : null
          return (
            <div key={s.key} className="kpi-card" style={{ borderTopColor: s.color }}>
              <span className="kpi-label">{s.name}</span>
              <span className="kpi-value">{last ? `${last.valor}%` : '—'}</span>
              <span className="kpi-date">{last?.data || ''}</span>
            </div>
          )
        })}
      </div>
      <TimeSeriesChart
        data={merged}
        series={SERIES}
        title="Taxas de Juros — Histórico"
        yLabel="% a.a."
      />
    </div>
  )
}

export default Juros
