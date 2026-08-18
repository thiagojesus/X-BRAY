import { useState, useMemo } from 'react'
import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'

interface ImabPoint {
  data: string
  indice: number | null
  variacao_diaria: number | null
  variacao_12m: number | null
  duration: number | null
  pmr: number | null
}

function Titulos() {
  const { data, loading, error } = useFetch<any>('/api/titulos/nntn-b')
  const [showReturn, setShowReturn] = useState(false)

  const chartData = useMemo(() => {
    if (!data?.data) return []
    return data.data
      .filter((p: ImabPoint) => p.indice !== null)
      .map((p: ImabPoint) => ({
        date: p.data,
        indice: p.indice,
        variacao_12m: p.variacao_12m,
        duration: p.duration,
      }))
  }, [data])

  const last = chartData.length > 0 ? chartData[chartData.length - 1] : null

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data || chartData.length === 0) return <ErrorDisplay message="Sem dados disponíveis" />

  return (
    <div className="page">
      <div className="info-box">
        <p><strong>Fonte:</strong> ANBIMA — Índices de Mercado (IMA) Histórico</p>
        <p>{data.description}</p>
      </div>

      {last && (
        <div className="kpi-row">
          <div className="kpi-card" style={{ borderLeft: '4px solid #ff6b6b' }}>
            <div className="kpi-name">IMA-B (Índice)</div>
            <div className="kpi-value" style={{ color: '#ff6b6b' }}>
              {last.indice?.toLocaleString('pt-BR', { minimumFractionDigits: 4, maximumFractionDigits: 4 })}
            </div>
            <div className="kpi-date">{last.date}</div>
          </div>
          {last.variacao_12m !== null && (
            <div className="kpi-card" style={{ borderLeft: '4px solid #4ecdc4' }}>
              <div className="kpi-name">Retorno 12 meses</div>
              <div className="kpi-value" style={{ color: '#4ecdc4' }}>
                {last.variacao_12m.toFixed(2)}%
              </div>
              <div className="kpi-date">{last.date}</div>
            </div>
          )}
          {last.duration !== null && (
            <div className="kpi-card" style={{ borderLeft: '4px solid #ffa502' }}>
              <div className="kpi-name">Duration</div>
              <div className="kpi-value" style={{ color: '#ffa502' }}>
                {last.duration} d.u.
              </div>
              <div className="kpi-date">{last.date}</div>
            </div>
          )}
        </div>
      )}

      <div className="series-controls">
        <div className="series-toggles">
          <label className={`series-toggle ${!showReturn ? 'active' : ''}`}>
            <input
              type="radio"
              name="imab-view"
              checked={!showReturn}
              onChange={() => setShowReturn(false)}
            />
            <span>Índice</span>
          </label>
          <label className={`series-toggle ${showReturn ? 'active' : ''}`}>
            <input
              type="radio"
              name="imab-view"
              checked={showReturn}
              onChange={() => setShowReturn(true)}
            />
            <span>Retorno 12m</span>
          </label>
        </div>
      </div>

      {!showReturn ? (
        <TimeSeriesChart
          data={chartData}
          series={[{ key: 'indice', name: 'IMA-B', color: '#ff6b6b', format: 'idx' as const }]}
          title="IMA-B — Evolução do Índice"
          height={400}
        />
      ) : (
        <TimeSeriesChart
          data={chartData}
          series={[{ key: 'variacao_12m', name: 'Retorno 12m (%)', color: '#4ecdc4', format: 'pct' as const }]}
          title="IMA-B — Retorno acumulado 12 meses"
          yLabel="%"
          height={400}
        />
      )}
    </div>
  )
}

export default Titulos
