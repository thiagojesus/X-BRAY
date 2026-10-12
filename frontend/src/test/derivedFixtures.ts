const asOf = '2026-09-30T12:00:00+00:00'
const availableAt = '2026-09-27T10:00:00+00:00'
const focusFilter = "Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0"

function sgsInput(value: number, unit: string, code: number, referencePeriod = '2026-08-01') {
  return {
    value,
    unit,
    reference_period: referencePeriod,
    available_at: availableAt,
    source: `https://api.bcb.gov.br/series/${code}`,
    metadata: {
      availability_basis: 'first_seen_at',
      provider_code: code,
      first_seen_at: availableAt,
      source_published_at: null,
    },
  } as const
}

const focusInput = {
  value: 4.66,
  unit: 'pct_annual',
  reference_period: '2026-09-25',
  available_at: availableAt,
  source: 'Focus IPCA 12m',
  metadata: { availability_basis: 'first_seen_at', filter: focusFilter },
} as const

const diInput = {
  value: 11.82,
  unit: 'pct_annual',
  reference_period: '2026-09-25',
  available_at: availableAt,
  source: 'B3 DI',
  metadata: { availability_basis: 'first_seen_at', symbol: 'DI1V27', maturity: '2027-09-25' },
} as const

export const realRateFixture = {
  status: 'ok',
  data: {
    key: 'juro_real_ex_ante_1y',
    value: 6.841200076438003,
    unit: 'pct_annual',
    as_of: asOf,
    methodology_version: '1.0.0',
    formula: '((1 + DI/100) / (1 + IPCA/100) - 1) * 100',
    reference_date: '2026-09-25',
    historical_percentile: null,
    classification: null,
    classification_reason: 'insufficient_history',
    unavailable_reason: null,
    inputs: { expected_inflation: focusInput, nominal_rate: diInput },
  },
} as const

export const aggregateFixture = {
  status: 'ok',
  data: {
    cobertura_externa: {
      key: 'cobertura_externa',
      value: 1.4,
      unit: 'ratio',
      as_of: asOf,
      methodology_version: '1.0.0',
      formula: 'idp_12m_pib / abs(conta_corrente_12m_pib)',
      reference_date: '2026-08-01',
      reason: null,
      inputs: {
        idp_12m_pib: sgsInput(3.5, 'pct_gdp', 23080),
        conta_corrente_12m_pib: sgsInput(-2.5, 'pct_gdp', 23079),
      },
    },
    variacao_saldo_primario_12m: {
      key: 'variacao_saldo_primario_12m',
      value: 0.35,
      unit: 'pp',
      as_of: asOf,
      methodology_version: '1.0.0',
      formula: 'saldo_primario_12m_pib(t) - saldo_primario_12m_pib(t-12 meses)',
      reference_date: '2026-08-01',
      reason: null,
      inputs: {
        current_balance: sgsInput(0.15, 'pct_gdp', 5793),
        previous_balance: sgsInput(-0.2, 'pct_gdp', 5793, '2025-08-01'),
      },
    },
    spread_credito_pf_selic: {
      key: 'spread_credito_pf_selic',
      value: 10.12,
      unit: 'pp',
      as_of: asOf,
      methodology_version: '1.0.0',
      formula: 'taxa_media_credito_pf - selic_meta',
      reference_date: '2026-08-01',
      reason: null,
      inputs: {
        taxa_media_credito_pf: sgsInput(25.12, 'pct_annual', 20716),
        selic_meta: sgsInput(15, 'pct_annual', 432, '2026-08-31'),
      },
    },
    distancia_focus_meta: {
      key: 'distancia_focus_meta',
      value: 1.66,
      unit: 'pp',
      as_of: asOf,
      methodology_version: '1.0.0',
      formula: 'Focus IPCA 12m - centro da meta de inflação',
      reference_date: '2026-09-25',
      reason: null,
      inputs: {
        expected_inflation: focusInput,
        inflation_target: {
          value: 3,
          unit: 'pct_annual',
          reference_period: '2026-09-25',
          available_at: availableAt,
          source: 'https://www.bcb.gov.br/controleinflacao/historicometas',
          metadata: {
            availability_basis: 'first_seen_at',
            effective_from: '2025-01-01',
            effective_to: null,
            legal_reference: null,
            regime: 'continuous_target',
            rule_id: 'bcb-continuous-target-2025',
            source_url: 'https://www.bcb.gov.br/controleinflacao/historicometas',
            table_version: '1.0.0',
            tolerance_band_pp: 1.5,
          },
        },
      },
    },
    inclinacao_di_5y_1y: {
      key: 'inclinacao_di_5y_1y',
      value: -0.4,
      unit: 'pp',
      as_of: asOf,
      methodology_version: '1.0.0',
      formula: 'DI5Y - DI1Y',
      reference_date: '2026-09-25',
      reason: null,
      inputs: {
        di_1y: diInput,
        di_5y: {
          ...diInput,
          value: 11.42,
          metadata: { ...diInput.metadata, symbol: 'DI1V31', maturity: '2031-09-25' },
        },
      },
    },
  },
} as const

export const aggregateExpectations = [
  { key: 'cobertura_externa', title: 'Cobertura externa por IDP', value: '140,00%' },
  { key: 'variacao_saldo_primario_12m', title: 'Variação do saldo primário em 12 meses', value: '0,35 p.p.' },
  { key: 'spread_credito_pf_selic', title: 'Spread crédito PF × Selic', value: '10,12 p.p.' },
  { key: 'distancia_focus_meta', title: 'Distância Focus à meta de inflação', value: '1,66 p.p.' },
  { key: 'inclinacao_di_5y_1y', title: 'Inclinação DI 5 anos × 1 ano', value: '-0,40 p.p.' },
] as const
