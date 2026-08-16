import { useState, useMemo } from 'react'
import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'

const TABS = [
  { key: 'grupos', label: 'Grupos de Despesa' },
  { key: 'naturezas', label: 'Naturezas' },
  { key: 'core', label: 'Core' },
  { key: 'precos', label: 'Livres vs Administrados' },
]

const GROUP_COLORS: Record<string, string> = {
  alimentacao_bebidas: '#ff6b6b',
  habitacao: '#ffa502',
  artigos_residencia: '#a29bfe',
  vestuario: '#4ecdc4',
  transportes: '#fd79a8',
  comunicacao: '#00cec9',
  saude_cuidados: '#6c5ce7',
  despesas_pessoais: '#ffeaa7',
}

const NATURE_COLORS: Record<string, string> = {
  bens_duraveis: '#ff6b6b',
  bens_semi_duraveis: '#ffa502',
  bens_nao_duraveis: '#4ecdc4',
  servicos: '#a29bfe',
}

const CORE_COLORS: Record<string, string> = {
  core_medias_aparadas: '#ffa502',
  core_dp: '#a29bfe',
}

const PRICE_COLORS: Record<string, string> = {
  itens_livres: '#4ecdc4',
  transacionaveis: '#ffa502',
  nao_transacionaveis: '#a29bfe',
  administrados: '#ff6b6b',
}

const COLOR_MAP: Record<string, Record<string, string>> = {
  grupos: GROUP_COLORS,
  naturezas: NATURE_COLORS,
  core: CORE_COLORS,
  precios: PRICE_COLORS,
}

const LABELS: Record<string, string> = {
  alimentacao_bebidas: 'Alimentação e Bebidas',
  habitacao: 'Habitação',
  artigos_residencia: 'Artigos de Residência',
  vestuario: 'Vestuário',
  transportes: 'Transportes',
  comunicacao: 'Comunicação',
  saude_cuidados: 'Saúde e Cuidados Pessoais',
  despesas_pessoais: 'Despesas Pessoais',
  bens_duraveis: 'Bens Duráveis',
  bens_semi_duraveis: 'Bens Semiduráveis',
  bens_nao_duraveis: 'Bens Não Duráveis',
  servicos: 'Serviços',
  core_medias_aparadas: 'Médias Aparadas',
  core_dp: 'DP',
  itens_livres: 'Itens Livres',
  transacionaveis: 'Transacionáveis',
  nao_transacionaveis: 'Não Transacionáveis',
  administrados: 'Administrados',
}

function mergeData(raw: Record<string, any[]>) {
  const merged: Record<string, any>[] = []
  const allDates = new Set<string>()
  for (const points of Object.values(raw)) {
    if (Array.isArray(points)) for (const p of points) allDates.add(p.data)
  }
  const sorted = Array.from(allDates).sort((a, b) => {
    const [da, ma, ya] = a.split('/').map(Number)
    const [db, mb, yb] = b.split('/').map(Number)
    return ya * 10000 + ma * 100 + da - (yb * 10000 + mb * 100 + db)
  })
  for (const date of sorted) {
    const row: Record<string, any> = { date }
    for (const [name, points] of Object.entries(raw)) {
      if (Array.isArray(points)) {
        const m = points.find((p: any) => p.data === date)
        if (m) row[name] = parseFloat(m.valor.replace(',', '.'))
      }
    }
    merged.push(row)
  }
  return merged
}

function IpcaDecomposicao() {
  const [tab, setTab] = useState('grupos')
  const [showIpcaTotal, setShowIpcaTotal] = useState(false)
  const [enabledKeys, setEnabledKeys] = useState<Set<string>>(new Set())
  const { data, loading, error } = useFetch<any>('/api/ipca-decomposicao/tudo')
  const { data: ipcaData } = useFetch<any>('/api/inflacao/ipca')

  const colors = COLOR_MAP[tab] || {}
  const allKeys = Object.keys(colors)

  const merged = useMemo(() => {
    if (!data) return []
    const raw = data[tab] || {}
    return mergeData(raw)
  }, [data, tab])

  const activeKeys = useMemo(() => {
    const keys = enabledKeys.size > 0 ? enabledKeys : new Set(allKeys)
    return Array.from(keys)
  }, [enabledKeys, allKeys])

  const series = useMemo(() => {
    const s = activeKeys.map(k => ({
      key: k,
      name: LABELS[k] || k.replace(/_/g, ' '),
      color: colors[k] || '#999',
      format: 'pct' as const,
    }))
    if (showIpcaTotal && ipcaData?.data) {
      s.push({ key: 'ipca_total', name: 'IPCA Total', color: '#ffffff', format: 'pct' as const })
    }
    return s
  }, [activeKeys, colors, showIpcaTotal, ipcaData])

  const chartData = useMemo(() => {
    if (!showIpcaTotal || !ipcaData?.data) return merged
    const ipcaMap = new Map<string, number>()
    for (const p of ipcaData.data) {
      ipcaMap.set(p.data, parseFloat(p.valor.replace(',', '.')))
    }
    return merged.map(row => ({
      ...row,
      ipca_total: ipcaMap.get(row.date) ?? null,
    }))
  }, [merged, showIpcaTotal, ipcaData])

  const toggleKey = (key: string) => {
    setEnabledKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const selectAll = () => setEnabledKeys(new Set(allKeys))
  const selectNone = () => setEnabledKeys(new Set())

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data) return <ErrorDisplay message="Sem dados" />

  return (
    <div className="page">
      <div className="tabs">
        {TABS.map(t => (
          <button key={t.key} className={`tab ${tab === t.key ? 'active' : ''}`} onClick={() => { setTab(t.key); setEnabledKeys(new Set()) }}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="series-controls">
        <div className="series-toggles">
          <button className="toggle-action" onClick={selectAll}>Todos</button>
          <button className="toggle-action" onClick={selectNone}>Nenhum</button>
          {allKeys.map(k => (
            <label key={k} className={`series-toggle ${enabledKeys.has(k) || enabledKeys.size === 0 ? 'active' : ''}`}>
              <input
                type="checkbox"
                checked={enabledKeys.has(k) || enabledKeys.size === 0}
                onChange={() => toggleKey(k)}
              />
              <span className="toggle-swatch" style={{ background: colors[k] }} />
              <span>{LABELS[k] || k.replace(/_/g, ' ')}</span>
            </label>
          ))}
          <label className={`series-toggle ipca-total-toggle ${showIpcaTotal ? 'active' : ''}`}>
            <input type="checkbox" checked={showIpcaTotal} onChange={() => setShowIpcaTotal(!showIpcaTotal)} />
            <span className="toggle-swatch" style={{ background: '#ffffff' }} />
            <span>IPCA Total</span>
          </label>
        </div>
      </div>
      <TimeSeriesChart
        data={chartData}
        series={series}
        title={`IPCA — ${TABS.find(t => t.key === tab)?.label}`}
        yLabel="% a.m."
        height={400}
      />
    </div>
  )
}

export default IpcaDecomposicao
