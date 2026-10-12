import { Database } from 'lucide-react'
import { Fragment } from 'react'
import { useFetch } from '../hooks/useFetch'
import {
  ADDITIONAL_INDICATORS,
  type AdditionalIndicatorKey,
  type AdditionalIndicatorSnapshot,
  type DerivedInput,
  type DerivedMetadataValue,
  parseAdditionalIndicatorResponse,
} from '../types/derived'

const INDICATOR_DETAILS = {
  cobertura_externa: {
    title: 'Cobertura externa por IDP',
    description: 'Compara o IDP em 12 meses ao déficit em conta corrente no mesmo período.',
  },
  variacao_saldo_primario_12m: {
    title: 'Variação do saldo primário em 12 meses',
    description: 'Compara o saldo primário normalizado ao mesmo mês do ano anterior; valor positivo indica melhora aritmética, não efeito causal.',
  },
  spread_credito_pf_selic: {
    title: 'Spread crédito PF × Selic',
    description: 'Diferença entre a taxa média anual de crédito PF e a meta Selic anual no mesmo mês.',
  },
  distancia_focus_meta: {
    title: 'Distância Focus à meta de inflação',
    description: 'Diferença entre a expectativa Focus IPCA de 12 meses e o centro da meta efetiva na data-base.',
  },
  inclinacao_di_5y_1y: {
    title: 'Inclinação DI 5 anos × 1 ano',
    description: 'Diferença entre taxas dos contratos selecionados na mesma data de negociação; não é previsão.',
  },
} as const satisfies Readonly<Record<AdditionalIndicatorKey, { readonly title: string; readonly description: string }>>

const INPUT_LABELS: Readonly<Record<string, string>> = {
  idp_12m_pib: 'IDP acumulado em 12 meses',
  conta_corrente_12m_pib: 'Conta corrente em 12 meses',
  current_balance: 'Saldo primário atual',
  previous_balance: 'Saldo primário de 12 meses antes',
  taxa_media_credito_pf: 'Taxa média de crédito PF',
  selic_meta: 'Meta Selic',
  expected_inflation: 'Expectativa Focus IPCA 12 meses',
  inflation_target: 'Centro da meta de inflação',
  di_1y: 'Contrato DI de 1 ano',
  di_5y: 'Contrato DI de 5 anos',
}

const METADATA_LABELS: Readonly<Record<string, string>> = {
  availability_basis: 'Disponível desde',
  effective_from: 'Início da vigência',
  effective_to: 'Fim da vigência',
  first_seen_at: 'Primeira observação',
  filter: 'Filtro Focus',
  legal_reference: 'Ato normativo',
  maturity: 'Vencimento',
  provider_code: 'Código SGS',
  regime: 'Regime da meta',
  rule_id: 'Regra aplicada',
  source_published_at: 'Publicação oficial',
  source_url: 'Fonte metodológica',
  symbol: 'Código do contrato',
  table_version: 'Versão da tabela',
  tolerance_band_pp: 'Banda de tolerância',
}

const UNAVAILABLE_REASONS: Readonly<Record<string, string>> = {
  current_account_non_negative: 'A conta corrente não está em déficit neste snapshot.',
  di_contract_outside_tolerance: 'Não há contratos DI elegíveis dentro da tolerância de 90 dias.',
  di_contract_reused: 'Um contrato DI não pode representar os dois horizontes.',
  focus_12m_missing: 'Expectativa Focus IPCA de 12 meses ausente no corte.',
  inflation_target_missing: 'Não há meta de inflação versionada para a data-base.',
  input_not_available_at_as_of: 'Um ou mais insumos ainda não estavam disponíveis no corte.',
  input_unit_mismatch: 'Os insumos não usam as unidades exigidas pela metodologia.',
  non_finite_result: 'Os insumos não produziram um resultado finito.',
  primary_balance_previous_month_missing: 'Não há saldo primário para o mesmo mês de 12 meses antes.',
  reference_month_mismatch: 'Os insumos não correspondem ao mesmo período exigido.',
  required_input_missing: 'Falta um insumo obrigatório no corte.',
}

