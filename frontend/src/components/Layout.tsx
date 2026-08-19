import { Outlet, NavLink, useLocation } from 'react-router-dom'
import { RefreshCw, Activity, TrendingUp, BarChart3, DollarSign, FileText, Target, Layers, Landmark, Vote, Menu, X } from 'lucide-react'
import { useState, useEffect } from 'react'

const sections = [
  { path: '/', label: 'Dashboard', icon: BarChart3 },
  { path: '/eleicoes', label: 'Eleições', icon: Vote },
  { path: '/juros', label: 'Taxas de Juros', icon: TrendingUp },
  { path: '/inflacao', label: 'Inflação', icon: Activity },
  { path: '/ipca-decomposicao', label: 'IPCA Decomposição', icon: Layers },
  { path: '/atividade', label: 'Atividade Econômica', icon: Activity },
  { path: '/cambio', label: 'Câmbio', icon: DollarSign },
  { path: '/titulos', label: 'IMA-B', icon: FileText },
  { path: '/tesouro-direto', label: 'Tesouro Direto', icon: Landmark },
  { path: '/focus', label: 'Expectativas FOCUS', icon: Target },
  { path: '/complementares', label: 'Complementares', icon: Layers },
]

function formatLastUpdated(iso?: string | null): string {
  if (!iso) return ''
  const dt = new Date(iso)
  if (isNaN(dt.getTime())) return ''
  return dt.toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function Layout() {
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()

  useEffect(() => {
    setSidebarOpen(false)
  }, [location.pathname])

  useEffect(() => {
    let cancelled = false
    fetch('/api/status')
      .then(r => r.json())
      .then(body => { if (!cancelled) setLastUpdated(formatLastUpdated(body?.last_updated)) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const res = await fetch('/api/refresh', { method: 'POST' })
      const body = await res.json()
      const startedAt = body?.timestamp
      for (let i = 0; i < 120; i++) {
        await new Promise(r => setTimeout(r, 2000))
        try {
          const status = await (await fetch('/api/status')).json()
          if (status.last_updated && startedAt && status.last_updated >= startedAt) {
            setLastUpdated(formatLastUpdated(status.last_updated))
            break
          }
        } catch { /* keep polling */ }
      }
      window.location.reload()
    } catch (e) {
      console.error('Refresh failed:', e)
      setRefreshing(false)
    }
  }

  return (
    <div className="app-layout">
      <div
        className={`sidebar-backdrop ${sidebarOpen ? 'visible' : ''}`}
        onClick={() => setSidebarOpen(false)}
        aria-hidden="true"
      />
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <img src="/logo.png" alt="X-BRAY" className="logo-img" />
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
          <div className="header-left">
            <button
              className="menu-toggle"
              onClick={() => setSidebarOpen(o => !o)}
              aria-label={sidebarOpen ? 'Fechar menu' : 'Abrir menu'}
            >
              {sidebarOpen ? <X size={20} /> : <Menu size={20} />}
            </button>
            <h2>{sections.find(s => s.path === location.pathname)?.label || 'Dashboard'}</h2>
          </div>
          <span className="last-update">
            Dados: refresh diário 06:00 BRT
            {lastUpdated && (
              <span className="last-update-label">
                &nbsp;· Última atualização: {lastUpdated}
              </span>
            )}
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