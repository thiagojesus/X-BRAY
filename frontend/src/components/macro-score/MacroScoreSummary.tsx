import { useEffect, useState } from 'react'
import { z } from 'zod'
import { numberLabel } from './format'
import {
  type MacroScoreSummaryPayload,
  macroScoreSummaryResponseSchema,
  macroScoreSummaryUnavailableSchema,
} from './schema'

type SummaryState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly summary: MacroScoreSummaryPayload }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'error'; readonly message: string }

class SummaryHttpError extends Error {
  readonly name = 'SummaryHttpError'

  constructor(readonly status: number) {
    super(`HTTP ${status}`)
  }
}

function signedNumberLabel(value: number): string {
  return `${value > 0 ? '+' : ''}${numberLabel(value)}`
}

export function MacroScoreSummary() {
  const [state, setState] = useState<SummaryState>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    const load = async (): Promise<void> => {
      try {
        const response = await fetch('/api/macro-score/summary', { signal: controller.signal })
        if (response.status === 409) {
          macroScoreSummaryUnavailableSchema.parse(await response.json())
          if (!controller.signal.aborted) setState({ kind: 'unavailable' })
          return
        }
        if (!response.ok) throw new SummaryHttpError(response.status)
        const result = macroScoreSummaryResponseSchema.parse(await response.json())
        if (!controller.signal.aborted) setState({ kind: 'ready', summary: result.data })
      } catch (error: unknown) {
        if (controller.signal.aborted) return
        if (error instanceof SummaryHttpError) {
          setState({ kind: 'error', message: `Falha de transporte: HTTP ${error.status}.` })
        } else if (error instanceof z.ZodError || error instanceof SyntaxError) {
          setState({ kind: 'error', message: 'Resposta inválida da API; dados rejeitados.' })
        } else if (error instanceof Error) {
          setState({ kind: 'error', message: `Falha de transporte: ${error.message}.` })
        } else {
          throw error
        }
      }
    }
    void load()
    return () => controller.abort()
  }, [])

  return (
    <section className="macro-summary macro-panel" aria-labelledby="macro-summary-title">
      <h2 id="macro-summary-title">Resumo Macro Score</h2>
      {state.kind === 'loading' && <p role="status">Carregando resumo...</p>}
      {state.kind === 'unavailable' && (
        <p role="status">Resumo indisponível: são necessários dois ciclos de produção equivalentes.</p>
      )}
      {state.kind === 'error' && <p role="alert">{state.message}</p>}
      {state.kind === 'ready' && <SummaryDetails summary={state.summary} />}
    </section>
  )
}

function SummaryDetails({ summary }: { readonly summary: MacroScoreSummaryPayload }) {
  return (
    <>
      <p>{summary.summary}</p>
      <dl className="macro-summary-facts">
        <div><dt>Score anterior · {summary.previous_score_month}</dt><dd>{numberLabel(summary.previous_score)}</dd></div>
        <div><dt>Score atual · {summary.current_score_month}</dt><dd>{numberLabel(summary.current_score)}</dd></div>
        <div><dt>Variação do score</dt><dd>{signedNumberLabel(summary.score_delta)}</dd></div>
      </dl>
      <p className="macro-summary-caveat">Variação observada, sem atribuição causal. Não é previsão nem recomendação.</p>
    </>
  )
}
