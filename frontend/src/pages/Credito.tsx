import { TimeSeriesChart, type SeriesConfig } from '../charts/TimeSeriesChart'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import { useFetch } from '../hooks/useFetch'
import type { IndicatorUnit, RawSeriesPoint } from '../types/indicators'
import { formatIndicatorValue } from '../utils/format'
import {
  groupByMonthLastValue,
  lastValidSeriesPoint,
  mergeSeriesByMonthLastValue,
} from '../utils/series'

type CreditStatus = 'ok' | 'partial' | 'unavailable'

type CreditMetadata = {
  readonly code: number
  readonly unit: IndicatorUnit
  readonly frequency: string
  readonly reference_period: string | null
  readonly deadline: string | null
  readonly freshness: 'ok' | 'stale'
}

type CreditResponse = {
  readonly last_updated?: string | null
  readonly status: CreditStatus
  readonly data: Readonly<Record<string, readonly RawSeriesPoint[]>>
  readonly metadata: Readonly<Record<string, CreditMetadata>>
  readonly unavailable: readonly string[]
}

type InterestRateResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

type CreditKpi = {
  readonly key: string
  readonly name: string
  readonly color: string
  readonly fallbackUnit: IndicatorUnit
  readonly description: string
}

const KPI_SERIES: readonly CreditKpi[] = [
  {
    key: 'inadimplencia_pf',
    name: 'Inadimplência PF',
    color: '#ff6b6b',
    fallbackUnit: 'pct',
    description: 'Operações de crédito das pessoas físicas com atraso superior a 90 dias.',
  },
  {
    key: 'endividamento_familias',
    name: 'Endividamento das famílias',
    color: '#ffa502',
    fallbackUnit: 'pct',
    description: 'Dívida das famílias em relação à renda acumulada em doze meses.',
  },
  {
    key: 'comprometimento_renda',
    name: 'Comprometimento de renda',
    color: '#ffd166',
    fallbackUnit: 'pct',
    description: 'Parcela da renda mensal destinada ao serviço das dívidas.',
  },
  {
    key: 'icc_pf',
    name: 'ICC PF',
    color: '#a29bfe',
    fallbackUnit: 'pct_annual',
    description: 'Indicador de Custo do Crédito para pessoas físicas, em taxa anual.',
  },
  {
    key: 'taxa_media_credito_pf',
    name: 'Taxa média do crédito PF',
    color: '#4ecdc4',
    fallbackUnit: 'pct_annual',
    description: 'Taxa média anual das novas operações de crédito para pessoas físicas.',
  },
]

const COMMITMENT_DELINQUENCY_SERIES: SeriesConfig[] = [
  { key: 'comprometimento_renda', name: 'Comprometimento (% da renda)', color: '#ffd166', format: 'pct' },
  { key: 'inadimplencia_pf', name: 'Inadimplência (% da carteira)', color: '#ff6b6b', format: 'pct', yAxisId: 'right' },
]

const SELIC_CREDIT_SERIES: SeriesConfig[] = [
  { key: 'selic_meta', name: 'Selic meta (% a.a.)', color: '#a29bfe', format: 'pct' },
  { key: 'taxa_media_credito_pf', name: 'Taxa média PF (% a.a.)', color: '#4ecdc4', format: 'pct', yAxisId: 'right' },
]

const DEBT_COMMITMENT_SERIES: SeriesConfig[] = [
  { key: 'endividamento_familias', name: 'Endividamento (% da renda anual)', color: '#ffa502', format: 'pct' },
  { key: 'comprometimento_renda', name: 'Comprometimento (% da renda mensal)', color: '#ffd166', format: 'pct', yAxisId: 'right' },
]

const percentTick = (value: number): string => `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`

function trendSummary(
  label: string,
  points: readonly RawSeriesPoint[] | undefined,
  unit: IndicatorUnit,
): string {
  const monthly = groupByMonthLastValue(points)
  if (monthly.length < 2) return `${label}: dados insuficientes para comparar os dois últimos períodos.`

  const previous = monthly[monthly.length - 2]
  const current = monthly[monthly.length - 1]
  if (previous === undefined || current === undefined) {
    return `${label}: dados insuficientes para comparar os dois últimos períodos.`
  }

  const direction = current.value > previous.value
    ? 'subiu'
    : current.value < previous.value
      ? 'caiu'
      : 'ficou estável'
  const previousValue = formatIndicatorValue(previous.value, unit) ?? 'indisponível'
  const currentValue = formatIndicatorValue(current.value, unit) ?? 'indisponível'
  return `${label} ${direction}: ${previousValue} em ${previous.date} para ${currentValue} em ${current.date}.`
}

