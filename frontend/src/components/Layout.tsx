import { Outlet, NavLink, useLocation } from 'react-router-dom'
import { RefreshCw, Activity, TrendingUp, BarChart3, DollarSign, FileText, Target, Layers } from 'lucide-react'
import { useState } from 'react'

const sections = [
  { path: '/', label: 'Dashboard', icon: BarChart3 },
  { path: '/juros', label: 'Taxas de Juros', icon: TrendingUp },
  { path: '/inflacao', label: 'Inflação', icon: Activity },
  { path: '/ipca-decomposicao', label: 'IPCA Decomposição', icon: Layers },
  { path: '/atividade', label: 'Atividade Econômica', icon: Activity },
  { path: '/cambio', label: 'Câmbio', icon: DollarSign },
  { path: '/titulos', label: 'Títulos Públicos', icon: FileText },
  { path: '/focus', label: 'Expectativas FOCUS', icon: Target },
  { path: '/complementares', label: 'Complementares', icon: Layers },
]

function Layout() {
  const [refreshing, setRefreshing] = useState(false)
  const location = useLocation()

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      await fetch('/api/refresh', { method: 'POST' })
      window.location.reload()
    } catch (e) {
      console.error('Refresh failed:', e)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1 className="logo">X-BRAY</h1>
          <p className="logo-sub">Macro Brasil</p>
        </div>
        <nav className="sidebar-nav">
          {sections.map((s) => {
            const Icon = s.icon
            return (
              <NavLink
                key={s.path}
                to={s.path}
                end={s.path === '/'}
                className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
              >
                <Icon size={18} />
                <span>{s.label}</span>
              </NavLink>
            )
          })}
        </nav>
        <div className="sidebar-footer">
          <button
            className="refresh-btn"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <RefreshCw size={16} className={refreshing ? 'spin' : ''} />
            {refreshing ? 'Atualizando...' : 'Refresh Agora'}
          </button>
        </div>
      </aside>
      <main className="main-content">
        <header className="main-header">
          <h2>{sections.find(s => s.path === location.pathname)?.label || 'Dashboard'}</h2>
          <span className="last-update">
            Dados: refresh diário 06:00 BRT
          </span>
        </header>
        <div className="page-content">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

export default Layout
