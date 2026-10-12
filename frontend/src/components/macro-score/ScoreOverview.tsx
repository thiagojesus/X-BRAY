import { Gauge } from 'lucide-react'
import { classificationLabel, coverageLabel, numberLabel, timestampLabel } from './format'
import type { MacroScorePayload } from './schema'

export function ScoreOverview({ snapshot }: { readonly snapshot: MacroScorePayload }) {
  return (
    <section className="macro-overview macro-panel" aria-labelledby="macro-overview-title">
      <div className="macro-score-reading">
        <h2 id="macro-overview-title"><Gauge size={18} aria-hidden="true" /> Score geral</h2>
        <strong className="macro-value" data-testid="macro-score-value">{numberLabel(snapshot.score)}</strong>
        <span>{snapshot.score === null ? 'Sem score elegível neste snapshot' : 'pontos (0 a 100)'}</span>
        <strong>{classificationLabel(snapshot.classification)}</strong>
      </div>
      <dl className="macro-facts">
        <div><dt>Mês-base</dt><dd>{snapshot.score_month}</dd></div>
        <div><dt>Corte de informação (as_of)</dt><dd>{timestampLabel(snapshot.as_of)}</dd></div>
        <div><dt>Cobertura das dimensões</dt><dd>{snapshot.valid_dimensions}/{snapshot.total_dimensions} · {coverageLabel(snapshot.overall_coverage)}</dd></div>
        <div><dt>Metodologia Beta</dt><dd>{snapshot.methodology_version}</dd></div>
      </dl>
      <p className="macro-coverage-state" role="status">
        {snapshot.score === null ? 'Score indisponível com snapshot persistido.'
          : snapshot.overall_coverage === 1 ? 'Cobertura completa das dimensões.' : 'Cobertura parcial das dimensões.'}
        {' '}A cobertura de componentes está indicada em cada dimensão.
      </p>
      {snapshot.warnings.length > 0 && (
        <div className="macro-warnings">
          <h3>Avisos do snapshot</h3>
          <ul>{snapshot.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>
        </div>
      )}
    </section>
  )
}