function formatNumber(value: number): string {
  return value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function formatDate(value: string | null): string {
  if (value === null) return 'Não informado'
  const date = new Date(`${value}T00:00:00.000Z`)
  return date.toLocaleDateString('pt-BR', { timeZone: 'UTC' })
}

function formatTimestamp(value: string): string {
  return `${new Date(value).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short', timeZone: 'UTC' })} UTC`
}

function breakableTechnicalText(value: string) {
  let offset = 0
  return value.split(/([_/.])/u).map(part => {
    const key = `${offset}:${part}`
    offset += part.length
    return (
      <Fragment key={key}>
        {part}
        {part === '_' || part === '.' || part === '/' ? <wbr /> : null}
      </Fragment>
    )
  })
}

function formatInputValue(input: DerivedInput): string {
  const number = formatNumber(input.value)
  if (input.unit === 'pct_annual') return `${number}% a.a.`
  if (input.unit === 'pct_gdp') return `${number}% do PIB`
  if (input.unit === 'pp') return `${number} p.p.`
  return `${number} ${input.unit}`
}

function formatDerivedValue(key: AdditionalIndicatorKey, value: number): string {
  if (key === 'cobertura_externa') return `${formatNumber(value * 100)}%`
  return `${formatNumber(value)} p.p.`
}

function formatMetadataValue(value: DerivedMetadataValue): string {
  return value === null ? 'Não informado' : String(value)
}

function formatMetadata(key: string, value: DerivedMetadataValue): string {
  if (value === null) return 'Não informado'
  if ((key === 'effective_from' || key === 'effective_to' || key === 'maturity') && typeof value === 'string') {
    return formatDate(value)
  }
  if ((key === 'first_seen_at' || key === 'source_published_at') && typeof value === 'string') {
    return formatTimestamp(value)
  }
  if (key === 'availability_basis' && value === 'first_seen_at') return 'Primeira observação local'
  if (key === 'availability_basis' && value === 'source_published_at') return 'Publicação oficial'
  if (key === 'tolerance_band_pp' && typeof value === 'number') return `±${formatNumber(value)} p.p.`
  return formatMetadataValue(value)
}

function inputLabel(key: string): string {
  return INPUT_LABELS[key] ?? key
}

function metadataLabel(key: string): string {
  return METADATA_LABELS[key] ?? key.split('_').join(' ')
}

function reasonLabel(reason: string): string {
  return UNAVAILABLE_REASONS[reason] ?? reason
}

function IndicatorInputs({ inputs }: { readonly inputs: Readonly<Record<string, DerivedInput>> }) {
  return (
    <dl className="derived-input-list additional-derived-input-list">
      {Object.entries(inputs).map(([key, input]) => (
        <div className="additional-derived-input" key={key}>
          <dt>{inputLabel(key)}</dt>
          <dd>
            <span>{formatInputValue(input)}</span>
            <span>{breakableTechnicalText(input.source)}</span>
            <span>Referência: {formatDate(input.reference_period)}</span>
            <span>Disponível: {formatTimestamp(input.available_at)}</span>
            {Object.entries(input.metadata ?? {})
              .filter(([metadataKey, metadataValue]) => metadataKey !== 'source_url' || metadataValue !== input.source)
              .map(([metadataKey, metadataValue]) => (
              <span key={metadataKey}>{metadataLabel(metadataKey)}: {formatMetadata(metadataKey, metadataValue)}</span>
            ))}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function IndicatorCard({
  keyName,
  snapshot,
}: {
  readonly keyName: AdditionalIndicatorKey
  readonly snapshot: AdditionalIndicatorSnapshot | null
}) {
  const details = INDICATOR_DETAILS[keyName]
  const titleId = `additional-derived-${keyName}`
  const stateLabel = snapshot === null
    ? 'Snapshot não encontrado neste cache.'
    : snapshot.kind === 'invalid'
      ? 'Payload inválido.'
      : snapshot.kind === 'unavailable'
        ? reasonLabel(snapshot.reason)
        : null

  return (
    <article className="additional-derived-card" aria-labelledby={titleId}>
      <header className="additional-derived-card-heading">
        <h3 id={titleId}>{details.title}</h3>
        <span>{snapshot === null ? 'Sem snapshot' : snapshot.kind === 'available' ? 'Disponível' : 'Indisponível'}</span>
      </header>
      <p className="additional-derived-description">{details.description}</p>
      <div className="additional-derived-value">
        {snapshot?.kind === 'available' ? (
          <strong>{formatDerivedValue(keyName, snapshot.value)}</strong>
        ) : (
          <strong>{snapshot?.kind === 'invalid' ? 'Indisponível' : 'Indisponível'}</strong>
        )}
        {stateLabel && <span>{stateLabel}</span>}
      </div>
      {snapshot !== null && snapshot.kind !== 'invalid' && (
        <>
          <dl className="derived-input-list additional-derived-meta">
            <div><dt>Data-base</dt><dd>{formatDate(snapshot.reference_date)}</dd></div>
            <div><dt>Corte</dt><dd>{formatTimestamp(snapshot.as_of)}</dd></div>
            <div><dt>Metodologia</dt><dd>{snapshot.methodology_version}</dd></div>
            <div><dt>Unidade</dt><dd>{snapshot.unit === 'ratio' ? 'ratio' : 'p.p.'}</dd></div>
          </dl>
          <details className="derived-methodology additional-derived-methodology">
            <summary>Metodologia e insumos</summary>
            <div className="derived-methodology-body">
              <code>{breakableTechnicalText(snapshot.formula)}</code>
              {Object.keys(snapshot.inputs).length > 0
                ? <IndicatorInputs inputs={snapshot.inputs} />
                : <p>Este snapshot não registra insumos disponíveis.</p>}
            </div>
          </details>
        </>
      )}
    </article>
  )
}

export function AdditionalDerivedIndicators() {
  const { data, loading, error, isStale } = useFetch<unknown>('/api/derivados')
  const parsed = data === null ? null : parseAdditionalIndicatorResponse(data)

  return (
    <section className="derived-section additional-derived-section" aria-labelledby="additional-derived-title">
      <div className="derived-section-heading">
        <Database size={19} aria-hidden="true" />
        <h2 id="additional-derived-title">Outros indicadores derivados</h2>
      </div>
      {loading && data === null && <p role="status">Carregando indicadores adicionais...</p>}
      {error && <div className="derived-state-message" role="alert">Falha ao consultar snapshots adicionais: {error}</div>}
      {isStale && data !== null && <p className="additional-derived-stale" role="status">Dados possivelmente desatualizados.</p>}
      {parsed?.kind === 'invalid_envelope' && <div className="derived-state-message" role="alert">Resposta inválida de /api/derivados.</div>}
      {parsed?.kind === 'empty' && <p className="additional-derived-empty" role="status">Nenhum snapshot dos cinco indicadores está disponível no cache.</p>}
      {parsed?.kind === 'populated' && (
        <div className="additional-derived-grid">
          {ADDITIONAL_INDICATORS.map(keyName => (
            <IndicatorCard key={keyName} keyName={keyName} snapshot={parsed.snapshots[keyName] ?? null} />
          ))}
        </div>
      )}
    </section>
  )
}
