import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useFetch } from '../hooks/useFetch'

type SidraPoint = {
  readonly reference_period: string
  readonly period_label: string
  readonly value: number | string
}

type SidraMetadata = {
  readonly unit: string
  readonly unit_label: string
  readonly base_version: string | null
  readonly frequency: string
  readonly period_label: string
}

type SidraResponse = {
  readonly status: 'ok' | 'partial'
  readonly data: Readonly<Record<string, readonly SidraPoint[]>>
  readonly metadata: Readonly<Record<string, SidraMetadata>>
  readonly unavailable: readonly string[]
}

type DatasetDefinition = {
  readonly key: string
  readonly name: string
  readonly shortName: string
  readonly color: string
}

const DATASETS = [
  { key: 'pim_industria', name: 'Produção industrial', shortName: 'PIM-PF', color: '#4ecdc4' },
  { key: 'pmc_varejo_ampliado', name: 'Varejo ampliado', shortName: 'PMC', color: '#7b8cff' },
  { key: 'pms_servicos', name: 'Volume de serviços', shortName: 'PMS', color: '#41d9a0' },
  { key: 'massa_rendimento_real', name: 'Massa de rendimento real', shortName: 'Massa real', color: '#ffd166' },
  { key: 'taxa_subutilizacao', name: 'Taxa de subutilização', shortName: 'Subutilização', color: '#ff6b6b' },
] as const satisfies readonly DatasetDefinition[]

const CHARTS = [
  { title: 'Histórico da produção industrial', keys: ['pim_industria'] },
  {
    title: 'Comércio e serviços',
    keys: ['pmc_varejo_ampliado', 'pms_servicos'],
  },
  {
    title: 'Renda do trabalho e subutilização',
    keys: ['massa_rendimento_real', 'taxa_subutilizacao'],
  },
] as const

function numericValue(value: number | string): number | null {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function formatValue(point: SidraPoint | undefined, unit: string | undefined): string {
  if (point === undefined) return '—'
  const parsed = numericValue(point.value)
  if (parsed === null) return '—'
  const formatted = parsed.toLocaleString('pt-BR', {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })
  if (unit === 'pct') return `${formatted}%`
  if (unit === 'brl_million') return `R$ ${formatted} mi`
  return formatted
}

function definitionFor(key: string): DatasetDefinition {
  return DATASETS.find(dataset => dataset.key === key) ?? {
    key,
    name: key,
    shortName: key,
    color: 'var(--accent)',
  }
}

function chartData(
  datasets: readonly { readonly key: string; readonly series: readonly SidraPoint[] }[],
): Array<Record<string, number | string>> {
  const periods = new Map<string, Record<string, number | string>>()
  datasets.forEach(dataset => {
    dataset.series.forEach(point => {
      const value = numericValue(point.value)
      if (value === null) return
      const row = periods.get(point.reference_period) ?? { period: point.period_label }
      row[dataset.key] = value
      periods.set(point.reference_period, row)
    })
  })
  return [...periods.entries()]
    .sort(([leftPeriod], [rightPeriod]) => leftPeriod.localeCompare(rightPeriod))
    .map(([, row]) => row)
}

type SidraSectionsProps = {
  readonly embedded?: boolean
}

export function SidraSections({ embedded = false }: SidraSectionsProps) {
  const { data, error } = useFetch<SidraResponse>('/api/atividade/sidra')

  if (error !== null) {
    return (
      <section className="sidra-section" aria-labelledby="sidra-heading">
        <h2 id="sidra-heading">IBGE/SIDRA</h2>
        <p className="sidra-notice sidra-error" role="alert">
          Dados SIDRA indisponíveis; séries SGS mantidas.
        </p>
      </section>
    )
  }

  if (data === null) return null

  return (
    <section className="sidra-section" aria-labelledby="sidra-heading">
      <header className="sidra-heading">
        <div>
          <p className="sidra-eyebrow">IBGE / SIDRA</p>
          <h2 id="sidra-heading">Aprofundamento IBGE/SIDRA</h2>
        </div>
        <p className="sidra-context">Brasil · frequência mensal ou trimestre móvel</p>
      </header>

      {data.status === 'partial' && (
        <p className="sidra-notice" role="status">
          Fonte parcialmente indisponível. {data.unavailable.length} série(s) ainda indisponível(is).
        </p>
      )}

      <div className="sidra-kpis">
        {DATASETS.map(definition => {
          const series = data.data[definition.key]
          const metadata = data.metadata[definition.key]
          const latest = series?.[series.length - 1]
          const frequency = metadata?.frequency === 'moving_quarter'
            ? `Trimestre móvel encerrado em ${metadata.period_label}`
            : 'Mensal'
          return (
            <article className="sidra-kpi" key={definition.name}>
              <span className="sidra-kpi-marker" style={{ background: definition.color }} />
              <span className="sidra-kpi-name">{definition.name}</span>
              <strong>{formatValue(latest, metadata?.unit)}</strong>
              <span>{latest?.period_label ?? 'Sem observação'}</span>
              <small>
                {metadata
                  ? `${metadata.unit_label}${metadata.base_version ? ` · ${metadata.base_version}` : ''} · ${frequency}`
                  : 'Série indisponível'}
              </small>
            </article>
          )
        })}
      </div>

      <div className="sidra-charts">
        {CHARTS.map(chart => {
          const datasets = chart.keys.flatMap(key => {
            const series = data.data[key]
            return series === undefined ? [] : [{ key, series }]
          })
          return (
            <article className={`${embedded ? '' : 'chart-container'} sidra-chart`} key={chart.title}>
              <div className="chart-header sidra-chart-header">
                <h3 className="chart-title">{chart.title}</h3>
                <span>
                  {datasets.map(dataset => data.metadata[dataset.key]?.unit_label ?? '').join(' / ')}
                </span>
              </div>
              {datasets.length === 0 ? (
                <p className="sidra-chart-empty">Série indisponível neste corte.</p>
              ) : (
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={chartData(datasets)} margin={{ top: 12, right: 18, left: 4, bottom: 4 }}>
                    <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
                    <XAxis dataKey="period" minTickGap={32} tick={{ fill: 'var(--text-secondary)', fontSize: 11 }} />
                    <YAxis tick={{ fill: 'var(--text-secondary)', fontSize: 11 }} width={46} />
                    <Tooltip />
                    <Legend />
                    {datasets.map(dataset => {
                      const definition = definitionFor(dataset.key)
                      return (
                        <Line
                          dataKey={dataset.key}
                          dot={false}
                          key={dataset.key}
                          name={definition.shortName}
                          stroke={definition.color}
                          strokeWidth={2}
                          type="monotone"
                        />
                      )
                    })}
                  </LineChart>
                </ResponsiveContainer>
              )}
            </article>
          )
        })}
      </div>
    </section>
  )
}
