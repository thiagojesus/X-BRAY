import { useFetch } from '../hooks/useFetch'

type DataQuality = {
  readonly status?: 'ok' | 'partial' | 'unavailable'
  readonly last_updated?: string | null
  readonly metadata?: Readonly<Record<string, { readonly freshness?: 'ok' | 'stale' }>>
}

type DataQualityNoticeProps = {
  readonly dataset: string
  readonly isStale?: boolean
  readonly quality?: DataQuality
  readonly loading?: boolean
  readonly error?: string | null
}

function formatLastSuccess(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function DataQualityNotice({ dataset, isStale = false, quality, loading = false, error = null }: DataQualityNoticeProps) {
  const companion = useFetch<DataQuality>(quality === undefined && !loading && !error ? `/api/data-quality/${dataset}` : '')
  const current = quality ?? companion.data
  if (loading) {
    return <div className="data-quality-notice" role="status" aria-live="polite"><strong>Carregando dados...</strong></div>
  }
  if (error) {
    return <div className="data-quality-notice" role="alert"><strong>Dados indisponíveis.</strong> Erro ao carregar dados: {error}</div>
  }
  const partial = current?.status === 'partial'
  const stale = isStale || Object.values(current?.metadata ?? {}).some(item => item.freshness === 'stale')
  if (!partial && !stale) return null

  const updatedAt = formatLastSuccess(current?.last_updated)
  return (
    <div className="data-quality-notice" role="status" aria-live="polite">
      <strong>{partial ? 'Fonte parcialmente indisponível.' : 'Dados possivelmente desatualizados.'}</strong>
      {' '}Exibindo a última atualização disponível.
      {updatedAt && <> Última atualização: {updatedAt}.</>}
    </div>
  )
}
