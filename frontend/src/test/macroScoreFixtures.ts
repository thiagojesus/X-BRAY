import { macroScoreHistorySchema, macroScoreResponseSchema, macroScoreSchema } from '../components/macro-score/schema'

const input = {
  value: 6.2, unit: 'pct', reference_period: '2026-08-01',
  available_at: '2026-09-28T10:00:00+00:00', source: 'BCB SGS',
  metadata: { provider_code: 24369, availability_basis: 'first_seen_at', first_seen_at: '2026-09-28T10:00:00+00:00' },
} as const

const component = {
  value: 6.2, unit: 'pct', reference_period: '2026-08-01', available_at: '2026-09-28T10:00:00+00:00',
  methodology_version: '0.1.0-beta', formula: 'desemprego', direction: 'lower_is_better', weight: 1,
  percentile: 38.75, score: 61.25, reason: null, history_count: 24,
  history_fingerprint: 'history-fixture-602', inputs: { desemprego: input },
} as const

export const macroScoreFixture = macroScoreResponseSchema.parse({
  status: 'ok',
  capabilities: { macro_summary: false },
  data: {
    score: 73.125, classification: 'forte', score_month: '2026-09',
    as_of: '2026-09-30T12:00:00+00:00', calculated_at: '2026-09-30T12:00:02+00:00',
    methodology_version: '0.1.0-beta', overall_coverage: 1, valid_dimensions: 6, total_dimensions: 6,
    inputs_hash: '602a91f267e7b74094202b539fab6733459092254113256238587180a37179971',
    calculation_cycle_id: 'cycle-xbr-602-20260930', origin: 'test_fixture', warnings: [],
    dimensions: {
      activity: { score: 61.25, valid_components: 3, total_components: 3, coverage: 1, reason: null, components: ['ibc_br_sa_yoy', 'desemprego', 'rendimento_real_yoy'] },
      inflation: { score: 50, valid_components: 2, total_components: 2, coverage: 1, reason: null, components: ['abs_ipca_12m_target', 'abs_focus_12m_target'] },
      monetary: { score: 48, valid_components: 2, total_components: 2, coverage: 1, reason: null, components: ['juro_real_ex_ante_1y', 'inclinacao_di_5y_1y'] },
      fiscal: { score: 32, valid_components: 4, total_components: 4, coverage: 1, reason: null, components: ['saldo_primario_12m_pib', 'dbgg_pib', 'dbgg_yoy_pp', 'saldo_nominal_12m_pib'] },
      credit: { score: 57, valid_components: 4, total_components: 4, coverage: 1, reason: null, components: ['inadimplencia_pf', 'comprometimento_renda', 'icc_pf', 'spread_credito_pf_selic'] },
      external: { score: 62, valid_components: 3, total_components: 3, coverage: 1, reason: null, components: ['cobertura_externa', 'conta_corrente_12m_pib', 'reservas_internacionais'] },
    },
    components: {
      ibc_br_sa_yoy: { ...component, key: 'ibc_br_sa_yoy', dimension: 'activity', value: 2.1, formula: '((ibc_br_sa_t / ibc_br_sa_t-12) - 1) * 100', direction: 'higher_is_better', inputs: { ibc_br_sa_t: { ...input, value: 110, unit: 'index' }, ibc_br_sa_previous: { ...input, value: 107.73, unit: 'index', reference_period: '2025-08-01' } } },
      desemprego: { ...component, key: 'desemprego', dimension: 'activity' },
      rendimento_real_yoy: { ...component, key: 'rendimento_real_yoy', dimension: 'activity', value: 3.2, formula: '((rendimento_real_t / rendimento_real_t-12) - 1) * 100', direction: 'higher_is_better', inputs: { rendimento_real: { ...input, value: 3520, unit: 'brl', source: 'IBGE SIDRA', metadata: { provider_code: '6392/6293', source_url: 'https://sidra.ibge.gov.br/tabela/6392' } } } },
      abs_ipca_12m_target: { ...component, key: 'abs_ipca_12m_target', dimension: 'inflation', value: 1.8, unit: 'pp', formula: 'abs(ipca_12m - inflation_target)', inputs: { ipca_12m: { ...input, value: 4.8 }, inflation_target: { ...input, value: 3, source: 'BCB', metadata: { rule_id: 'continuous_2025', source_url: 'https://www.bcb.gov.br/controleinflacao/historicometas', effective_from: '2025-01-01', effective_to: null } } } },
      abs_focus_12m_target: { ...component, key: 'abs_focus_12m_target', dimension: 'inflation', value: 1.6, unit: 'pp', formula: 'abs(expected_inflation - inflation_target)', inputs: { expected_inflation: { ...input, value: 4.6, unit: 'pct_annual', source: 'Focus IPCA 12m', metadata: { filter: "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0" } } } },
      juro_real_ex_ante_1y: { ...component, key: 'juro_real_ex_ante_1y', dimension: 'monetary', value: 6.84, unit: 'pct_annual', formula: '((1 + DI/100) / (1 + IPCA/100) - 1) * 100', inputs: { nominal_rate: { ...input, value: 11.82, unit: 'pct_annual', source: 'B3 DI', metadata: { symbol: 'DI1V27', maturity: '2027-09-25' } } } },
      inclinacao_di_5y_1y: { ...component, key: 'inclinacao_di_5y_1y', dimension: 'monetary', value: 0.8, unit: 'pp', formula: 'di_5y - di_1y', direction: 'higher_is_better', inputs: { di_5y: { ...input, value: 12.62, unit: 'pct_annual', source: 'B3 DI', metadata: { symbol: 'DI1V31', maturity: '2031-09-25' } } } },
      saldo_primario_12m_pib: { ...component, key: 'saldo_primario_12m_pib', dimension: 'fiscal', value: -0.2, unit: 'pct_gdp', formula: 'saldo_primario_12m_pib', direction: 'higher_is_better', inputs: { saldo_primario_12m_pib: { ...input, value: -0.2, unit: 'pct_gdp' } } },
      dbgg_pib: { ...component, key: 'dbgg_pib', dimension: 'fiscal', value: 78, unit: 'pct_gdp', formula: 'dbgg_pib', inputs: { dbgg_pib: { ...input, value: 78, unit: 'pct_gdp' } } },
      dbgg_yoy_pp: { ...component, key: 'dbgg_yoy_pp', dimension: 'fiscal', value: 2.5, unit: 'pp', formula: 'dbgg_pib_t - dbgg_pib_t-12', inputs: { dbgg_pib_t: { ...input, value: 78, unit: 'pct_gdp' }, dbgg_pib_previous: { ...input, value: 75.5, unit: 'pct_gdp', reference_period: '2025-08-01' } } },
      saldo_nominal_12m_pib: { ...component, key: 'saldo_nominal_12m_pib', dimension: 'fiscal', value: -8.5, unit: 'pct_gdp', formula: 'saldo_nominal_12m_pib', direction: 'higher_is_better', inputs: { saldo_nominal_12m_pib: { ...input, value: -8.5, unit: 'pct_gdp' } } },
      inadimplencia_pf: { ...component, key: 'inadimplencia_pf', dimension: 'credit', value: 5, formula: 'inadimplencia_pf', inputs: { inadimplencia_pf: { ...input, value: 5 } } },
      comprometimento_renda: { ...component, key: 'comprometimento_renda', dimension: 'credit', value: 27, formula: 'comprometimento_renda', inputs: { comprometimento_renda: { ...input, value: 27 } } },
      icc_pf: { ...component, key: 'icc_pf', dimension: 'credit', value: 29, unit: 'pct_annual', formula: 'icc_pf', inputs: { icc_pf: { ...input, value: 29, unit: 'pct_annual' } } },
      spread_credito_pf_selic: { ...component, key: 'spread_credito_pf_selic', dimension: 'credit', value: 15.4, unit: 'pp', formula: 'taxa_media_credito_pf - selic_meta', inputs: { taxa_media_credito_pf: { ...input, value: 30.4, unit: 'pct_annual' }, selic_meta: { ...input, value: 15, unit: 'pct_annual' } } },
      cobertura_externa: { ...component, key: 'cobertura_externa', dimension: 'external', value: 1.4, unit: 'ratio', formula: 'idp_12m_pib / abs(conta_corrente_12m_pib)', direction: 'higher_is_better', inputs: { idp_12m_pib: { ...input, value: 3.5, unit: 'pct_gdp' }, conta_corrente_12m_pib: { ...input, value: -2.5, unit: 'pct_gdp' } } },
      conta_corrente_12m_pib: { ...component, key: 'conta_corrente_12m_pib', dimension: 'external', value: -2.5, unit: 'pct_gdp', formula: 'conta_corrente_12m_pib', direction: 'higher_is_better', inputs: { conta_corrente_12m_pib: { ...input, value: -2.5, unit: 'pct_gdp' } } },
      reservas_internacionais: { ...component, key: 'reservas_internacionais', dimension: 'external', value: 360000, unit: 'usd_million', formula: 'reservas_internacionais', direction: 'higher_is_better', inputs: { reservas_internacionais: { ...input, value: 360000, unit: 'usd_million', metadata: { provider_code: 3546, observed: true, note: null } } } },
    },
  },
})

