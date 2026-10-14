import type { DerivedHistoryResponse } from '../components/derived-history/schema'

export const derivedHistoryFixture: DerivedHistoryResponse = {
  status: 'ok',
  window: {
    start_month: '2024-11',
    end_month: '2026-10',
    cutoff: '2026-10-14T14:00:00Z',
  },
  data: {
    juro_real_ex_ante_1y: [],
    cobertura_externa: [],
    variacao_saldo_primario_12m: [
      {
        month: '2026-08',
        snapshot: {
          key: 'variacao_saldo_primario_12m',
          value: null,
          unit: 'pp',
          as_of: '2026-08-31T14:00:00Z',
          methodology_version: '1.0.0',
          formula: 'fixture',
          inputs: {},
          reason: 'primary_balance_previous_month_missing',
          reference_date: '2026-08-31',
        },
      },
    ],
    spread_credito_pf_selic: [
      {
        month: '2026-06',
        snapshot: {
          key: 'spread_credito_pf_selic',
          value: 2.1,
          unit: 'pp',
          as_of: '2026-06-30T14:00:12.345678+00:00',
          methodology_version: '1.0.0',
          formula: 'fixture',
          inputs: {},
          reason: null,
          reference_date: '2026-06-30',
        },
      },
      {
        month: '2026-10',
        snapshot: {
          key: 'spread_credito_pf_selic',
          value: 1.8,
          unit: 'pp',
          as_of: '2026-10-14T14:00:00Z',
          methodology_version: '1.0.0',
          formula: 'fixture',
          inputs: {},
          reason: null,
          reference_date: '2026-10-14',
        },
      },
    ],
    distancia_focus_meta: [],
    inclinacao_di_5y_1y: [
      {
        month: '2026-09',
        snapshot: {
          key: 'inclinacao_di_5y_1y',
          value: -0.4,
          unit: 'pp',
          as_of: '2026-09-30T14:00:00Z',
          methodology_version: '1.0.0',
          formula: 'fixture',
          inputs: {},
          reason: null,
          reference_date: '2026-09-30',
        },
      },
    ],
  },
}
