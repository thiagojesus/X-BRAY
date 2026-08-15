import { useState } from 'react'
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
  core_ex1: '#ff6b6b',
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
  const { data, loading, error } = useFetch<any>('/api/ipca-decomposicao/tudo')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data) return <ErrorDisplay message="Sem dados" />

  const raw = data[tab] || {}
  const merged = mergeData(raw)
  const colors = COLOR_MAP[tab] || {}
  const series = Object.keys(colors).map(k => ({
    key: k,
    name: k.replace(/_/g, ' '),
    color: colors[k],
  }))

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
    core_ex1: 'Core EX1',
    core_medias_aparadas: 'Médias Aparadas',
    core_dp: 'DP',
    itens_livres: 'Itens Livres',
    transacionaveis: 'Transacionáveis',
    nao_transacionaveis: 'Não Transacionáveis',
    administrados: 'Administrados',
  }

  return (
    <div className="page">
      <div className="tabs">
        {TABS.map(t => (
          <button key={t.key} className={`tab ${tab === t.key ? 'active' : ''}`} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      <TimeSeriesChart
        data={merged}
        series={series.map(s => ({ ...s, name: LABELS[s.key] || s.name }))}
        title={`IPCA — ${TABS.find(t => t.key === tab)?.label}`}
        yLabel="% a.m."
        height={400}
      />
    </div>
  )
}

export default IpcaDecomposicao