export const readySnapshot = macroScoreSchema.parse(macroScoreFixture.data)

export const partialScoreFixture = macroScoreResponseSchema.parse({
  status: 'ok', capabilities: { macro_summary: false }, data: {
    ...readySnapshot, score: 69.875, valid_dimensions: 5, overall_coverage: 0.8333333333333334,
    warnings: ['monetary:insufficient_coverage', 'juro_real_ex_ante_1y:stale_input'],
    dimensions: { ...readySnapshot.dimensions, monetary: { ...readySnapshot.dimensions.monetary, score: null, valid_components: 1, coverage: 0.5, reason: 'insufficient_coverage' } },
    components: { ...readySnapshot.components, juro_real_ex_ante_1y: { ...readySnapshot.components.juro_real_ex_ante_1y, value: null, reference_period: null, available_at: null, score: null, percentile: null, reason: 'stale_input', inputs: {} } },
  },
})

export const unavailableScoreFixture = macroScoreResponseSchema.parse({
  status: 'unavailable', capabilities: { macro_summary: false }, data: {
    ...readySnapshot, score: null, classification: null, valid_dimensions: 4, overall_coverage: 0.6666666666666666,
    warnings: ['inflation:insufficient_coverage', 'monetary:insufficient_coverage'],
    dimensions: { ...readySnapshot.dimensions,
      inflation: { ...readySnapshot.dimensions.inflation, score: null, valid_components: 0, coverage: 0, reason: 'insufficient_coverage' },
      monetary: { ...readySnapshot.dimensions.monetary, score: null, valid_components: 0, coverage: 0, reason: 'insufficient_coverage' },
    },
  },
})

export const macroScoreHistoryFixture = macroScoreHistorySchema.parse({
  status: 'ok', data: [
    { ...readySnapshot, score_month: '2026-06', score: 40.125, classification: 'neutro' },
    { ...unavailableScoreFixture.data, score_month: '2026-07' },
    readySnapshot,
  ],
})
