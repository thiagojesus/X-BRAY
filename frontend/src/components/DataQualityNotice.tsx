import { useFetch } from '../hooks/useFetch'

type DataQuality = {
  readonly status?: 'ok' | 'partial' | 'unavailable'
  readonly last_updated?: string | null
  readonly metadata?: Readonly<Record<string, { readonly freshness?: 'ok' | 'stale' }>>
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

export function DataQualityNotice({ dataset, isStale = false }: { dataset: string; isStale?: boolean }) {
  const quality = useFetch<DataQuality>(`/api/data-quality/${dataset}`)
  const partial = quality.data?.status === 'partial'
  const stale = isStale || Object.values(quality.data?.metadata ?? {}).some(item => item.freshness === 'stale')
  if (!partial && !stale) return null

  const updatedAt = formatLastSuccess(quality.data?.last_updated)
  return (
    <div className="data-quality-notice" role="status" aria-live="polite">
      <strong>{partial ? 'Fonte parcialmente indisponível.' : 'Fonte indisponível.'}</strong>
      {' '}Exibindo a última atualização disponível.
      {updatedAt && <> Última atualização: {updatedAt}.</>}
    </div>
  )
}
