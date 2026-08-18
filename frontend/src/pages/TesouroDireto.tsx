import { useState, useMemo, useEffect } from 'react'
import { useFetch } from '../hooks/useFetch'
import { Loading, ErrorDisplay } from '../components/Status'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'

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

const COUPON_LABEL: Record<string, string> = {
  zero: 'Sem cupom',
  semestrais: 'Juros semestrais',
  mensais: 'Juros mensais',
}

function HistoryChart() {
  const { data: catalogData, loading: catalogLoading } = useFetch<any>('/api/tesouro-direto/titulos')
  const catalog = useMemo(() => {
    const items: CatalogBond[] = catalogData?.data || []
    return items
      .filter(b => b.indexer === 'prefixado' || b.indexer === 'ipca')
      .sort((a, b) => {
        const byIndexer = INDEXERS.findIndex(i => i.key === a.indexer) - INDEXERS.findIndex(i => i.key === b.indexer)
        if (byIndexer !== 0) return byIndexer
        return (a.targetYear ?? 0) - (b.targetYear ?? 0)
      })
  }, [catalogData])

  const [selectedCode, setSelectedCode] = useState<number | null>(null)
  const [period, setPeriod] = useState<number>(30)
  const [metric, setMetric] = useState<'taxa' | 'preco'>('taxa')
  const [showBuy, setShowBuy] = useState(true)
  const [showSell, setShowSell] = useState(true)

  useEffect(() => {
    if (selectedCode === null && catalog.length > 0) {
      setSelectedCode(catalog[0].code)
    }
  }, [catalog, selectedCode])

  const historyUrl = selectedCode !== null
    ? `/api/tesouro-direto/historico?code=${selectedCode}&days=${period}`
    : null

  const { data: historyData, loading: historyLoading, error: historyError } = useFetch<any>(historyUrl ?? '')

  const selectedBond = useMemo(() => catalog.find(c => c.code === selectedCode) || null, [catalog, selectedCode])

  const points: HistoryPoint[] = useMemo(() => {
    return historyData?.data?.points || []
  }, [historyData])

  const chartData = useMemo(() => {
    return points.map(p => ({
      date: p.date,
      buy: metric === 'taxa' ? p.buyRate : p.buyPrice,
      sell: metric === 'taxa' ? p.sellRate : p.sellPrice,
    }))
  }, [points, metric])

  const series = useMemo(() => {
    const list = []
    if (showBuy) list.push({ key: 'buy', name: 'Investimento', color: '#4ecdc4' })
    if (showSell) list.push({ key: 'sell', name: 'Resgate', color: '#ff6b6b' })
    return list
  }, [showBuy, showSell])

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

  const xTickFormatter = (v: string) => {
    const parts = v.split('/')
    return parts.length === 3 ? `${parts[1]}/${parts[0].slice(2)}` : v
  }

  if (catalogLoading) return <Loading />
  if (catalog.length === 0) return <ErrorDisplay message="Sem dados disponíveis" />

  return (
    <div className="chart-container">
      <h3 className="chart-title">
        Histórico de Preços e Taxas — {selectedBond ? `${selectedBond.name} ${selectedBond.targetYear ?? ''}`.trim() : '—'}
      </h3>

      <div className="series-controls">
        <div className="series-toggles">
          <span className="toggle-label">Título:</span>
          <select
            className="hist-select"
            value={selectedCode ?? ''}
            onChange={e => setSelectedCode(parseInt(e.target.value))}
          >
            {catalog.map(b => (
              <option key={b.code} value={b.code}>
                {`${b.name} ${b.targetYear ?? ''}`.trim()} {COUPON_LABEL[b.couponType] ? `— ${COUPON_LABEL[b.couponType]}` : ''}
              </option>
            ))}
          </select>
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
          <label className={`series-toggle ${showBuy ? 'active' : ''}`}>
            <input type="checkbox" checked={showBuy} onChange={() => setShowBuy(!showBuy)} />
            <span className="toggle-swatch" style={{ background: '#4ecdc4' }} />
            <span>Investimento</span>
          </label>
          <label className={`series-toggle ${showSell ? 'active' : ''}`}>
            <input type="checkbox" checked={showSell} onChange={() => setShowSell(!showSell)} />
            <span className="toggle-swatch" style={{ background: '#ff6b6b' }} />
            <span>Resgate</span>
          </label>
        </div>
      </div>

      {historyLoading && <Loading />}
      {historyError && <ErrorDisplay message={historyError} />}
      {!historyLoading && !historyError && chartData.length === 0 && (
        <ErrorDisplay message="Sem dados disponíveis" />
      )}
      {!historyLoading && !historyError && chartData.length > 0 && series.length > 0 && (
        <ResponsiveContainer width="100%" height={400}>
          <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 20 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#333" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 11, fill: '#999' }}
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

      <HistoryChart />

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
