export function Loading() {
  return (
    <div className="loading">
      <div className="spinner" />
      <p>Carregando dados...</p>
    </div>
  )
}

export function ErrorDisplay({ message }: { message: string }) {
  return (
    <div className="error-box">
      <p>Erro ao carregar dados: {message}</p>
    </div>
  )
}

export function EmptyState({ message = 'Sem dados disponíveis' }: { message?: string }) {
  return (
    <div className="empty-state">
      <p>{message}</p>
    </div>
  )
}
