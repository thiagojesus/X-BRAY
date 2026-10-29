import type { CreditModality, CreditModalityKey } from '../types/credit'

type HistoryPoint = CreditModality['history'][number]

function historyPoint(
  reference: string,
  value: number | null,
  mom: number | null,
  yoy: number | null,
  momReason: HistoryPoint['mom_reason'],
  yoyReason: HistoryPoint['yoy_reason'],
  momBase: string,
  yoyBase: string,
): HistoryPoint {
  return {
    reference,
    value,
    mom_pct: mom,
    yoy_pct: yoy,
    mom_base_reference: momBase,
    yoy_base_reference: yoyBase,
    mom_reason: momReason,
    yoy_reason: yoyReason,
  }
}

function modality(
  code: number,
  label: string,
  history: readonly HistoryPoint[],
): CreditModality {
  const latestHistory = history[history.length - 1]
  const keyLabel = label
  return {
    code,
    label: keyLabel,
    short_label: keyLabel,
    official_label: code === 20574
      ? 'Saldo da carteira de crédito com recursos livres - Pessoas físicas - Crédito pessoal não consignado total'
      : code === 20588
        ? 'Saldo da carteira de crédito com recursos livres - Pessoas físicas - Cartão de crédito parcelado'
        : `Saldo de crédito PF - ${label}`,
    source_url: `https://api.bcb.gov.br/dados/serie/bcdata.sgs.${code}/dados`,
    frequency: 'monthly',
    unit: 'brl_million',
    universe: 'Pessoas físicas — recursos livres',
    measure: 'Saldo nominal devedor no fim do mês',
    interpretation: 'Exposição nominal selecionada; aumento não mede, isoladamente, deterioração de crédito.',
    latest: {
      reference: latestHistory?.reference ?? null,
      value: latestHistory?.value ?? null,
      reason: latestHistory?.value === null || latestHistory === undefined ? 'no_valid_observations' : null,
    },
    mom: {
      value_pct: latestHistory?.mom_pct ?? null,
      current_reference: latestHistory?.reference ?? null,
      base_reference: latestHistory?.mom_base_reference ?? null,
      base_observation_reference: latestHistory?.mom_pct === null ? null : latestHistory?.mom_base_reference ?? null,
      reason: latestHistory?.mom_reason ?? 'no_valid_observations',
    },
    yoy: {
      value_pct: latestHistory?.yoy_pct ?? null,
      current_reference: latestHistory?.reference ?? null,
      base_reference: latestHistory?.yoy_base_reference ?? null,
      base_observation_reference: latestHistory?.yoy_pct === null ? null : latestHistory?.yoy_base_reference ?? null,
      reason: latestHistory?.yoy_reason ?? 'no_valid_observations',
    },
    history: [...history],
    unavailable: history.length === 0 || latestHistory?.value === null,
  }
}

export const syntheticModalities: Record<CreditModalityKey, CreditModality> = {
  carteira_pf_cheque_especial: modality(20573, 'Cheque especial', [
    historyPoint('2025-01-01', 100.5, null, null, 'missing_base', 'missing_base', '2024-12-01', '2024-01-01'),
    historyPoint('2025-11-01', 100.5, null, 8, 'missing_base', null, '2025-10-01', '2024-11-01'),
    historyPoint('2025-12-01', 110.5, null, 10, 'missing_base', null, '2025-11-01', '2024-12-01'),
    historyPoint('2026-01-01', 120.5, 9.049773755656098, 19.90049751243781, null, null, '2025-12-01', '2025-01-01'),
  ]),
  carteira_pf_credito_pessoal_nao_consignado: modality(20574, 'Crédito pessoal não consignado', [
    historyPoint('2025-01-01', 500, null, null, 'missing_base', 'missing_base', '2024-12-01', '2024-01-01'),
    historyPoint('2025-12-01', 550, null, 12, 'missing_base', null, '2025-11-01', '2024-12-01'),
    historyPoint('2026-01-01', 575, 4.545454545454547, 15, null, null, '2025-12-01', '2025-01-01'),
  ]),
  carteira_pf_cartao_rotativo: modality(20587, 'Cartão rotativo', [
    historyPoint('2025-01-01', 70, null, null, 'missing_base', 'missing_base', '2024-12-01', '2024-01-01'),
    historyPoint('2025-12-01', 75, null, 7, 'missing_base', null, '2025-11-01', '2024-12-01'),
    historyPoint('2026-01-01', 77, 2.666666666666665, 10, null, null, '2025-12-01', '2025-01-01'),
  ]),
  carteira_pf_cartao_parcelado: modality(20588, 'Cartão parcelado com juros', [
    historyPoint('2025-01-01', 80, null, null, 'missing_base', 'missing_base', '2024-12-01', '2024-01-01'),
    historyPoint('2025-12-01', 90, null, 15, 'missing_base', null, '2025-11-01', '2024-12-01'),
    historyPoint('2026-01-01', 96, 6.666666666666665, 20, null, null, '2025-12-01', '2025-01-01'),
  ]),
}
