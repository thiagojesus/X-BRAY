import { Calculator, CalendarClock, Database, Gauge, Sigma } from 'lucide-react'
import { AdditionalDerivedIndicators } from '../components/AdditionalDerivedIndicators'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { DerivedHistory } from '../components/derived-history/DerivedHistory'
import { useFetch } from '../hooks/useFetch'

type Classification = 'muito_baixo' | 'baixo' | 'neutro' | 'restritivo' | 'muito_restritivo'

type DerivedInputMetadata = {
  readonly availability_basis?: string
  readonly filter?: string
  readonly symbol?: string
  readonly maturity?: string
}

type DerivedInput = {
  readonly value: number
  readonly unit: 'pct_annual'
  readonly reference_period: string
  readonly available_at: string
  readonly source: string
  readonly metadata?: DerivedInputMetadata
}

type RealRateSnapshot = {
  readonly key: 'juro_real_ex_ante_1y'
  readonly value: number | null
  readonly unit: 'pct_annual'
  readonly as_of: string
  readonly methodology_version: string
  readonly formula: string
  readonly inputs: Readonly<Partial<Record<'nominal_rate' | 'expected_inflation', DerivedInput>>>
  readonly reference_date: string | null
  readonly historical_percentile: number | null
  readonly classification: Classification | null
  readonly classification_reason: 'insufficient_history' | 'non_finite_value' | null
  readonly unavailable_reason: string | null
}

type RealRateResponse = {
  readonly status: 'ok' | 'unavailable'
  readonly data: RealRateSnapshot | { readonly key?: undefined }
}

const CLASSIFICATION_LABELS: Readonly<Record<Classification, string>> = {
  muito_baixo: 'Muito baixo',
  baixo: 'Baixo',
  neutro: 'Neutro',
  restritivo: 'Restritivo',
  muito_restritivo: 'Muito restritivo',
}

const UNAVAILABLE_LABELS: Readonly<Record<string, string>> = {
  focus_12m_missing: 'Expectativa Focus IPCA 12 meses ausente no corte de cálculo.',
  di_curve_missing: 'Curva DI ausente no corte de cálculo.',
  di_contract_missing: 'Nenhum contrato DI elegível para o horizonte de um ano.',
  di_contract_outside_tolerance: 'Nenhum contrato DI ficou dentro da tolerância de 90 dias.',
  input_not_available_at_as_of: 'Os insumos ainda não estavam disponíveis no corte de cálculo.',
  non_finite_result: 'Os insumos não produziram um resultado finito.',
}

const FALLBACK_FORMULA = '((1 + DI/100) / (1 + IPCA/100) - 1) * 100'

function formatRate(value: number | null): string {
  return value === null ? '—' : `${value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}% a.a.`
}

function formatPercentile(value: number | null): string {
  return value === null ? 'Pendente' : `${value.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}º`
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleDateString('pt-BR', { timeZone: 'UTC' })
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return `${date.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'UTC' })} UTC`
}

function inputCard(title: string, input: DerivedInput | undefined) {
  return (
    <article className="derived-input-card">
      <div className="derived-input-heading">
        <Database size={18} aria-hidden="true" />
        <h3>{title}</h3>
      </div>
      {input ? (
        <dl className="derived-input-list">
          <div><dt>Valor</dt><dd>{formatRate(input.value)}</dd></div>
          <div><dt>Fonte</dt><dd>{input.source}</dd></div>
          <div><dt>Data de referência</dt><dd>{formatDate(input.reference_period)}</dd></div>
          <div><dt>Disponível em</dt><dd>{formatTimestamp(input.available_at)}</dd></div>
          {input.metadata?.availability_basis && <div><dt>Base de disponibilidade</dt><dd>{input.metadata.availability_basis}</dd></div>}
          {input.metadata?.filter && <div><dt>Filtro da fonte</dt><dd>{input.metadata.filter}</dd></div>}
          {input.metadata?.symbol && <div><dt>Contrato</dt><dd>{input.metadata.symbol}</dd></div>}
          {input.metadata?.maturity && <div><dt>Vencimento</dt><dd>{formatDate(input.metadata.maturity)}</dd></div>}
        </dl>
      ) : <p className="derived-input-missing">Insumo indisponível neste snapshot.</p>}
    </article>
  )
}

