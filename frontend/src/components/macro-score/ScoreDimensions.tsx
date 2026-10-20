import { componentLabel, coverageLabel, DIMENSION_LABELS, DIRECTION_LABELS, numberLabel, reasonLabel, timestampLabel, unitLabel } from './format'
import { DIMENSION_KEYS, type MacroScoreComponent, type MacroScoreInput, type MacroScorePayload } from './schema'

function InputProvenance({ inputKey, input }: { readonly inputKey: string; readonly input: MacroScoreInput }) {
  return (
    <section className="macro-input" aria-label={`Insumo ${inputKey}`}>
      <h5>{inputKey}</h5>
      <dl className="macro-facts">
        <div><dt>Valor / unidade</dt><dd>{numberLabel(input.value)} {unitLabel(input.unit)} ({input.unit})</dd></div>
        <div><dt>Fonte</dt><dd>{input.source}</dd></div>
        <div><dt>Referência</dt><dd>{input.reference_period}</dd></div>
        <div><dt>Disponível em</dt><dd>{timestampLabel(input.available_at)}</dd></div>
        {Object.entries(input.metadata).map(([key, value]) => (
          <div key={key}><dt>{key}</dt><dd>{value === null ? 'Não informado' : String(value)}</dd></div>
        ))}
      </dl>
    </section>
  )
}

export function ScoreComponent({ component }: { readonly component: MacroScoreComponent }) {
  return (
    <section className="macro-component" aria-label={componentLabel(component.key)}>
      <h4>{componentLabel(component.key)}</h4>
      <p className="macro-component-key">{component.key}</p>
      <dl className="macro-facts">
        <div><dt>Valor</dt><dd>{numberLabel(component.value)} {unitLabel(component.unit)}</dd></div>
        <div><dt>Unidade da API</dt><dd>{component.unit}</dd></div>
        <div><dt>Score / 100</dt><dd>{numberLabel(component.score)}</dd></div>
        <div><dt>Percentil / 100</dt><dd>{numberLabel(component.percentile)}</dd></div>
        <div><dt>Direção</dt><dd>{DIRECTION_LABELS[component.direction]}</dd></div>
        <div><dt>Peso</dt><dd>{component.weight}</dd></div>
        <div><dt>Referência</dt><dd>{component.reference_period ?? 'Não disponível'}</dd></div>
        <div><dt>Disponível em</dt><dd>{timestampLabel(component.available_at)}</dd></div>
        <div><dt>Freshness</dt><dd>{component.reason === 'stale_input' ? 'Vencido no corte (backend)' : 'Sem flag separada na API; elegibilidade avaliada no backend'}</dd></div>
        <div><dt>Motivo de ausência</dt><dd>{component.reason === null ? 'Nenhum' : `${reasonLabel(component.reason)} (${component.reason})`}</dd></div>
      </dl>
      <details className="macro-disclosure">
        <summary>Insumos e método: {componentLabel(component.key)}</summary>
        <div className="macro-disclosure-body">
          <p>Metodologia: {component.methodology_version}</p>
          <code>{component.formula}</code>
          <dl className="macro-facts">
            <div><dt>Observações históricas</dt><dd>{component.history_count} · {component.history_basis === 'retrospective_reference' ? 'referência retrospectiva' : 'captura forward'}</dd></div>
            <div><dt>Hash do histórico</dt><dd>{component.history_fingerprint}</dd></div>
          </dl>
          {Object.entries(component.inputs).length === 0
            ? <p>Nenhum insumo disponível registrado neste componente.</p>
            : Object.entries(component.inputs).map(([key, input]) => <InputProvenance key={key} inputKey={key} input={input} />)}
        </div>
      </details>
    </section>
  )
}

export function ScoreDimensions({ snapshot }: { readonly snapshot: MacroScorePayload }) {
  return (
    <section aria-labelledby="macro-dimensions-title">
      <h2 id="macro-dimensions-title" className="macro-section-title">Seis dimensões</h2>
      <div className="macro-dimension-grid">
        {DIMENSION_KEYS.map(key => {
          const dimension = snapshot.dimensions[key]
          return (
            <article key={key} className="macro-dimension macro-panel" aria-labelledby={`macro-${key}-title`}>
              <header><h3 id={`macro-${key}-title`}>{DIMENSION_LABELS[key]}</h3><span>{dimension.score === null ? 'Indisponível' : dimension.coverage === 1 ? 'Disponível' : 'Cobertura parcial'}</span></header>
              <strong className="macro-value">{numberLabel(dimension.score)}{dimension.score !== null && <small> / 100</small>}</strong>
              <p>Cobertura: {dimension.valid_components}/{dimension.total_components} componentes · {coverageLabel(dimension.coverage)}</p>
              {dimension.reason !== null && <p>{reasonLabel(dimension.reason)} ({dimension.reason})</p>}
              <details className="macro-disclosure">
                <summary>Ver componentes de {DIMENSION_LABELS[key]}</summary>
                <div className="macro-disclosure-body">
                  {dimension.components.map(componentKey => <ScoreComponent key={componentKey} component={snapshot.components[componentKey]} />)}
                </div>
              </details>
            </article>
          )
        })}
      </div>
    </section>
  )
}
