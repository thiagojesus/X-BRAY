import type { DimensionKey, MacroScoreComponent, MacroScorePayload } from './schema'

export const DIMENSION_LABELS = {
  activity: 'Atividade', inflation: 'Inflação', monetary: 'Monetária',
  fiscal: 'Fiscal', credit: 'Crédito', external: 'Externa',
} as const satisfies Readonly<Record<DimensionKey, string>>

export const DIRECTION_LABELS = {
  higher_is_better: 'Maior é mais favorável', lower_is_better: 'Menor é mais favorável',
} as const satisfies Readonly<Record<MacroScoreComponent['direction'], string>>

const COMPONENT_LABELS: Readonly<Record<string, string>> = {
  ibc_br_sa_yoy: 'IBC-Br dessazonalizado: variação anual',
  desemprego: 'Desemprego', rendimento_real_yoy: 'Rendimento real: variação anual',
  abs_ipca_12m_target: 'Distância absoluta IPCA à meta',
  abs_focus_12m_target: 'Distância absoluta Focus à meta',
  juro_real_ex_ante_1y: 'Juro real ex-ante de um ano', inclinacao_di_5y_1y: 'Inclinação DI 5 anos × 1 ano',
  saldo_primario_12m_pib: 'Saldo primário em 12 meses', dbgg_pib: 'Dívida bruta / PIB',
  dbgg_yoy_pp: 'Dívida bruta: variação anual', saldo_nominal_12m_pib: 'Saldo nominal em 12 meses',
  inadimplencia_pf: 'Inadimplência PF', comprometimento_renda: 'Comprometimento de renda',
  icc_pf: 'Custo do crédito PF', spread_credito_pf_selic: 'Spread crédito PF × Selic',
  cobertura_externa: 'Cobertura externa', conta_corrente_12m_pib: 'Conta corrente em 12 meses',
  reservas_internacionais: 'Reservas internacionais',
}

const REASON_LABELS: Readonly<Record<string, string>> = {
  insufficient_coverage: 'Cobertura insuficiente', insufficient_history: 'Histórico insuficiente',
  required_input_missing: 'Insumo obrigatório ausente', stale_input: 'Insumo vencido no corte',
  reference_month_mismatch: 'Períodos incompatíveis', current_account_non_negative: 'Conta corrente sem déficit',
  historical_lag_missing: 'Par histórico ausente', invalid_denominator: 'Denominador inválido',
  inflation_target_missing: 'Meta ausente', inflation_target_not_observed: 'Meta ainda não observada no corte',
  derived_snapshot_missing: 'Snapshot derivado ausente', derived_snapshot_unavailable: 'Snapshot derivado indisponível',
}

const UNIT_LABELS: Readonly<Record<string, string>> = {
  pct: '%', pct_annual: '% a.a.', pct_gdp: '% do PIB', pp: 'p.p.', ratio: 'razão', usd_million: 'US$ milhões',
}

const CLASSIFICATION_LABELS = {
  muito_fraco: 'Muito fraco', fraco: 'Fraco', neutro: 'Neutro', forte: 'Forte', muito_forte: 'Muito forte',
} as const

export const componentLabel = (key: string): string => COMPONENT_LABELS[key] ?? key
export const reasonLabel = (reason: string): string => REASON_LABELS[reason] ?? reason
export const unitLabel = (unit: string): string => UNIT_LABELS[unit] ?? unit
export const numberLabel = (value: number | null): string => value === null
  ? 'Indisponível' : value.toLocaleString('pt-BR', { maximumSignificantDigits: 21 })
export const coverageLabel = (coverage: number): string => coverage.toLocaleString('pt-BR', { style: 'percent', maximumFractionDigits: 2 })
export const classificationLabel = (classification: MacroScorePayload['classification']): string => classification === null
  ? 'Sem classificação' : CLASSIFICATION_LABELS[classification]
export const timestampLabel = (value: string | null): string => value === null ? 'Não disponível'
  : `${new Date(value).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC' })} UTC`
