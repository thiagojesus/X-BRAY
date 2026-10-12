import { Activity, Calculator, CreditCard, DollarSign, Gauge, Globe2, Landmark, Layers, Scale, Target, TrendingUp, Vote } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ErrorDisplay, Loading } from '../components/Status'
import { useFetch } from '../hooks/useFetch'

type StatusResponse = {
  readonly status?: string
  readonly db_stats?: Readonly<Record<string, unknown>>
}

function Dashboard() {
  const { data, loading, error } = useFetch<StatusResponse>('/api/status')

  const storedRecords = Object.values(data?.db_stats ?? {}).reduce<number>((total, value) => {
    return typeof value === 'number' && Number.isFinite(value) ? total + value : total
  }, 0)

  const sections = [
    { path: '/derivados', label: 'Indicadores Derivados', desc: 'Juro real ex-ante de um ano', icon: Calculator, color: '#4ecdc4' },
    { path: '/eleicoes', label: 'Eleições', desc: 'Presidencial 2026, Polymarket', icon: Vote, color: '#4ecdc4' },
    { path: '/juros', label: 'Taxas de Juros', desc: 'Selic, CDI, TR', icon: TrendingUp, color: '#ff6b6b' },
    { path: '/inflacao', label: 'Inflação', desc: 'IPCA, INPC, IGP-M', icon: Activity, color: '#ffa502' },
    { path: '/ipca-decomposicao', label: 'IPCA Decomposição', desc: 'Grupos, Naturezas, Core', icon: Layers, color: '#a29bfe' },
    { path: '/atividade', label: 'Atividade Econômica', desc: 'PIB, IBC-Br, Desemprego', icon: Activity, color: '#4ecdc4' },
    { path: '/fiscal', label: 'Fiscal', desc: 'DBGG, DLSP e resultados fiscais', icon: Scale, color: '#ffd166' },
    { path: '/credito', label: 'Crédito e Famílias', desc: 'Juros, orçamento e inadimplência', icon: CreditCard, color: '#74b9ff' },
    { path: '/setor-externo', label: 'Setor Externo', desc: 'Conta corrente, fluxos e reservas', icon: Globe2, color: '#4ecdc4' },
    { path: '/cambio', label: 'Câmbio', desc: 'PTAX USD, EUR', icon: DollarSign, color: '#fd79a8' },
    { path: '/titulos', label: 'Títulos Públicos', desc: 'IMA ANBIMA', icon: Layers, color: '#00cec9' },
    { path: '/tesouro-direto', label: 'Tesouro Direto', desc: 'Prefixado e IPCA+', icon: Landmark, color: '#f6c445' },
    { path: '/focus', label: 'Expectativas FOCUS', desc: 'Projeções de mercado', icon: Target, color: '#6c5ce7' },
    { path: '/complementares', label: 'Complementares', desc: 'IC-Br e agregados M0/M1/M2', icon: Layers, color: '#00b894' },
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
          <span>Caches: {storedRecords} registros armazenados</span>
        </div>
      )}

      <div className="dashboard-grid">
        <Link to="/macro-score" className="dashboard-card">
          <Gauge size={24} aria-hidden="true" />
          <div><h3>Macro Score Beta</h3><p>Seis dimensões, cobertura e insumos explicáveis</p></div>
        </Link>
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
