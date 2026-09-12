import { useFetch } from '../hooks/useFetch'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Loading, ErrorDisplay } from '../components/Status'
import { DataQualityNotice } from '../components/DataQualityNotice'
import { KpiCard } from '../components/KpiCard'
import type { RawSeriesPoint } from '../types/indicators'
import { lastValidSeriesPoint, mergeSeriesByDate } from '../utils/series'

type ExchangeResponse = {
  readonly data?: Readonly<Record<string, readonly RawSeriesPoint[]>> | null
}

function Cambio() {
  const { data, loading, error, isStale } = useFetch<ExchangeResponse>('/api/cambio')

  if (loading) return <Loading />
  if (error && !data) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />
  const exchangeData = data.data

  const merged = mergeSeriesByDate(exchangeData)

  const series = [
    { key: 'ptax_compra_usd', name: 'PTAX Compra USD', color: '#4ecdc4', format: 'brl' as const, desc: 'Câmbio PTAX de compra do dólar — média ponderada das cotações.bankais na sessão de fechamento, used para operações de câmbio.' },
    { key: 'ptax_venda_usd', name: 'PTAX Venda USD', color: '#ff6b6b', format: 'brl' as const, desc: 'Câmbio PTAX de venda do dólar — preço de referência para conversões e derivativos cambiais.' },
    { key: 'eur_brl', name: 'EUR/BRL', color: '#ffa502', format: 'brl' as const, desc: 'Câmbio Euro/Real — cotação de referência do euro contra o real, relevante para importadores da Zona Euro.' },
  ]

  const last = (key: string) => {
    return lastValidSeriesPoint(exchangeData[key])
  }

  return (
    <div className="page">
      <DataQualityNotice dataset="cambio" isStale={isStale} />
      <div className="kpi-row">
        {series.map(s => {
          const l = last(s.key)
          return (
            <KpiCard
              key={s.key}
              name={s.name}
              value={l ? `R$ ${l.valor}` : ''}
              date={l?.data || ''}
              color={s.color}
              description={s.desc}
            />
          )
        })}
      </div>
      <TimeSeriesChart data={merged} series={series} title="Câmbio — Histórico" yLabel="R$" />
    </div>
  )
}

export default Cambio