function Credito() {
  const credit = useFetch<CreditResponse>('/api/credito')
  const interestRates = useFetch<InterestRateResponse>('/api/juros')

  if (credit.loading) {
    return <div className="page"><DataQualityNotice dataset="credito" loading /></div>
  }
  if (credit.error && !credit.data) {
    return <div className="page"><DataQualityNotice dataset="credito" error={credit.error} /></div>
  }
  if (!credit.data?.data) {
    return <div className="page"><DataQualityNotice dataset="credito" error="Sem dados" /></div>
  }

  const response = credit.data
  const unitFor = (key: string, fallback: IndicatorUnit): IndicatorUnit => (
    response.metadata[key]?.unit ?? fallback
  )
  const selicPoints = interestRates.data?.data?.selic_meta
  const latestSelic = lastValidSeriesPoint(selicPoints)
  const monthlyCredit = mergeSeriesByMonthLastValue(response.data)
  const selicCredit = mergeSeriesByMonthLastValue({
    selic_meta: selicPoints,
    taxa_media_credito_pf: response.data.taxa_media_credito_pf,
  })
  const quality = {
    status: response.status,
    last_updated: response.last_updated,
    metadata: response.metadata,
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="credito" quality={quality} isStale={credit.isStale} />
      {interestRates.loading ? (
        <div className="data-quality-notice" role="status" aria-live="polite">
          <strong>Carregando Selic meta...</strong> A comparação com a taxa média PF será completada quando a série estiver disponível.
        </div>
      ) : latestSelic === null ? (
        <div className="data-quality-notice" role="status" aria-live="polite">
          <strong>Selic meta indisponível.</strong> A comparação exibe somente os dados de crédito disponíveis.
        </div>
      ) : interestRates.isStale ? (
        <div className="data-quality-notice" role="status" aria-live="polite">
          <strong>Dados da Selic possivelmente desatualizados.</strong> Exibindo a última observação disponível.
        </div>
      ) : null}
      <div className="info-box">
        <p><strong>Fonte:</strong> Banco Central do Brasil, Sistema Gerenciador de Séries Temporais (SGS).</p>
        <p><strong>Método mensal:</strong> cada série usa sua última observação válida no mês; meses sem observação permanecem como lacunas, sem preenchimento.</p>
        <p><strong>Leitura econômica:</strong> Selic → custo do crédito → orçamento familiar → inadimplência → consumo. A sequência é apresentada como mecanismo econômico geral, não como prova causal dos dados do gráfico.</p>
      </div>
      <div className="kpi-row">
        {KPI_SERIES.map(series => {
          const latest = lastValidSeriesPoint(response.data[series.key])
          const formatted = latest
            ? formatIndicatorValue(latest.valor, unitFor(series.key, series.fallbackUnit))
            : null
          return (
            <KpiCard
              key={series.key}
              name={series.name}
              value={formatted ?? 'Indisponível'}
              date={latest?.data ?? ''}
              color={series.color}
              description={series.description}
            />
          )
        })}
      </div>
      <div className="info-box" aria-label="Resumo de tendências">
        <p><strong>Último movimento de cada série</strong></p>
        <p>{trendSummary('Custo do crédito', response.data.taxa_media_credito_pf, unitFor('taxa_media_credito_pf', 'pct_annual'))}</p>
        <p>{trendSummary('Comprometimento de renda', response.data.comprometimento_renda, unitFor('comprometimento_renda', 'pct'))}</p>
        <p>{trendSummary('Inadimplência PF', response.data.inadimplencia_pf, unitFor('inadimplencia_pf', 'pct'))}</p>
      </div>
      <TimeSeriesChart
        data={monthlyCredit}
        series={COMMITMENT_DELINQUENCY_SERIES}
        title="Comprometimento de renda × inadimplência PF"
        yLabel="Comprometimento (% da renda)"
        rightYLabel="Inadimplência (% da carteira)"
        yTickFormatter={percentTick}
        rightYTickFormatter={percentTick}
        defaultWindow="5Y"
        connectNulls={false}
        showReferenceDates
      />
      <TimeSeriesChart
        data={selicCredit}
        series={SELIC_CREDIT_SERIES}
        title="Selic meta × taxa média do crédito PF"
        yLabel="Selic meta (% a.a.)"
        rightYLabel="Taxa média PF (% a.a.)"
        yTickFormatter={percentTick}
        rightYTickFormatter={percentTick}
        defaultWindow="5Y"
        connectNulls={false}
        showReferenceDates
      />
      <TimeSeriesChart
        data={monthlyCredit}
        series={DEBT_COMMITMENT_SERIES}
        title="Endividamento × comprometimento de renda"
        yLabel="Endividamento (% da renda anual)"
        rightYLabel="Comprometimento (% da renda mensal)"
        yTickFormatter={percentTick}
        rightYTickFormatter={percentTick}
        defaultWindow="5Y"
        connectNulls={false}
        showReferenceDates
      />
    </div>
  )
}

export default Credito
