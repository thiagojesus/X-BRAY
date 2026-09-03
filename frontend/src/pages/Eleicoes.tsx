import { TimeSeriesChart, type SeriesConfig } from '../charts/TimeSeriesChart'
import BrasilMap, { type StatesPayload } from '../charts/BrasilMap'
import { KpiCard } from '../components/KpiCard'
import { Loading, ErrorDisplay } from '../components/Status'
import { useFetch } from '../hooks/useFetch'

const STATES_URL = '/api/eleicoes/estados'

const COLORS = ['#ff6b6b', '#ffa502', '#4ecdc4', '#a29bfe', '#fd79a8', '#74b9ff', '#55efc4', '#fdcb6e']

interface Candidate {
  name: string
  price: number
  volume: number
}

interface ElectionsPayload extends StatesPayload {
  national: {
    candidates: Candidate[]
    history: Record<string, Record<string, number>>
  }
}

function Eleicoes() {
  const { data, loading, error } = useFetch<ElectionsPayload>(STATES_URL)
  const candidates = data?.national?.candidates ?? []
  const history = data?.national?.history ?? {}
  const chartData: Record<string, string | number>[] = Object.entries(history)
    .map(([date, values]) => ({ date, ...values }))
    .sort((a, b) => String(a.date).localeCompare(String(b.date)))

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
      {data && data.ufs.length > 0 && <BrasilMap payload={data} />}
    </div>
  )
}

export default Eleicoes
