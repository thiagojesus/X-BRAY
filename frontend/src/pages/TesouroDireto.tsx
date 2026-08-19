import { useState, useMemo, useEffect } from 'react'
import { useFetch } from '../hooks/useFetch'
import { Loading, ErrorDisplay } from '../components/Status'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { buildXTicks, makeAdaptiveTickFormatter } from '../charts/TimeSeriesChart'

interface Bond {
  symbol: string
  name: string
  indexer: string
  maturityDate: string
  durationDays: number
  maturityLabel: string
  buyRate: number | null
  sellRate: number | null
  buyPrice: number | null
  sellPrice: number | null
  basePrice: number | null
  couponType: string
  rateInfo: {
    rateType?: string
    rateUnit?: string
    description?: string
  }
}

interface CatalogBond {
  code: number
  name: string
  indexer: string
  targetYear: number | null
  couponType: string
  available: boolean
}

interface HistoryPoint {
  date: string
  buyRate: number | null
  sellRate: number | null
  buyPrice: number | null
  sellPrice: number | null
}

const INDEXERS = [
  { key: 'prefixado', label: 'Pré-fixado', color: '#ff6b6b', description: 'Taxa nominal contratada (ex.: 14% a.a.)' },
  { key: 'ipca', label: 'IPCA+', color: '#4ecdc4', description: 'Taxa real acima da inflação (ex.: 7% a.a. + IPCA)' },
]

const MATURITY_FILTERS = [
  { key: 'all', label: 'Todos' },
  { key: 'Curto (~2 anos)', label: 'Curto (~2 anos)' },
  { key: 'Médio (~5 anos)', label: 'Médio (~5 anos)' },
  { key: 'Longo (~10 anos)', label: 'Longo (~10 anos)' },
  { key: 'Muito Longo (~15 anos)', label: 'Muito Longo (~15 anos)' },
  { key: 'Ultra Longo (20+ anos)', label: 'Ultra Longo (20+)' },
]

const HISTORY_PERIODS = [
  { key: 30, label: 'Últimos 30 dias' },
  { key: 90, label: 'Últimos 90 dias' },
  { key: 180, label: 'Últimos 6 meses' },
  { key: 365, label: 'Últimos 12 meses' },
  { key: 0, label: 'Todo o período' },
]

const BOND_COLORS = ['#4ecdc4', '#ff6b6b', '#74b9ff', '#fdcb6e', '#a29bfe']
const MAX_COMPARE = 5

interface CompareHistory {
  code: number
  name: string
  maturityDate: string
  points: HistoryPoint[]
}

function parseDateKey(date: string): number {
  const [d, m, y] = date.split('/').map(Number)
  return y * 10000 + m * 100 + d
}

function bondLabel(b: CatalogBond): string {
  return `${b.name} ${b.targetYear ?? ''}`.trim()
}

