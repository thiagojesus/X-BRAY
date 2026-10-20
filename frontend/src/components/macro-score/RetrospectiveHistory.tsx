import { useEffect, useState } from 'react'

type CoverageResponse = {
  readonly status: 'ok' | 'unavailable'
  readonly source_basis: 'retrospective_reference'
  readonly point_in_time: false
  readonly di_history_available: boolean
  readonly coverage: Readonly<Record<string, {
    readonly distinct_months: number
    readonly eligible_distinct_months?: number
    readonly percentile_eligible: boolean
  }>>
}

function isCoverageResponse(value: unknown): value is CoverageResponse {
  if (typeof value !== 'object' || value === null) return false
  const payload = value as Partial<CoverageResponse>
  return payload.source_basis === 'retrospective_reference'
    && payload.point_in_time === false
    && typeof payload.di_history_available === 'boolean'
    && typeof payload.coverage === 'object'
    && payload.coverage !== null
}

export function RetrospectiveHistory() {
  const [data, setData] = useState<CoverageResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/macro-score/retrospective-history?start=2016-01&end=2026-12', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('Histórico retrospectivo indisponível.')
        const payload: unknown = await response.json()
        if (!isCoverageResponse(payload)) throw new Error('Resposta de histórico retrospectivo inválida.')
        return payload
      })
      .then(setData)
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Falha ao consultar histórico retrospectivo.')
      })
    return () => controller.abort()
  }, [])

  return (
    <section className="macro-panel" aria-labelledby="retrospective-history-title">
      <h2 id="retrospective-history-title">Referências históricas reconstruídas</h2>
      <p>Valores oficiais coletados retrospectivamente; não representam o que era conhecido em cada mês e não são backtest. Não se misturam ao histórico de snapshots de produção.</p>
      {error !== null && <p>{error}</p>}
      {data === null && error === null && <p>Consultando cache...</p>}
      {data !== null && Object.keys(data.coverage).length === 0 && <p role="status">Nenhum backfill retrospectivo foi publicado neste cache.</p>}
      {data !== null && Object.keys(data.coverage).length > 0 && (
        <ul>
          {Object.entries(data.coverage).map(([key, coverage]) => (
            <li key={key}>{key}: {coverage.distinct_months} meses candidatos
              {coverage.eligible_distinct_months !== undefined && ` · ${coverage.eligible_distinct_months} elegíveis`}
              {coverage.percentile_eligible ? ' · elegível para percentil' : ' · não elegível para percentil'}</li>
          ))}
        </ul>
      )}
      <p>DI: {data?.di_history_available ? 'há valores candidatos retrospectivos, não point-in-time. O vencimento é confrontado com o cadastro de instrumentos DI1 (XprtnDt), separado do último dia de negociação (TradgEndDt), com identidade e validade temporal quando disponíveis. Apenas meses com proveniência de cadastro validada contam para o mínimo de 24 meses elegíveis por componente; não se misturam a snapshots de produção.' : 'sem histórico retrospectivo equivalente disponível; nenhum proxy foi usado.'}</p>
    </section>
  )
}
