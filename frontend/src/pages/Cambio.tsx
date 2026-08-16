import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'

function Cambio() {
  const { data, loading, error } = useFetch<any>('/api/cambio')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  const merged: Record<string, any>[] = []
  const allDates = new Set<string>()
  for (const points of Object.values(data.data)) {
    if (Array.isArray(points)) for (const p of points) allDates.add(p.data)
  }
  const sorted = Array.from(allDates).sort((a, b) => {
    const [da, ma, ya] = a.split('/').map(Number)
    const [db, mb, yb] = b.split('/').map(Number)
    return ya * 10000 + ma * 100 + da - (yb * 10000 + mb * 100 + db)
  })
  for (const date of sorted) {
    const row: Record<string, any> = { date }
    for (const [name, points] of Object.entries(data.data)) {
      if (Array.isArray(points)) {
        const m = points.find((p: any) => p.data === date)
        if (m) row[name] = parseFloat(m.valor.replace(',', '.'))
      }
    }
    merged.push(row)
  }

  const series = [
    { key: 'ptax_compra_usd', name: 'PTAX Compra USD', color: '#4ecdc4', format: 'brl' as const },
    { key: 'ptax_venda_usd', name: 'PTAX Venda USD', color: '#ff6b6b', format: 'brl' as const },
    { key: 'eur_brl', name: 'EUR/BRL', color: '#ffa502', format: 'brl' as const },
  ]

  const last = (key: string) => {
    const pts = data.data[key]
    return Array.isArray(pts) && pts.length ? pts[pts.length - 1] : null
  }

  return (
    <div className="page">
      <div className="kpi-row">
        {series.map(s => {
          const l = last(s.key)
          return (
            <div key={s.key} className="kpi-card" style={{ borderTopColor: s.color }}>
              <span className="kpi-label">{s.name}</span>
              <span className="kpi-value">{l ? `R$ ${l.valor}` : '—'}</span>
              <span className="kpi-date">{l?.data || ''}</span>
            </div>
          )
        })}
      </div>
      <TimeSeriesChart data={merged} series={series} title="Câmbio — Histórico" yLabel="R$" />
    </div>
  )
}

export default Cambio