export default function Derivados() {
  const { data, loading, error, isStale } = useFetch<RealRateResponse>('/api/derivados/juro-real-ex-ante-1y')
  const snapshot = data?.data.key ? data.data : null
  const blockingError = error && !data ? error : null
  const unavailable = Boolean(data && (!snapshot || data.status === 'unavailable' || snapshot.value === null))
  const pendingClassification = snapshot !== null && snapshot.value !== null && snapshot.classification_reason === 'insufficient_history'
  const classification = snapshot?.classification ? CLASSIFICATION_LABELS[snapshot.classification] : 'Pendente'
  const reason = snapshot?.unavailable_reason
    ? (UNAVAILABLE_LABELS[snapshot.unavailable_reason] ?? snapshot.unavailable_reason)
    : 'O primeiro snapshot elegível ainda não foi calculado.'

  return (
    <div className="page derived-page">
      <DataQualityNotice
        dataset="derived:juro_real_ex_ante_1y"
        loading={loading}
        error={blockingError}
        isStale={isStale}
        quality={{ status: 'ok', last_updated: snapshot?.as_of }}
      />

      {!loading && <p className="info-box">O resultado é uma medida derivada dos dados disponíveis no corte; não é uma previsão nem uma afirmação causal.</p>}

      {!loading && !blockingError && (
        <>
          <section className={`derived-hero ${unavailable ? 'is-unavailable' : ''}`} aria-labelledby="real-rate-title">
            <div className="derived-hero-copy">
              <span className="derived-eyebrow"><Calculator size={16} aria-hidden="true" /> Indicador derivado</span>
              <h1 id="real-rate-title">Juro real ex-ante de um ano</h1>
              <p className="derived-hero-description">Medida calculada pela equação de Fisher a partir do contrato DI elegível selecionado para o horizonte de um ano e da expectativa Focus IPCA de 12 meses, sem arredondamento intermediário.</p>
              <div className="derived-dates">
                <span><CalendarClock size={16} aria-hidden="true" /> Corte: {formatTimestamp(snapshot?.as_of)}</span>
                <span>Data-base: {formatDate(snapshot?.reference_date)}</span>
              </div>
            </div>
            <div className="derived-primary-value">
              <span>{unavailable ? 'Indisponível' : 'Medida derivada'}</span>
              <strong>{formatRate(snapshot?.value ?? null)}</strong>
              <small>Horizonte de 1 ano</small>
            </div>
          </section>

          {unavailable && (
            <div className="derived-state-message" role="status">
              <strong>Indicador indisponível.</strong>
              <span>{reason}</span>
            </div>
          )}

          {pendingClassification && (
            <div className="derived-state-message is-pending" role="status">
              <strong>Classificação em formação.</strong>
              <span>O valor já está disponível; percentil e classe exigem ao menos 24 snapshots históricos elegíveis.</span>
            </div>
          )}

          <section className="derived-section" aria-labelledby="reading-title">
            <div className="derived-section-heading">
              <Gauge size={19} aria-hidden="true" />
              <h2 id="reading-title">Leitura do indicador</h2>
            </div>
            <div className="derived-metric-grid">
              <article><span>Valor</span><strong>{formatRate(snapshot?.value ?? null)}</strong><small>Fisher ex-ante</small></article>
              <article><span>Classe</span><strong>{snapshot === null || snapshot.value === null ? '—' : classification}</strong><small>Posição histórica</small></article>
              <article><span>Percentil</span><strong>{snapshot === null || snapshot.value === null ? '—' : formatPercentile(snapshot.historical_percentile)}</strong><small>Até 10 anos</small></article>
              <article><span>Metodologia</span><strong>{snapshot?.methodology_version ?? '1.0.0'}</strong><small>Versão do cálculo</small></article>
            </div>
          </section>

          <section className="derived-section" aria-labelledby="inputs-title">
            <div className="derived-section-heading">
              <Database size={19} aria-hidden="true" />
              <h2 id="inputs-title">Insumos no corte</h2>
            </div>
            <div className="derived-input-grid">
              {inputCard('Taxa nominal DI', snapshot?.inputs.nominal_rate)}
              {inputCard('Expectativa IPCA 12 meses', snapshot?.inputs.expected_inflation)}
            </div>
          </section>

          <details className="derived-methodology">
            <summary><Sigma size={19} aria-hidden="true" /> Como o indicador é calculado</summary>
            <div className="derived-methodology-body">
              <p>Aplicamos a equação de Fisher ex-ante à taxa DI do contrato mais próximo de um ano e à mediana suavizada do Focus IPCA 12 meses.</p>
              <code>{snapshot?.formula ?? FALLBACK_FORMULA}</code>
              <ul>
                <li>O contrato DI deve estar a no máximo 90 dias do alvo de 365 dias.</li>
                <li>Somente informações disponíveis até o corte de cálculo são elegíveis.</li>
                <li>Não há fallback para expectativa anual de calendário nem busca de rede nesta tela.</li>
              </ul>
            </div>
          </details>
        </>
      )}
      <AdditionalDerivedIndicators />
      <DerivedHistory />
    </div>
  )
}
