import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { DataQualityNotice } from '../components/DataQualityNotice'

type FocusPoint = {
  readonly Data: string
  readonly Mediana?: number | string | null
  readonly Media?: number | string | null
  readonly Indicador?: string | null
}

type FocusResponse = {
  readonly data?: Readonly<Record<string, readonly FocusPoint[]>> | null
}

function Focus() {
  const { data, loading, error, isStale } = useFetch<FocusResponse>('/api/focus')

  if (loading) return <Loading />
  if (error && !data) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />
  const focusData = data.data

  const indicators = Object.keys(focusData).filter(k => {
    const v = focusData[k]
    return Array.isArray(v) && v.length > 0
  })

  return (
    <div className="page">
      <DataQualityNotice dataset="focus" isStale={isStale} />
      <div className="info-box">
        <p><strong>Fonte:</strong> BCB — Expectativas de Mercado (FOCUS OData API)</p>
        <p>Dados coletados das expectativas anuais dos principais indicadores macroeconômicos.</p>
      </div>
      <div className="focus-grid">
        {indicators.map(ind => {
          const points = focusData[ind]
          const sorted = [...points].sort((a, b) => {
            const da = new Date(a.Data).getTime()
            const db = new Date(b.Data).getTime()
            return da - db
          })

          const latest = sorted[sorted.length - 1]
          const chartData = sorted.map(p => ({
            date: new Date(p.Data).toISOString().slice(0, 10),
            value: p.Mediana ?? p.Indicador ? parseFloat(String(p.Mediana || p.Media || 0)) : 0,
          })).filter(p => p.value !== 0)

          const fmt = ind === 'Câmbio' ? 'brl' as const : 'pct' as const
          const series = [{ key: 'value', name: `Mediana ${ind}`, color: ind === 'IPCA' ? '#ff6b6b' : ind === 'Selic' ? '#ffa502' : ind === 'PIB' ? '#4ecdc4' : ind === 'Câmbio' ? '#a29bfe' : '#fd79a8', format: fmt }]

          return (
            <div key={ind} className="focus-card">
              <div className="focus-header">
                <h3>{ind}</h3>
                {latest && (
                  <span className="focus-latest">
                    Mediana: {latest.Mediana ?? '—'}% | Consenso: {new Date(latest.Data).toLocaleDateString('pt-BR')}
                  </span>
                )}
              </div>
              <TimeSeriesChart data={chartData} series={series} height={220} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default Focus
