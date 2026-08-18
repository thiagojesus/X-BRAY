import { useFetch } from '../hooks/useFetch'
import { Loading, ErrorDisplay } from '../components/Status'
import { Link } from 'react-router-dom'
import { TrendingUp, Activity, DollarSign, Target, Layers, Landmark } from 'lucide-react'

function Dashboard() {
  const { data, loading, error } = useFetch<any>('/api/status')

  const sections = [
    { path: '/juros', label: 'Taxas de Juros', desc: 'Selic, CDI, TR', icon: TrendingUp, color: '#ff6b6b' },
    { path: '/inflacao', label: 'Inflação', desc: 'IPCA, INPC, IGP-M', icon: Activity, color: '#ffa502' },
    { path: '/ipca-decomposicao', label: 'IPCA Decomposição', desc: 'Grupos, Naturezas, Core', icon: Layers, color: '#a29bfe' },
    { path: '/atividade', label: 'Atividade Econômica', desc: 'PIB, IBC-Br, Desemprego', icon: Activity, color: '#4ecdc4' },
    { path: '/cambio', label: 'Câmbio', desc: 'PTAX USD, EUR', icon: DollarSign, color: '#fd79a8' },
    { path: '/titulos', label: 'Títulos Públicos', desc: 'IMA ANBIMA', icon: Layers, color: '#00cec9' },
    { path: '/tesouro-direto', label: 'Tesouro Direto', desc: 'Prefixado e IPCA+', icon: Landmark, color: '#f6c445' },
    { path: '/focus', label: 'Expectativas FOCUS', desc: 'Projeções de mercado', icon: Target, color: '#6c5ce7' },
    { path: '/complementares', label: 'Complementares', desc: 'Reservas, Base Monetária', icon: Layers, color: '#00b894' },
  ]

  return (
    <div className="page dashboard">
      <div className="hero">
        <img src="/logo.png" alt="X-BRAY" className="hero-logo" />
        <p>Raio-X do Macro Brasileiro</p>
        <p className="hero-sub">Indicadores econômicos do Brasil em tempo real — dados do BCB, ANBIMA e Tesouro Direto.</p>
      </div>

      {loading && <Loading />}
      {error && <ErrorDisplay message={error} />}
      {data && (
        <div className="status-bar">
          <span>API: {data.status === 'running' ? '🟢 Online' : '🔴 Offline'}</span>
          <span>Caches: {Object.keys(data.cache || {}).length} séries armazenadas</span>
        </div>
      )}

      <div className="dashboard-grid">
        {sections.map(s => {
          const Icon = s.icon
          return (
            <Link key={s.path} to={s.path} className="dashboard-card" style={{ borderLeftColor: s.color }}>
              <Icon size={24} color={s.color} />
              <div>
                <h3>{s.label}</h3>
                <p>{s.desc}</p>
              </div>
            </Link>
          )
        })}
      </div>
    </div>
  )
}

export default Dashboard
