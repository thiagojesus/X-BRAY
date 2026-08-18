import { useEffect, useState } from 'react'
import { TimeSeriesChart, type SeriesConfig } from '../charts/TimeSeriesChart'
import { KpiCard } from '../components/KpiCard'
import { Loading, ErrorDisplay } from '../components/Status'

const EVENT_URL = 'https://gamma-api.polymarket.com/events?slug=brazil-presidential-election'
const PRICES_URL = (token: string) =>
  `https://clob.polymarket.com/prices-history?market=${token}&interval=max&fidelity=1440`

const COLORS = ['#ff6b6b', '#ffa502', '#4ecdc4', '#a29bfe', '#fd79a8', '#74b9ff', '#55efc4', '#fdcb6e']

interface Candidate {
  name: string
  token: string
  price: number
  volume: number
}

function extractName(question: string): string {
  const match = question.match(/^Will (.+?) win the 2026 Brazilian presidential election\?$/)
  return match ? match[1] : question.replace('Will ', '').replace(' win the 2026 Brazilian presidential election?', '')
}

function toDateKey(t: number): string {
  return new Date(t * 1000).toISOString().slice(0, 10)
}

function Eleicoes() {
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [chartData, setChartData] = useState<Record<string, any>[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(EVENT_URL)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const events = await res.json()
        const event = Array.isArray(events) ? events[0] : events
        if (!event?.markets) {
          if (!cancelled) {
            setCandidates([])
            setChartData([])
            setLoading(false)
          }
          return
        }

        const pool = event.markets
          .map((m: any) => ({
            name: extractName(m.question),
            token: JSON.parse(m.clobTokenIds || '[]')[0] as string,
            price: Number(m.lastTradePrice ?? 0),
            volume: Number(m.volume ?? 0),
          }))
          .filter((c: Candidate) => c.token && !/^Person/.test(c.name) && c.name !== 'another person')
          .filter((c: Candidate) => c.price >= 0.003 || c.volume > 100000)

        pool.sort((a: Candidate, b: Candidate) => b.price - a.price)
        const top = pool.slice(0, 8)

        const rows: Record<string, any>[] = []
        for (const cand of top) {
          const pr = await fetch(PRICES_URL(cand.token))
          if (!pr.ok) continue
          const body = await pr.json()
          for (const pt of body.history ?? []) {
            const date = toDateKey(pt.t)
            const row = rows.find(r => r.date === date)
            const value = Number(pt.p) * 100
            if (row) row[cand.name] = value
            else rows.push({ date, [cand.name]: value })
          }
        }

        rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
        if (cancelled) return
        setCandidates(top)
        setChartData(rows)
        setLoading(false)
      } catch (e: any) {
        if (!cancelled) {
          setError(e.message)
          setLoading(false)
        }
      }
    }
    load()
    return () => { cancelled = true }
  }, [])

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (candidates.length === 0 || chartData.length === 0) {
    return <div className="empty-state"><p>Sem dados disponíveis</p></div>
  }

  const series: SeriesConfig[] = candidates.map((c, i) => ({
    key: c.name,
    name: c.name,
    color: COLORS[i % COLORS.length],
    format: 'pct' as const,
  }))

  return (
    <div className="page">
      <div className="info-box">
        <p><strong>Fonte:</strong> Polymarket — <a href="https://polymarket.com/event/brazil-presidential-election" target="_blank" rel="noreferrer">Eleição Presidencial do Brasil 2026</a></p>
        <p>Probabilidade de vitória (inclui eventual segundo turno) precificada por traders. Aba temporária.</p>
      </div>
      <div className="kpi-row">
        {candidates.map((c, i) => (
          <KpiCard
            key={c.name}
            name={c.name}
            value={`${(c.price * 100).toFixed(1)}%`}
            date="Polymarket"
            color={COLORS[i % COLORS.length]}
          />
        ))}
      </div>
      <TimeSeriesChart
        data={chartData}
        series={series}
        title="Probabilidade de vitória — Presidente do Brasil 2026"
        yLabel="% de chance"
        defaultWindow="ALL"
      />
    </div>
  )
}

export default Eleicoes
