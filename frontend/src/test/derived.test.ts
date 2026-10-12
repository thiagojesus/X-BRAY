import { describe, expect, it } from 'vitest'
import { parseAdditionalIndicatorResponse } from '../types/derived'

const input = {
  value: 2.5,
  unit: 'pct_gdp',
  reference_period: '2026-08-01',
  available_at: '2026-09-27T10:00:00+00:00',
  source: 'fixture source',
}

const snapshot = {
  key: 'cobertura_externa',
  value: 1.2,
  unit: 'ratio',
  as_of: '2026-09-30T12:00:00+00:00',
  methodology_version: '1.0.0',
  formula: 'idp_12m_pib / abs(conta_corrente_12m_pib)',
  reference_date: null,
  reason: null,
  inputs: { idp_12m_pib: input },
}

describe('parseAdditionalIndicatorResponse', () => {
  it('parses an available snapshot with optional provenance omitted', () => {
    // Given
    const response = { status: 'ok', data: { cobertura_externa: snapshot } }

    // When
    const parsed = parseAdditionalIndicatorResponse(response)

    // Then
    expect(parsed.kind).toBe('populated')
    if (parsed.kind !== 'populated') return
    expect(parsed.snapshots.cobertura_externa).toMatchObject({
      kind: 'available',
      value: 1.2,
      reference_date: null,
      inputs: { idp_12m_pib: { source: 'fixture source' } },
    })
  })

  it('rejects an input with empty source text', () => {
    // Given
    const malformed = { ...snapshot, inputs: { idp_12m_pib: { ...input, source: '' } } }

    // When
    const parsed = parseAdditionalIndicatorResponse({ status: 'ok', data: { cobertura_externa: malformed } })

    // Then
    expect(parsed.kind).toBe('populated')
    if (parsed.kind !== 'populated') return
    expect(parsed.snapshots.cobertura_externa).toEqual({ kind: 'invalid', key: 'cobertura_externa' })
  })

  it('rejects a snapshot with an invalid reference date', () => {
    // Given
    const malformed = { ...snapshot, reference_date: '2026-02-30' }

    // When
    const parsed = parseAdditionalIndicatorResponse({ status: 'ok', data: { cobertura_externa: malformed } })

    // Then
    expect(parsed.kind).toBe('populated')
    if (parsed.kind !== 'populated') return
    expect(parsed.snapshots.cobertura_externa).toEqual({ kind: 'invalid', key: 'cobertura_externa' })
  })

  it('rejects an unavailable envelope that includes snapshots', () => {
    // Given
    const response = { status: 'unavailable', data: { cobertura_externa: snapshot } }

    // When
    const parsed = parseAdditionalIndicatorResponse(response)

    // Then
    expect(parsed).toEqual({ kind: 'invalid_envelope' })
  })

  it('rejects an ok envelope with no snapshots', () => {
    // Given
    const response = { status: 'ok', data: {} }

    // When
    const parsed = parseAdditionalIndicatorResponse(response)

    // Then
    expect(parsed).toEqual({ kind: 'invalid_envelope' })
  })
})
