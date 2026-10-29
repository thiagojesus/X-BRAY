import { describe, expect, it } from 'vitest'

import { syntheticModalities } from './creditoModalities'
import { mergeCreditExposureHistory } from '../utils/creditExposureCharts'

describe('mergeCreditExposureHistory', () => {
  it('aligns backend history by month with null holes and keeps authoritative values', () => {
    const rows = mergeCreditExposureHistory(syntheticModalities, 'value')
    expect(rows.map(row => row.date)).toEqual([
      '2025-01-01', '2025-11-01', '2025-12-01', '2026-01-01',
    ])
    expect(rows[0]?.carteira_pf_cheque_especial).toBe(100.5)
    expect(rows[0]?.carteira_pf_credito_pessoal_nao_consignado).toBe(500)
    expect(rows[0]?.carteira_pf_cartao_rotativo).toBe(70)
    expect(rows[0]?.carteira_pf_cartao_parcelado).toBe(80)
    expect(rows[1]?.carteira_pf_credito_pessoal_nao_consignado).toBeNull()
    expect(rows[3]?.carteira_pf_cheque_especial).toBe(120.5)
    expect(rows[3]?.carteira_pf_cheque_especialReferenceDate).toBe('2026-01-01')
  })

  it('uses server-supplied YoY values and does not reconstruct them from balances', () => {
    const contradictory = {
      ...syntheticModalities,
      carteira_pf_cheque_especial: {
        ...syntheticModalities.carteira_pf_cheque_especial,
        history: syntheticModalities.carteira_pf_cheque_especial.history.map(point => ({
          ...point,
          value: 9000,
          yoy_pct: point.reference === '2026-01-01' ? 19.90049751243781 : point.yoy_pct,
        })),
      },
    }
    const rows = mergeCreditExposureHistory(contradictory, 'yoy_pct')
    const latest = rows[rows.length - 1]
    expect(latest?.carteira_pf_cheque_especial).toBe(19.90049751243781)
    expect(latest?.carteira_pf_cartao_rotativo).toBe(10)
  })

  it('returns no points for an absent modality block without fabricating dates', () => {
    expect(mergeCreditExposureHistory(undefined, 'value')).toEqual([])
    expect(mergeCreditExposureHistory({}, 'yoy_pct')).toEqual([])
  })
})
