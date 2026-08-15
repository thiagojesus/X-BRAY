import { useFetch } from '../hooks/useFetch'
import { Loading, ErrorDisplay } from '../components/Status'

function Icva() {
  const { data, loading, error } = useFetch<any>('/api/icva')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" }

  const icva = data.data

  return (
    <div className="page">
      <div className="info-box">
        <p><strong>Fonte:</strong> Cielo — Índice de Consumo e Vendas no Varejo (ICVA)</p>
        <p>Dados de 18 setores e 3 macro-setores. Dados históricos disponíveis em ri.cielo.com.br.</p>
      </div>

      <div className="kpi-row">
        <div className="kpi-card" style={{ borderTopColor: '#ff6b6b' }}>
          <span className="kpi-label">Setores</span>
          <span className="kpi-value">{icva.sectors?.length || 18}</span>
        </div>
        <div className="kpi-card" style={{ borderTopColor: '#4ecdc4' }}>
          <span className="kpi-label">Macro-Setores</span>
          <span className="kpi-value">{Object.keys(icva.macro_sectors || {}).length}</span>
        </div>
      </div>

      <div className="sectors-section">
        <h3>Setores do ICVA</h3>
        <div className="sector-grid">
          {(icva.sectors || []).map((s: string) => (
            <div key={s} className="sector-chip">{s}</div>
          ))}
        </div>
      </div>

      <div className="sectors-section">
        <h3>Macro-Setores</h3>
        {Object.entries(icva.macro_sectors || {}).map(([macro, subs]) => (
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

      <div className="info-box" style={{ marginTop: '1.5rem' }}>
        <p><strong>Nota:</strong> O ICVA é publicado mensalmente no blog da Cielo. Dados detalhados por setor estão disponíveis no relatório de inteligência da Cielo.</p>
      </div>
    </div>
  )
}

export default Icva
