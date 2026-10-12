import { useState } from 'react'

import type { SeriesConfig } from '../charts/TimeSeriesChart'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { SidraSections } from '../components/SidraSections'
import { useFetch } from '../hooks/useFetch'
import type { RawSeriesPoint, SeriesMap } from '../types/indicators'
import { lastValidSeriesPoint, mergeSeriesByDate } from '../utils/series'

const ACTIVITY_MODES = [
  { key: 'level', label: 'Nível' },
  { key: 'mom', label: 'Variação mensal' },
  { key: 'yoy', label: 'Variação 12m' },
] as const

type ActivityMode = (typeof ACTIVITY_MODES)[number]['key']

type LegacyActivityResponse = {
  readonly data?: SeriesMap | null
}

type SeriesMetadata = {
  readonly freshness?: 'ok' | 'stale'
}

type SectoralActivityResponse = {
  readonly mode: ActivityMode
  readonly status: 'ok' | 'partial'
  readonly last_updated?: string | null
  readonly unavailable: readonly string[]
  readonly metadata: Readonly<Record<string, SeriesMetadata>>
  readonly data?: SeriesMap | null
}

const TOTAL_SERIES = [
  { key: 'ibc_br_sa', name: 'IBC-Br total', color: '#4ecdc4', format: 'idx' as const },
  { key: 'ibc_ex_agro_sa', name: 'IBC-Br ex-agro', color: '#ff6b6b', format: 'idx' as const },
] as const

const SECTOR_SERIES = [
  { key: 'ibc_agro_sa', name: 'Agropecuária', color: '#ffd166', format: 'idx' as const },
  { key: 'ibc_industria_sa', name: 'Indústria', color: '#7b8cff', format: 'idx' as const },
  { key: 'ibc_servicos_sa', name: 'Serviços', color: '#4ecdc4', format: 'idx' as const },
] as const

const LABOR_SERIES = [
  { key: 'desemprego', name: 'Desemprego', color: '#ff6b6b', format: 'pct' as const },
  { key: 'rendimento_medio_real', name: 'Rendimento médio real', color: '#4ecdc4', yAxisId: 'right', format: 'brl' as const },
] as const

const SERIES_LABELS: Readonly<Record<string, string>> = {
  ibc_br_sa: 'IBC-Br total',
  ibc_agro_sa: 'Agropecuária',
  ibc_industria_sa: 'Indústria',
  ibc_servicos_sa: 'Serviços',
  ibc_ex_agro_sa: 'IBC-Br ex-agro',
  rendimento_medio_real: 'Rendimento médio real',
}

const PERCENT_FORMATTER = new Intl.NumberFormat('pt-BR', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})

