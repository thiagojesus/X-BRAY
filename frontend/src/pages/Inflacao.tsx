import { useState, useMemo } from 'react'
import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { KpiCard } from '../components/KpiCard'

const ALL_SERIES = [
  { key: 'ipca', name: 'IPCA Mensal', color: '#ff6b6b', format: 'pct' as const, desc: 'Índice Nacional de Preços ao Consumidor Amplo — variação mensal dos preços de uma cesta de bens e serviços, principal referência de inflação do país.' },
  { key: 'ipca_12m', name: 'IPCA 12m', color: '#ffa502', format: 'pct' as const, desc: 'IPCA acumulado nos últimos 12 meses — indica a inflação anualizada percebida pelo consumidor.' },
  { key: 'ipca_15', name: 'IPCA 15 dias', color: '#a29bfe', format: 'pct' as const, desc: 'IPCA 15 dias — preview do mês atual com alta antecedência; cobre ~80% da cesta do IPCA oficial.' },
  { key: 'inpc', name: 'INPC', color: '#4ecdc4', format: 'pct' as const, desc: 'Índice Nacional de Preços ao Consumidor — foca em famílias com renda de 1 a 5 salários mínimos.' },
  { key: 'igpm', name: 'IGP-M', color: '#fd79a8', format: 'pct' as const, desc: 'Índice Geral de Preços do Mercado — captura preços no atacado, consumidor e construção civil.' },
  { key: 'igpdi', name: 'IGP-DI', color: '#00cec9', format: 'pct' as const, desc: 'IGP-DI — medido no mês seguinte ao de referência; usado para reajustes de aluguéis e contratos.' },
  { key: 'incc_di', name: 'INCC-DI', color: '#6c5ce7', format: 'pct' as const, desc: 'Índice Nacional de Custo da Construção — reflete inflação do setor de construção civil; base para atualização de FGTS e financiamentos imobiliários.' },
]

function Inflacao() {
  const { data, loading, error } = useFetch<any>('/api/inflacao')
  const [enabledKeys, setEnabledKeys] = useState<Set<string>>(new Set())

  const merged = useMemo(() => {
    if (!data?.data) return []
    const result: Record<string, any>[] = []
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
      result.push(row)
    }
    return result
  }, [data])

  const activeKeys = useMemo(() => {
    return enabledKeys.size > 0 ? Array.from(enabledKeys) : ALL_SERIES.map(s => s.key)
  }, [enabledKeys])

  const series = useMemo(() =>
    activeKeys.map(k => ALL_SERIES.find(s => s.key === k)!).filter(Boolean),
    [activeKeys]
  )

  const toggleKey = (key: string) => {
    setEnabledKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const selectAll = () => setEnabledKeys(new Set(ALL_SERIES.map(s => s.key)))
  const selectNone = () => setEnabledKeys(new Set())

  const last = (key: string) => {
    const pts = data?.data?.[key]
    return Array.isArray(pts) && pts.length ? pts[pts.length - 1] : null
  }

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  return (
    <div className="page">
      <div className="kpi-row">
        {ALL_SERIES.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? `${l.valor}%` : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <div className="series-controls">
        <div className="series-toggles">
          <button className="toggle-action" onClick={selectAll}>Todos</button>
          <button className="toggle-action" onClick={selectNone}>Nenhum</button>
          {ALL_SERIES.map(s => (
            <label key={s.key} className={`series-toggle ${enabledKeys.has(s.key) || enabledKeys.size === 0 ? 'active' : ''}`}>
              <input
                type="checkbox"
                checked={enabledKeys.has(s.key) || enabledKeys.size === 0}
                onChange={() => toggleKey(s.key)}
              />
              <span className="toggle-swatch" style={{ background: s.color }} />
              <span>{s.name}</span>
            </label>
          ))}
        </div>
      </div>
      <TimeSeriesChart
        data={merged}
        series={series}
        title="Inflação — Histórico"
        yLabel="% a.m."
        height={400}
      />
    </div>
  )
}

export default Inflacao
