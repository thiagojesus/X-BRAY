import { lazy, Suspense, useMemo, useState } from 'react'
import { DIMENSION_LABELS, numberLabel, timestampLabel } from './format'
import { historyMonths, historyPoints } from './history'
import { DIMENSION_KEYS, type HistorySeries, historySeriesSchema, macroScoreHistorySchema } from './schema'
import { useScoreResource } from './useScoreResource'

const ScoreChart = lazy(() => import('./ScoreChart').then(module => ({ default: module.ScoreChart })))

export function ScoreHistory({ endMonth }: { readonly endMonth: string }) {
  const [series, setSeries] = useState<HistorySeries>('overall')
  const months = useMemo(() => historyMonths(endMonth), [endMonth])
  const startMonth = months[0]
  const history = useScoreResource(`/api/macro-score/history?start=${startMonth}&end=${endMonth}`, macroScoreHistorySchema)
  const title = series === 'overall' ? 'Score geral' : DIMENSION_LABELS[series]
  const points = historyPoints(months, history.data?.data ?? [], series)
  const hasValues = points.some(point => point.value !== null)

  return (
    <section className="macro-panel macro-history" aria-labelledby="macro-history-title">
      <div className="macro-history-heading">
        <div><h2 id="macro-history-title">Histórico persistido</h2><p>{startMonth} a {endMonth} · 120 meses de calendário</p></div>
        <label>Série do histórico
          <select value={series} onChange={event => setSeries(historySeriesSchema.parse(event.currentTarget.value))}>
            <option value="overall">Score geral</option>
            {DIMENSION_KEYS.map(key => <option key={key} value={key}>{DIMENSION_LABELS[key]}</option>)}
          </select>
        </label>
      </div>
      <h3>Histórico: {title}</h3>
      <p>Unidade: pontos de 0 a 100. Cada ponto é um score persistido da API; meses sem snapshot e scores indisponíveis permanecem lacunas, sem interpolação.</p>
      {history.loading && <p role="status">Carregando histórico...</p>}
      {history.error && <p role="alert">Erro no histórico: {history.error}</p>}
      {history.isStale && <p role="status">Histórico em cache possivelmente desatualizado; a última resposta válida foi preservada.</p>}
      {history.data !== null && history.data.data.length === 0 && <p role="status">Histórico vazio: nenhum snapshot persistido nesta janela.</p>}
      {history.data !== null && history.data.data.length > 0 && !hasValues && <p role="status">Nenhum score disponível para esta série nesta janela; os valores permanecem indisponíveis no histórico persistido.</p>}
      <button type="button" className="macro-retry" disabled={history.refreshing} onClick={() => { void history.refetch() }}>Tentar histórico novamente</button>
      {history.data !== null && history.data.data.length > 0 && (
        <>
          {hasValues && <Suspense fallback={<p role="status">Carregando gráfico do histórico...</p>}><ScoreChart points={points} /></Suspense>}
          <details className="macro-disclosure macro-history-table">
            <summary>Valores acessíveis: {title}</summary>
            <div className="macro-disclosure-body">
              <table>
                <caption>{title} · pontos de 0 a 100 · snapshots da API</caption>
                <thead><tr><th scope="col">Mês-base</th><th scope="col">Score</th><th scope="col">Estado / corte</th></tr></thead>
                <tbody>{points.map(point => (
                  <tr key={point.month}>
                    <th scope="row">{point.month}</th>
                    <td>{numberLabel(point.value)}</td>
                    <td>{point.snapshot === null ? 'Sem snapshot' : point.value === null ? 'Score indisponível' : timestampLabel(point.snapshot.as_of)}</td>
                  </tr>
                ))}</tbody>
              </table>
              <ul className="macro-history-list" aria-label={`${title} · pontos de 0 a 100 · snapshots da API`}>
                {points.map(point => (
                  <li key={point.month}>
                    <dl className="macro-facts">
                      <div><dt>Mês-base</dt><dd>{point.month}</dd></div>
                      <div><dt>Score</dt><dd>{numberLabel(point.value)}</dd></div>
                      <div><dt>Estado</dt><dd>{point.snapshot === null ? 'Sem snapshot' : point.value === null ? 'Score indisponível' : 'Disponível'}</dd></div>
                      <div><dt>Corte de informação (as_of)</dt><dd>{timestampLabel(point.snapshot?.as_of ?? null)}</dd></div>
                    </dl>
                  </li>
                ))}
              </ul>
            </div>
          </details>
        </>
      )}
    </section>
  )
}
