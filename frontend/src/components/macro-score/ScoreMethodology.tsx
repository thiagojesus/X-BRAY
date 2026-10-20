import { Sigma } from 'lucide-react'
import { timestampLabel } from './format'
import type { MacroScorePayload } from './schema'

export function ScoreMethodology({ snapshot }: { readonly snapshot: MacroScorePayload | null }) {
  return (
    <details className="macro-methodology macro-disclosure macro-panel">
      <summary><Sigma size={18} aria-hidden="true" /> Metodologia Macro Score Beta</summary>
      <div className="macro-disclosure-body">
        <h2>Metodologia {snapshot?.methodology_version ?? '0.3.0-beta'}</h2>
        <p>Índice descritivo de 0 a 100. Não é previsão, recomendação de investimento nem estimativa causal. A tela apenas apresenta valores persistidos pela API.</p>
        <ul>
          <li>Normalização por componente: janela de 120 meses, ao menos 24 meses elegíveis, winsorização em 2% e 98% por quantis tipo 7 e percentil empírico midrank.</li>
          <li>A direção metodológica orienta o percentil: maior é mais favorável ou menor é mais favorável. Pesos de componentes iguais; dimensões válidas têm peso igual.</li>
          <li>Cobertura mínima: Atividade 2/3, Inflação 2/2, Monetária 2/2, Fiscal 3/4, Crédito 3/4 e Externa 2/3. O score geral exige ao menos 5/6 dimensões.</li>
          <li>Faixas: [0,20) muito fraco; [20,40) fraco; [40,60) neutro; [60,80) forte; [80,100] muito forte. A classificação exibida é a retornada pelo servidor.</li>
          <li>Somente insumos conhecidos até as_of são elegíveis. Mês-base segue America/Sao_Paulo; cálculo e disponibilidade conservam timestamps com fuso.</li>
          <li>As séries mensais de crédito admitem referências até dois meses-calendário anteriores ao mês do score, conforme a cadência de divulgação do BCB. as_of limita o conhecimento dos insumos forward; calculado_em registra a conclusão.</li>
          <li>Em 0.3.0-beta, séries históricas BCB/Focus reconstruídas retrospectivamente podem compor a normalização atual. São valores hoje conhecidos, não vintages point-in-time; cada componente informa sua base histórica. Snapshots mensais de produção não são retrodatados nem substituídos, e lacunas não são interpoladas.</li>
        </ul>
        {snapshot === null ? <p>Proveniência do cálculo ficará disponível quando houver um snapshot persistido.</p> : (
          <section aria-labelledby="macro-provenance-title">
            <h3 id="macro-provenance-title">Proveniência do snapshot</h3>
            <dl className="macro-facts">
              <div><dt>Calculado em</dt><dd>{timestampLabel(snapshot.calculated_at)}</dd></div>
              <div><dt>Corte (as_of)</dt><dd>{timestampLabel(snapshot.as_of)}</dd></div>
              <div><dt>Mês-base</dt><dd>{snapshot.score_month}</dd></div>
              <div><dt>Hash de entradas</dt><dd>{snapshot.inputs_hash}</dd></div>
              <div><dt>Ciclo de cálculo</dt><dd>{snapshot.calculation_cycle_id}</dd></div>
              <div><dt>Origem</dt><dd>{snapshot.origin}</dd></div>
            </dl>
          </section>
        )}
      </div>
    </details>
  )
}