function CompareChart() {
  const { data: catalogData, loading: catalogLoading } = useFetch<any>('/api/tesouro-direto/titulos')
  const catalog = useMemo(() => {
    const items: CatalogBond[] = catalogData?.data || []
    return items
      .filter(b => (b.indexer === 'prefixado' || b.indexer === 'ipca') && b.available !== false)
      .sort((a, b) => {
        const byIndexer = INDEXERS.findIndex(i => i.key === a.indexer) - INDEXERS.findIndex(i => i.key === b.indexer)
        if (byIndexer !== 0) return byIndexer
        return (a.targetYear ?? 0) - (b.targetYear ?? 0)
      })
  }, [catalogData])

  const [selectedCodes, setSelectedCodes] = useState<number[]>([])
  const [period, setPeriod] = useState<number>(30)
  const [metric, setMetric] = useState<'taxa' | 'preco'>('taxa')
  const [operation, setOperation] = useState<'buy' | 'sell'>('buy')

  useEffect(() => {
    if (selectedCodes.length > 0 || catalog.length === 0) return
    const prefixado = catalog
      .filter(b => b.indexer === 'prefixado')
      .sort((a, b) => (a.targetYear ?? 0) - (b.targetYear ?? 0))
    const pool = prefixado.length >= 2 ? prefixado : catalog
    const idxs = [...new Set([0, Math.floor(pool.length / 2), pool.length - 1])]
    setSelectedCodes(idxs.map(i => pool[i].code))
  }, [catalog, selectedCodes])

  const toggleBond = (code: number) => {
    setSelectedCodes(prev => {
      if (prev.includes(code)) return prev.filter(c => c !== code)
      if (prev.length >= MAX_COMPARE) return prev
      return [...prev, code]
    })
  }

  const compareUrl = selectedCodes.length > 0
    ? `/api/tesouro-direto/comparar?codes=${selectedCodes.join(',')}&days=${period}`
    : null

  const { data: compareData, loading: compareLoading, error: compareError } = useFetch<any>(compareUrl ?? '')

  const histories: CompareHistory[] = useMemo(() => {
    const list = compareData?.data
    return Array.isArray(list) ? list : []
  }, [compareData])

  const chartData = useMemo(() => {
    const byDate = new Map<string, Record<string, any>>()
    for (const h of histories) {
      const key = `b${h.code}`
      for (const p of h.points) {
        if (!byDate.has(p.date)) byDate.set(p.date, { date: p.date })
        const row = byDate.get(p.date)!
        row[key] = metric === 'taxa'
          ? (operation === 'buy' ? p.buyRate : p.sellRate)
          : (operation === 'buy' ? p.buyPrice : p.sellPrice)
      }
    }
    return [...byDate.values()].sort((a, b) => parseDateKey(a.date) - parseDateKey(b.date))
  }, [histories, metric, operation])

  const series = useMemo(() => {
    return histories.map((h, i) => {
      const bond = catalog.find(b => b.code === h.code)
      return {
        key: `b${h.code}`,
        name: bond ? bondLabel(bond) : `${h.name} ${h.maturityDate}`,
        color: BOND_COLORS[i % BOND_COLORS.length],
      }
    })
  }, [histories, catalog])

  const inversion = useMemo(() => {
    if (metric !== 'taxa' || selectedCodes.length < 2) return null
    const bonds = selectedCodes
      .map(code => catalog.find(b => b.code === code))
      .filter((b): b is CatalogBond => Boolean(b))
    if (bonds.length < 2) return null
    if (new Set(bonds.map(b => b.indexer)).size !== 1) return null
    const sorted = [...bonds].sort((a, b) => (a.targetYear ?? 0) - (b.targetYear ?? 0))
    const short = sorted[0]
    const long = sorted[sorted.length - 1]
    const hShort = histories.find(h => h.code === short.code)
    const hLong = histories.find(h => h.code === long.code)
    if (!hShort?.points.length || !hLong?.points.length) return null
    const lastShort = hShort.points[hShort.points.length - 1]
    const lastLong = hLong.points[hLong.points.length - 1]
    const rateShort = operation === 'buy' ? lastShort.buyRate : lastShort.sellRate
    const rateLong = operation === 'buy' ? lastLong.buyRate : lastLong.sellRate
    if (rateShort == null || rateLong == null) return null
    const spread = rateShort - rateLong
    return {
      inverted: spread > 0,
      spread,
      shortLabel: bondLabel(short),
      longLabel: bondLabel(long),
      rateShort,
      rateLong,
    }
  }, [catalog, histories, metric, operation, selectedCodes])

  const yLabel = metric === 'taxa' ? '% a.a.' : 'Preço (R$)'

  const fmt = (value: number) => {
    if (metric === 'taxa') return `${value.toFixed(2)}%`
    return `R$ ${value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }

  const tooltipFormatter = (value: any, name: string) => {
    const num = parseFloat(value)
    if (isNaN(num)) return [value, name]
    return [fmt(num), name]
  }

  const { ticks: xTicks, period: xTickPeriod } = useMemo(() => buildXTicks(chartData), [chartData])
  const xTickFormatter = makeAdaptiveTickFormatter(xTickPeriod)

  if (catalogLoading) return <Loading />
  if (catalog.length === 0) return <ErrorDisplay message="Sem dados disponíveis" />

  return (
    <div className="chart-container">
      <h3 className="chart-title">Comparar Títulos — Taxas e Preços</h3>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Títulos ({selectedCodes.length}/{MAX_COMPARE}):</span>
          {catalog.map(b => {
            const selected = selectedCodes.includes(b.code)
            const disabled = !selected && selectedCodes.length >= MAX_COMPARE
            return (
              <label key={b.code} className={`series-toggle ${selected ? 'active' : ''}`}>
                <input
                  type="checkbox"
                  checked={selected}
                  disabled={disabled}
                  onChange={() => toggleBond(b.code)}
                />
                <span style={{ color: b.indexer === 'ipca' ? '#4ecdc4' : '#ff6b6b' }}>{bondLabel(b)}</span>
              </label>
            )
          })}
        </div>
      </div>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Período:</span>
          {HISTORY_PERIODS.map(p => (
            <label key={p.key} className={`series-toggle ${period === p.key ? 'active' : ''}`}>
              <input type="radio" name="period" checked={period === p.key} onChange={() => setPeriod(p.key)} />
              <span>{p.label}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Metrica:</span>
          <label className={`series-toggle ${metric === 'taxa' ? 'active' : ''}`}>
            <input type="radio" name="metric" checked={metric === 'taxa'} onChange={() => setMetric('taxa')} />
            <span>Taxa</span>
          </label>
          <label className={`series-toggle ${metric === 'preco' ? 'active' : ''}`}>
            <input type="radio" name="metric" checked={metric === 'preco'} onChange={() => setMetric('preco')} />
            <span>Preço</span>
          </label>
          <span className="toggle-label">Operação:</span>
          <label className={`series-toggle ${operation === 'buy' ? 'active' : ''}`}>
            <input type="radio" name="operation" checked={operation === 'buy'} onChange={() => setOperation('buy')} />
            <span>Investimento</span>
          </label>
          <label className={`series-toggle ${operation === 'sell' ? 'active' : ''}`}>
            <input type="radio" name="operation" checked={operation === 'sell'} onChange={() => setOperation('sell')} />
            <span>Resgate</span>
          </label>
        </div>
      </div>

      {inversion && (
        <div className={`compare-badge ${inversion.inverted ? 'inverted' : 'normal'}`} role="status">
          {inversion.inverted
            ? `Curva INVERTIDA — título curto (${inversion.shortLabel}: ${inversion.rateShort.toFixed(2)}%) acima do longo (${inversion.longLabel}: ${inversion.rateLong.toFixed(2)}%) — spread de ${inversion.spread.toFixed(2)} p.p.`
            : `Curva NORMAL — título curto (${inversion.shortLabel}: ${inversion.rateShort.toFixed(2)}%) abaixo do longo (${inversion.longLabel}: ${inversion.rateLong.toFixed(2)}%) — spread de ${Math.abs(inversion.spread).toFixed(2)} p.p.`}
        </div>
      )}

      {selectedCodes.length === 0 && (
        <ErrorDisplay message={`Selecione até ${MAX_COMPARE} títulos para comparar`} />
      )}
      {selectedCodes.length > 0 && compareLoading && <Loading />}
      {selectedCodes.length > 0 && compareError && <ErrorDisplay message={compareError} />}
      {!compareLoading && !compareError && selectedCodes.length > 0 && chartData.length === 0 && (
        <ErrorDisplay message="Sem dados disponíveis" />
      )}
      {!compareLoading && !compareError && chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={400}>
          <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 20 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#333" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 11, fill: '#999' }}
              ticks={xTicks}
              tickFormatter={xTickFormatter}
            />
            <YAxis
              tick={{ fontSize: 11, fill: '#999' }}
              tickFormatter={(v: number) => (metric === 'taxa' ? `${v.toFixed(1)}%` : `R$ ${v.toLocaleString('pt-BR', { maximumFractionDigits: 0 })}`)}
              label={{ value: yLabel, angle: -90, position: 'insideLeft', fill: '#999' }}
            />
            <Tooltip
              contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: 8, color: '#eee' }}
              labelStyle={{ color: '#ccc' }}
              formatter={tooltipFormatter}
            />
            <Legend />
            {series.map(s => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.name}
                stroke={s.color}
                strokeWidth={2}
                dot={false}
                connectNulls
                activeDot={{ r: 4 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

function TesouroDireto() {
  const { data, loading, error } = useFetch<any>('/api/tesouro-direto')
  const [indexerFilter, setIndexerFilter] = useState<string>('all')
  const [maturityFilter, setMaturityFilter] = useState<string>('all')
  const [rateType, setRateType] = useState<'buy' | 'sell'>('buy')

  const allBonds = useMemo(() => {
    if (!data?.data) return []
    const prefixado = data.data.prefixado || []
    const ipca = data.data.ipca || []
    return [...prefixado, ...ipca]
  }, [data])

  const filteredBonds = useMemo(() => {
    let filtered = allBonds

    if (indexerFilter !== 'all') {
      filtered = filtered.filter((b: Bond) => b.indexer === indexerFilter)
    }

    if (maturityFilter !== 'all') {
      filtered = filtered.filter((b: Bond) => b.maturityLabel === maturityFilter)
    }

    return filtered
  }, [allBonds, indexerFilter, maturityFilter])

  const last = (key: string) => {
    const bonds = filteredBonds.filter((b: Bond) => b.indexer === key)
    return bonds.length > 0 ? bonds[bonds.length - 1] : null
  }

  const formatRate = (b: Bond) => {
    const rate = rateType === 'buy' ? b.buyRate : b.sellRate
    if (rate === null) return '-'
    if (b.indexer === 'ipca') return `+${rate.toFixed(2)}%`
    return `${rate.toFixed(2)}%`
  }

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  return (
    <div className="page">
      <div className="info-box">
        <p><strong>Fonte:</strong> Tesouro Direto — Tesouro Transparente</p>
        <p>Taxas indicativas de compra/venda dos títulos públicos ofertados no Tesouro Direto.</p>
      </div>

      <div className="kpi-row">
        {INDEXERS.map(idx => {
          const bond = last(idx.key)
          if (!bond) return null
          return (
            <div key={idx.key} className="kpi-card" style={{ borderLeft: `4px solid ${idx.color}` }}>
              <div className="kpi-name">{idx.label}</div>
              <div className="kpi-value" style={{ color: idx.color }}>
                {formatRate(bond)}
              </div>
              <div className="kpi-date">{idx.description}</div>
            </div>
          )
        })}
      </div>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Indexador:</span>
          <label className={`series-toggle ${indexerFilter === 'all' ? 'active' : ''}`}>
            <input type="radio" name="indexer" checked={indexerFilter === 'all'} onChange={() => setIndexerFilter('all')} />
            <span>Todos</span>
          </label>
          {INDEXERS.map(idx => (
            <label key={idx.key} className={`series-toggle ${indexerFilter === idx.key ? 'active' : ''}`}>
              <input type="radio" name="indexer" checked={indexerFilter === idx.key} onChange={() => setIndexerFilter(idx.key)} />
              <span style={{ color: idx.color }}>{idx.label}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Vencimento:</span>
          {MATURITY_FILTERS.map(mf => (
            <label key={mf.key} className={`series-toggle ${maturityFilter === mf.key ? 'active' : ''}`}>
              <input type="radio" name="maturity" checked={maturityFilter === mf.key} onChange={() => setMaturityFilter(mf.key)} />
              <span>{mf.label}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Taxa:</span>
          <label className={`series-toggle ${rateType === 'buy' ? 'active' : ''}`}>
            <input type="radio" name="rateType" checked={rateType === 'buy'} onChange={() => setRateType('buy')} />
            <span>Compra</span>
          </label>
          <label className={`series-toggle ${rateType === 'sell' ? 'active' : ''}`}>
            <input type="radio" name="rateType" checked={rateType === 'sell'} onChange={() => setRateType('sell')} />
            <span>Venda</span>
          </label>
        </div>
      </div>

      <CompareChart />

      <div className="chart-container">
        <h3 className="chart-title">Títulos Disponíveis</h3>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Título</th>
                <th>Indexador</th>
                <th>Vencimento</th>
                <th>Prazo (anos)</th>
                <th>{rateType === 'buy' ? 'Taxa Compra' : 'Taxa Venda'}</th>
                <th>{rateType === 'buy' ? 'Preço Compra' : 'Preço Venda'}</th>
              </tr>
            </thead>
            <tbody>
              {filteredBonds.map((bond: Bond) => (
                <tr key={bond.symbol}>
                  <td>{bond.name}</td>
                  <td>
                    <span style={{ color: INDEXERS.find(i => i.key === bond.indexer)?.color }}>
                      {bond.indexer === 'ipca' ? 'IPCA+' : 'Prefixado'}
                    </span>
                  </td>
                  <td>{bond.maturityDate}</td>
                  <td>{(bond.durationDays / 365.25).toFixed(1)}</td>
                  <td>
                    {rateType === 'buy'
                      ? (bond.buyRate !== null ? `${bond.buyRate.toFixed(2)}%` : '-')
                      : (bond.sellRate !== null ? `${bond.sellRate.toFixed(2)}%` : '-')}
                  </td>
                  <td>
                    {rateType === 'buy'
                      ? (bond.buyPrice !== null ? `R$ ${bond.buyPrice.toLocaleString('pt-BR', { minimumFractionDigits: 2 })}` : '-')
                      : (bond.sellPrice !== null ? `R$ ${bond.sellPrice.toLocaleString('pt-BR', { minimumFractionDigits: 2 })}` : '-')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export default TesouroDireto
