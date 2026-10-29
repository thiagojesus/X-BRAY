import { describe, expect, it } from 'vitest'

import { parseCreditResponse } from '../types/credit'
import { syntheticModalities } from './creditoModalities'

const baseResponse = {
  status: 'ok',
  data: {},
  metadata: {},
  unavailable: [],
}

describe('parseCreditResponse', () => {
  it('rejects malformed core contracts and retains a valid legacy contract without modalities', () => {
    expect(parseCreditResponse(null)).toBeNull()
    expect(parseCreditResponse({ ...baseResponse, status: 'unknown' })).toBeNull()
    expect(parseCreditResponse(baseResponse)?.modalities).toBeUndefined()
    expect(parseCreditResponse({ ...baseResponse, modalities: undefined })?.modalities).toEqual({})
    expect(parseCreditResponse({ ...baseResponse, modalities: null })?.modalities).toEqual({})
    expect(parseCreditResponse({ ...baseResponse, modalities: [] })?.modalities).toEqual({})
    expect(parseCreditResponse({ ...baseResponse, modalities: 'invalid' })?.modalities).toEqual({})
  })

  it('keeps valid and explicit-null known modalities, omitting malformed or unknown entries', () => {
    const parsed = parseCreditResponse({
      ...baseResponse,
      modalities: {
        carteira_pf_cheque_especial: syntheticModalities.carteira_pf_cheque_especial,
        carteira_pf_credito_pessoal_nao_consignado: null,
        carteira_pf_cartao_rotativo: { code: 20587 },
        not_a_modality: syntheticModalities.carteira_pf_cartao_parcelado,
      },
    })
    expect(parsed?.modalities?.carteira_pf_cheque_especial?.code).toBe(20573)
    expect(parsed?.modalities?.carteira_pf_credito_pessoal_nao_consignado).toBeNull()
    expect(parsed?.modalities?.carteira_pf_cartao_rotativo).toBeUndefined()
    expect(Object.keys(parsed?.modalities ?? {})).toEqual([
      'carteira_pf_cheque_especial',
      'carteira_pf_credito_pessoal_nao_consignado',
    ])
  })

  it('isolates mismatched codes and non-canonical or unsafe source links per modality', () => {
    const good = syntheticModalities.carteira_pf_cheque_especial
    const parsed = parseCreditResponse({
      ...baseResponse,
      modalities: {
        carteira_pf_cheque_especial: {
          ...good,
          code: 20574,
          source_url: 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.20574/dados',
        },
        carteira_pf_credito_pessoal_nao_consignado: {
          ...syntheticModalities.carteira_pf_credito_pessoal_nao_consignado,
          source_url: 'http://api.bcb.gov.br/dados/serie/bcdata.sgs.20574/dados',
        },
        carteira_pf_cartao_rotativo: {
          ...syntheticModalities.carteira_pf_cartao_rotativo,
          source_url: 'https://api.bcb.gov.br.evil.example/dados/serie/bcdata.sgs.20587/dados',
        },
        carteira_pf_cartao_parcelado: {
          ...syntheticModalities.carteira_pf_cartao_parcelado,
          source_url: 'javascript:alert(1)',
        },
      },
    })
    expect(parsed?.modalities?.carteira_pf_cheque_especial).toBeUndefined()
    expect(parsed?.modalities?.carteira_pf_credito_pessoal_nao_consignado).toBeUndefined()
    expect(parsed?.modalities?.carteira_pf_cartao_rotativo).toBeUndefined()
    expect(parsed?.modalities?.carteira_pf_cartao_parcelado).toBeUndefined()
    expect(Object.keys(parsed?.modalities ?? {})).toEqual([])
    const querySource = {
      ...baseResponse,
      modalities: {
        carteira_pf_cartao_rotativo: {
          ...syntheticModalities.carteira_pf_cartao_rotativo,
          source_url: 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.20587/dados?formato=json',
        },
      },
    }
    expect(parseCreditResponse(querySource)?.modalities?.carteira_pf_cartao_rotativo?.code).toBe(20587)
  })
})
