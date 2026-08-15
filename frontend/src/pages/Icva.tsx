import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'

function Icva() {
  const { data, loading, error } = useFetch<any>('/api/icva')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  const raw = data.data.data || data.data

  const chartData = (raw || [])
    .filter((d: any) => d.year && d.month)
    .map((d: any) => ({
      date: `${d.year}-${String(d.month).padStart(2, '0')}-01`,
      nominal: d.nominal ?? null,
      real: d.real ?? null,
    }))
    .sort((a: any, b: any) => a.date.localeCompare(b.date))

  const series = [
    { key: 'nominal', name: 'ICVA Nominal (%)', color: '#ff6b6b' },
    { key: 'real', name: 'ICVA Real (%)', color: '#4ecdc4' },
  ]

  const latest = chartData.length > 0 ? chartData[chartData.length - 1] : null

  return (
    <div className="page">
      <div className="kpi-row">
        <div className="kpi-card" style={{ borderTopColor: '#ff6b6b' }}>
          <span className="kpi-label">ICVA Nominal</span>
          <span className="kpi-value">{latest?.nominal != null ? `${latest.nominal}%` : '—'}</span>
          <span className="kpi-date">{latest?.date || ''}</span>
        </div>
        <div className="kpi-card" style={{ borderTopColor: '#4ecdc4' }}>
          <span className="kpi-label">ICVA Real</span>
          <span className="kpi-value">{latest?.real != null ? `${latest.real}%` : '—'}</span>
          <span className="kpi-date">{latest?.date || ''}</span>
        </div>
        <div className="kpi-card" style={{ borderTopColor: '#a29bfe' }}>
          <span className="kpi-label">Meses</span>
          <span className="kpi-value">{chartData.length}</span>
        </div>
      </div>

      <TimeSeriesChart
        data={chartData}
        series={series}
        title="ICVA — Índice Cielo do Varejo Ampliado"
        yLabel="%"
        height={350}
      />

      <div className="info-box" style={{ marginTop: '1rem' }}>
        <p><strong>Fonte:</strong> Cielo — Blog Índice ICVA (blog.cielo.com.br/indice-icva)</p>
        <p>ICVA Nominal: crescimento da receita de vendas. ICVA Real: descontado da inflação (IPCA/IPCA-15).</p>
      </div>

      <div className="sectors-section" style={{ marginTop: '1.5rem' }}>
        <h3>Setores do ICVA</h3>
        <div className="sector-grid">
          {(data.data.sectors || []).map((s: string) => (
            <div key={s} className="sector-chip">{s}</div>
          ))}
        </div>
      </div>

      <div className="sectors-section">
        <h3>Macro-Setores</h3>
        {Object.entries(data.data.macro_sectors || {}).map(([macro, subs]) => (
          <div key={macro} className="macro-sector">
            <h4>{macro}</h4>
            <div className="sector-grid">
              {(subs as string[]).map(s => (
                <div key={s} className="sector-chip sub">{s}</div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default Icva