const CURRENCY_FORMATTER = new Intl.NumberFormat('pt-BR', {
  style: 'currency',
  currency: 'BRL',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

function latestPoint(points: readonly RawSeriesPoint[] | undefined) {
  return lastValidSeriesPoint(points)
}

function formatPercent(value: number | undefined): string {
  return value === undefined ? '' : `${PERCENT_FORMATTER.format(value)}%`
}

function formatCurrency(value: number | undefined): string {
  return value === undefined ? '' : CURRENCY_FORMATTER.format(value)
}

function Atividade() {
  const [mode, setMode] = useState<ActivityMode>('level')
  const legacy = useFetch<LegacyActivityResponse>('/api/atividade')
  const level = useFetch<SectoralActivityResponse>('/api/atividade/setorial?mode=level')
  const mom = useFetch<SectoralActivityResponse>('/api/atividade/setorial?mode=mom')
  const yoy = useFetch<SectoralActivityResponse>('/api/atividade/setorial?mode=yoy')

  const responses = { level, mom, yoy }
  const selected = responses[mode]
  const loading = legacy.loading || level.loading || mom.loading || yoy.loading
  const error = legacy.error ?? level.error ?? mom.error ?? yoy.error

  if (loading && (
    !legacy.data?.data || !level.data?.data || !mom.data?.data || !yoy.data?.data || !selected.data?.data
  )) return <DataQualityNotice dataset="atividade" loading />
  if (!legacy.data?.data || !level.data?.data || !mom.data?.data || !yoy.data?.data || !selected.data?.data) {
    return <DataQualityNotice dataset="atividade" error={error ?? 'Sem dados'} />
  }

  const legacyData = legacy.data.data
  const levelData = level.data.data
  const selectedData = selected.data.data
  const ibcMom = latestPoint(mom.data.data.ibc_br_sa)
  const ibcYoy = latestPoint(yoy.data.data.ibc_br_sa)
  const unemployment = latestPoint(legacyData.desemprego)
  const income = latestPoint(levelData.rendimento_medio_real)
  const modeIsLevel = mode === 'level'
  const chartFormat: SeriesConfig['format'] = modeIsLevel ? 'idx' : 'pct'
  const yLabel = modeIsLevel
    ? 'Índice (base 100 na janela)'
    : mode === 'mom'
      ? 'Variação mensal (%)'
      : 'Variação 12m (%)'
  const totalSeries = TOTAL_SERIES.map(series => ({ ...series, format: chartFormat }))
  const sectorSeries = SECTOR_SERIES.map(series => ({ ...series, format: chartFormat }))
  const sectorRows = mergeSeriesByDate(selectedData)
  const laborRows = mergeSeriesByDate({
    desemprego: legacyData.desemprego,
    rendimento_medio_real: levelData.rendimento_medio_real,
  })

  return (
    <div className="page activity-page">
      <DataQualityNotice
        dataset="atividade_setorial"
        isStale={legacy.isStale || level.isStale || mom.isStale || yoy.isStale}
        quality={selected.data ?? undefined}
      />
      {selected.data.unavailable.length > 0 && (
        <p className="activity-unavailable">
          Séries indisponíveis neste modo: {selected.data.unavailable
            .map(key => SERIES_LABELS[key] ?? key)
            .join(', ')}.
        </p>
      )}

      <div className="kpi-row activity-kpis">
        <KpiCard
          name="IBC-Br m/m"
          value={formatPercent(ibcMom?.value)}
          date={ibcMom?.data ?? ''}
          color="var(--accent)"
          description="Variação mensal do IBC-Br dessazonalizado calculada contra o mês anterior."
        />
        <KpiCard
          name="IBC-Br 12m"
          value={formatPercent(ibcYoy?.value)}
          date={ibcYoy?.data ?? ''}
          color="#7b8cff"
          description="Variação do IBC-Br dessazonalizado contra o mesmo mês do ano anterior."
        />
        <KpiCard
          name="Desemprego"
          value={formatPercent(unemployment?.value)}
          date={unemployment?.data ?? ''}
          color="#ff6b6b"
          description="Taxa de desemprego da PNAD Contínua, em percentual da força de trabalho."
        />
        <KpiCard
          name="Rendimento médio real"
          value={formatCurrency(income?.value)}
          date={income?.data ?? ''}
          color="#ffd166"
          description="Rendimento médio real habitual das pessoas ocupadas, em reais."
        />
      </div>

      <fieldset className="activity-mode-bar" aria-label="Modo dos gráficos de atividade">
        {ACTIVITY_MODES.map(option => (
          <button
            key={option.key}
            type="button"
            className={`activity-mode-btn ${mode === option.key ? 'active' : ''}`}
            aria-pressed={mode === option.key}
            onClick={() => setMode(option.key)}
          >
            {option.label}
          </button>
        ))}
        <span className="activity-mode-unit">{yLabel}</span>
      </fieldset>

      <TimeSeriesChart
        data={sectorRows}
        series={totalSeries}
        title="IBC-Br total × ex-agro"
        yLabel={yLabel}
        defaultWindow="5Y"
        normalizeKeys={modeIsLevel ? TOTAL_SERIES.map(series => series.key) : []}
      />
      <TimeSeriesChart
        data={sectorRows}
        series={sectorSeries}
        title="Agro × indústria × serviços"
        yLabel={yLabel}
        defaultWindow="5Y"
        normalizeKeys={modeIsLevel ? SECTOR_SERIES.map(series => series.key) : []}
      />
      <TimeSeriesChart
        data={laborRows}
        series={LABOR_SERIES}
        title="Desemprego × rendimento real"
        yLabel="Desemprego (%)"
        rightYLabel="Rendimento real (R$)"
        defaultWindow="5Y"
        rightYTickFormatter={value => formatCurrency(value)}
      />
      <SidraSections embedded />
    </div>
  )
}

export default Atividade
