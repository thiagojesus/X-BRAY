import { BarChart3 } from 'lucide-react'
import { lazy, Suspense, useMemo, useState } from 'react'
import {
  buildDerivedHistoryRows,
  DERIVED_HISTORY_UNITS,
  type DerivedHistoryUnit,
  derivedHistorySeries,
  formatDerivedHistoryCutoff,
  formatDerivedHistoryMonth,
  formatDerivedHistoryValue,
} from './historyUtils'
import type { DerivedHistoryKey } from './schema'
import { useDerivedHistory } from './useDerivedHistory'
import './derived-history.css'

const DerivedHistoryChart = lazy(() => import('./DerivedHistoryChart').then(module => ({ default: module.DerivedHistoryChart })))

function referenceDateLabel(value: string): string {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString('pt-BR', { timeZone: 'UTC' })
}

export function DerivedHistory() {
  const history = useDerivedHistory()
  const [unit, setUnit] = useState<DerivedHistoryUnit>('pp')
  const [selected, setSelected] = useState<readonly DerivedHistoryKey[]>(() => derivedHistorySeries('pp').map(series => series.key))
  const rows = useMemo(() => history.data === null ? [] : buildDerivedHistoryRows(history.data), [history.data])
  const unitSeries = derivedHistorySeries(unit)
  const selectedKeys = new Set(selected)
  const visibleSeries = unitSeries.filter(series => selectedKeys.has(series.key) && (history.data?.data[series.key].length ?? 0) > 0)
  const hasNumericValues = rows.some(row => visibleSeries.some(series => row.points[series.key]?.snapshot.value !== null && row.points[series.key]?.snapshot.value !== undefined))

  function chooseUnit(nextUnit: DerivedHistoryUnit) {
    setUnit(nextUnit)
    setSelected(derivedHistorySeries(nextUnit).map(series => series.key))
  }

  function toggleSeries(key: DerivedHistoryKey, checked: boolean) {
    setSelected(current => checked
      ? [...current, key]
      : current.filter(item => item !== key))
  }

  return (
    <section className="derived-section derived-history" aria-labelledby="derived-history-title">
      <div className="derived-section-heading">
        <BarChart3 size={19} aria-hidden="true" />
        <h2 id="derived-history-title">Histórico dos indicadores derivados</h2>
      </div>
      <p className="derived-history-intro">Snapshots de produção persistidos nos últimos 24 meses. Meses sem snapshot e resultados indisponíveis permanecem como lacunas; nenhum valor é interpolado ou recalculado.</p>
      {history.loading && <p role="status">Carregando histórico persistido...</p>}
      {history.error && <p role="alert">Erro no histórico: {history.error}</p>}
      {history.isStale && <p role="status">Exibindo a última resposta válida; ela pode estar desatualizada.</p>}
      <button
        className="derived-history-retry"
        type="button"
        disabled={history.refreshing}
        onClick={() => { void history.refetch() }}
      >
        Tentar histórico novamente
      </button>
      {history.data?.status === 'unavailable' && (
        <p className="derived-history-empty" role="status">Nenhum snapshot de produção persistido nesta janela.</p>
      )}
      {history.data?.status === 'ok' && (
        <>
          <p className="derived-history-cutoff">Janela: {history.data.window.start_month} a {history.data.window.end_month} · Corte de leitura: {formatDerivedHistoryCutoff(history.data.window.cutoff)}</p>
          <fieldset className="derived-history-unit-controls">
            <legend>Unidade dos indicadores</legend>
            {DERIVED_HISTORY_UNITS.map(option => (
              <button
                key={option.value}
                type="button"
                aria-pressed={unit === option.value}
                onClick={() => chooseUnit(option.value)}
              >
                {option.label}
              </button>
            ))}
          </fieldset>
          <fieldset className="derived-history-series-controls">
            <legend>Séries do gráfico · mesma unidade</legend>
            {unitSeries.map(series => {
              const available = history.data?.data[series.key].length !== 0
              return (
                <label key={series.key} className={available ? '' : 'is-empty'}>
                  <input
                    type="checkbox"
                    checked={available && selectedKeys.has(series.key)}
                    disabled={!available}
                    onChange={event => toggleSeries(series.key, event.currentTarget.checked)}
                  />
                  <span aria-hidden="true" className="derived-history-swatch" style={{ backgroundColor: series.color }} />
                  <span>{series.label}</span>
                  {!available && <small>Sem snapshots</small>}
                </label>
              )
            })}
          </fieldset>
          {visibleSeries.length === 0 && <p className="derived-history-empty" role="status">Nenhuma série selecionada tem snapshots nesta janela.</p>}
          {visibleSeries.length > 0 && !hasNumericValues && (
            <p className="derived-history-empty" role="status">Há snapshots, mas não há valores numéricos disponíveis para as séries selecionadas.</p>
          )}
          {visibleSeries.length > 0 && hasNumericValues && (
            <Suspense fallback={<p role="status">Carregando gráfico mensal do histórico...</p>}>
              <DerivedHistoryChart rows={rows} series={visibleSeries} unit={unit} />
            </Suspense>
          )}
          {visibleSeries.length > 0 && (
            <details className="derived-history-values">
              <summary>Valores acessíveis e datas de referência</summary>
              <div className="derived-history-table-wrap">
                <table>
                  <caption>Último snapshot mensal disponível por série · {DERIVED_HISTORY_UNITS.find(option => option.value === unit)?.label}</caption>
                  <thead>
                    <tr><th scope="col">Mês</th>{visibleSeries.map(series => <th key={series.key} scope="col">{series.label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {rows.map(row => (
                      <tr key={row.month}>
                        <th scope="row">{row.month}</th>
                        {visibleSeries.map(series => {
                          const point = row.points[series.key]
                          const snapshot = point?.snapshot
                          return (
                            <td key={series.key}>
                              {snapshot === undefined ? <span>Sem snapshot</span> : (
                                <>
                                  <span>{snapshot.value === null ? `Indisponível (${snapshot.reason ?? 'sem motivo registrado'})` : formatDerivedHistoryValue(snapshot.value, series.unit)}</span>
                                  <small>Referência: {referenceDateLabel(snapshot.reference_date)} · Corte: {formatDerivedHistoryCutoff(snapshot.as_of)}</small>
                                </>
                              )}
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ol className="derived-history-list">
                {rows.map(row => (
                  <li key={row.month}>
                    <h3>{formatDerivedHistoryMonth(row.month)}</h3>
                    <dl>
                      {visibleSeries.map(series => {
                        const snapshot = row.points[series.key]?.snapshot
                        return (
                          <div key={series.key}>
                            <dt>{series.label}</dt>
                            <dd>{snapshot === undefined ? 'Sem snapshot' : snapshot.value === null ? `Indisponível (${snapshot.reason ?? 'sem motivo registrado'})` : formatDerivedHistoryValue(snapshot.value, series.unit)}</dd>
                            {snapshot !== undefined && <dd>Referência: {referenceDateLabel(snapshot.reference_date)} · Corte: {formatDerivedHistoryCutoff(snapshot.as_of)}</dd>}
                          </div>
                        )
                      })}
                    </dl>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </>
      )}
    </section>
  )
}
