import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { KpiCard } from '../components/KpiCard'

function Atividade() {
  const { data, loading, error } = useFetch<any>('/api/atividade')

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
    { key: 'pib', name: 'PIB (R$)', color: '#ff6b6b', format: 'brl' as const, desc: 'Produto Interno Bruto nominal anual — soma de todos os bens e serviços finais produzidos no país, em valores correntes.' },
    { key: 'ibc_br', name: 'IBC-Br (índice)', color: '#4ecdc4', format: 'idx' as const, desc: 'Índice de Atividade Econômica do Banco Central — proxy mensal do PIB com alta frequência, base 100 em 2002.' },
    { key: 'desemprego', name: 'Desemprego (%)', color: '#ffa502', format: 'pct' as const, desc: 'Taxa de desemprego da PNAD Contínua (IBGE) — percentual da população economicamente ativa sem emprego e buscando trabalho.' },
  ]

  const formatValue = (key: string, val: string) => {
    const num = parseFloat(val.replace(',', '.'))
    if (key === 'pib') {
      if (num >= 1e12) return `R$ ${(num / 1e12).toFixed(2)} tri`
      if (num >= 1e9) return `R$ ${(num / 1e9).toFixed(0)} bi`
      return `R$ ${num.toLocaleString('pt-BR')}`
    }
    return `${num.toFixed(1)}%`
  }

  const last = (key: string) => {
    const pts = data.data[key]
    return Array.isArray(pts) && pts.length ? pts[pts.length - 1] : null
  }

  const ibcDespSeries = [
    { key: 'ibc_br', name: 'IBC-Br (índice)', color: '#4ecdc4', format: 'idx' as const },
    { key: 'desemprego', name: 'Desemprego (%)', color: '#ffa502', yAxisId: 'right' as const, format: 'pct' as const },
  ]

  return (
    <div className="page">
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
      <TimeSeriesChart data={merged} series={[series[0]]} title="PIB (valores anuais)" yLabel="R$" />
      <TimeSeriesChart data={merged} series={ibcDespSeries} title="IBC-Br vs Desemprego" />
    </div>
  )
}

export default Atividade
