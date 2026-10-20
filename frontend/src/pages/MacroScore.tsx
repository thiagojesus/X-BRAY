import { FlaskConical } from 'lucide-react'
import { useState } from 'react'
import { currentScoreMonth } from '../components/macro-score/history'
import { MacroScoreSummary } from '../components/macro-score/MacroScoreSummary'
import { ScoreDimensions } from '../components/macro-score/ScoreDimensions'
import { ScoreHistory } from '../components/macro-score/ScoreHistory'
import { ScoreMethodology } from '../components/macro-score/ScoreMethodology'
import { ScoreOverview } from '../components/macro-score/ScoreOverview'
import { RetrospectiveHistory } from '../components/macro-score/RetrospectiveHistory'
import { macroScoreResponseSchema } from '../components/macro-score/schema'
import { useScoreResource } from '../components/macro-score/useScoreResource'
import '../components/macro-score/macro-score.css'

export function MacroScore() {
  const latest = useScoreResource('/api/macro-score', macroScoreResponseSchema)
  const [currentMonth] = useState(() => currentScoreMonth(new Date()))
  const snapshot = latest.data?.data ?? null

  return (
    <div className="page macro-page">
      <header className="macro-page-header">
        <div><h1>Macro Score</h1><p>Dimensões com cobertura e insumos rastreáveis; a dimensão monetária DI segue incompleta.</p></div>
        <span className="macro-beta"><FlaskConical size={16} aria-hidden="true" /> Beta · {snapshot?.methodology_version ?? '0.3.0-beta'}</span>
      </header>
      <p>Índice descritivo, não uma previsão ou recomendação. Valores e classificação são os persistidos no servidor.</p>
      <div className="macro-notice macro-panel" role="note">
        O histórico de componentes derivados de DI é somente forward-captured. A reconstrução de séries oficiais não-DI aparece separadamente e não representa conhecimento point-in-time. Não há proxy substituto; séries com menos de 24 meses distintos não recebem percentil ou score.
      </div>
      <ScoreMethodology snapshot={snapshot} />
      <RetrospectiveHistory />
      {latest.data?.capabilities.macro_summary === true && <MacroScoreSummary />}
      {latest.loading && <div className="macro-notice macro-panel" role="status">Carregando Macro Score...</div>}
      {latest.error && <div className="macro-notice macro-panel" role="alert">{latest.error}</div>}
      {latest.isStale && <div className="macro-notice macro-panel" role="status">Snapshot em cache possivelmente desatualizado. A última resposta válida foi preservada, incluindo cobertura e corte.</div>}
      {!latest.loading && latest.data !== null && snapshot === null && (
        <div className="macro-notice macro-panel" role="status">Sem snapshot persistido. Score, classificação, cobertura e data-base ainda não estão disponíveis.</div>
      )}
      <button type="button" className="macro-retry" disabled={latest.refreshing} onClick={() => { void latest.refetch() }}>Tentar Macro Score novamente</button>
      {snapshot !== null && <><ScoreOverview snapshot={snapshot} /><ScoreDimensions snapshot={snapshot} /></>}
      {!latest.loading && <ScoreHistory key={snapshot?.score_month ?? currentMonth} endMonth={snapshot?.score_month ?? currentMonth} />}
    </div>
  )
}
