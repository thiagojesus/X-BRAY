import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { KpiCard } from '../components/KpiCard'

const SERIES = [
  { key: 'reservas_internacionais', name: 'Reservas (USD mi)', color: '#4ecdc4', format: 'usd' as const, desc: 'Reservas internacionais líquidas do Banco Central — estoque de divisas que serve de colchão contra crises cambiais.' },
  { key: 'ic_commodities', name: 'IC-Br Commodities', color: '#a29bfe', format: 'idx' as const, desc: 'Índice de Commodities do BCB — preços de exportação do Brasil em dólares, base 100 em jul/2006; impacta câmbio e termos de troque.' },
]

const AGREGADOS = [
  { key: 'm0', name: 'M0 (R$ mi)', color: '#ffa502', format: 'brl' as const, desc: 'M0 — base monetária: papel-moeda emitido mais reservas bancárias; reflete a política monetária expansiva ou contracionista.' },
  { key: 'm1', name: 'M1 (R$ mi)', color: '#4ecdc4', format: 'brl' as const, desc: 'M1 — meios de pagamento restritos: papel-moeda em poder do público mais depósitos à vista.' },
  { key: 'm2', name: 'M2 (R$ mi)', color: '#a29bfe', format: 'brl' as const, desc: 'M2 — meios de pagamento ampliados: M1 mais depósitos de poupança e títulos emitidos por instituições depositárias.' },
]

const ALL_SERIES = [...SERIES, ...AGREGADOS]

function Complementares() {
  const { data, loading, error } = useFetch<any>('/api/complementares')

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

  const hasData = merged.length > 0 && merged.some(row =>
    ALL_SERIES.some(s => row[s.key] != null)
  )

  if (!hasData) {
    return (
      <div className="page">
        <div className="info-box">
          <p><strong>Fonte:</strong> BCB SGS</p>
          <p>Sem dados disponíveis para os indicadores complementares solicitados.</p>
        </div>
      </div>
    )
  }

  const last = (key: string) => {
    const pts = data.data[key]
    return Array.isArray(pts) && pts.length ? pts[pts.length - 1] : null
  }

  const formatVal = (key: string, val: string) => {
    const num = parseFloat(val.replace(',', '.'))
    const s = ALL_SERIES.find(x => x.key === key)
    if (s?.format === 'usd') {
      return `USD ${num.toLocaleString('pt-BR', { maximumFractionDigits: 0 })} mi`
    }
    if (s?.format === 'brl') {
      return `R$ ${num.toLocaleString('pt-BR', { maximumFractionDigits: 0 })}`
    }
    return num.toFixed(1)
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
              value={l ? formatVal(s.key, l.valor) : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <TimeSeriesChart
        data={merged}
        series={[SERIES[0]]}
        title="Reservas Internacionais"
        yLabel="USD milhões"
      />
      <TimeSeriesChart
        data={merged}
        series={AGREGADOS}
        title="Agregados Monetários (M0, M1, M2)"
        yLabel="R$ milhões"
      />
      <TimeSeriesChart
        data={merged}
        series={[SERIES[1]]}
        title="IC-Br Commodities"
      />
    </div>
  )
}

export default Complementares