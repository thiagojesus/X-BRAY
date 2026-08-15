import { useFetch } from '../hooks/useFetch'
import { Loading, ErrorDisplay } from '../components/Status'

function Titulos() {
  const { data, loading, error } = useFetch<any>('/api/titulos')

  if (loading) return <Loading />
  if (error) return <ErrorDisplay message={error} />
  if (!data?.data) return <ErrorDisplay message="Sem dados" />

  const sheets = Object.keys(data.data).filter(k => k !== 'error')

  return (
    <div className="page">
      <div className="info-box">
        <p><strong>Fonte:</strong> ANBIMA — Índices de Mercado (IMA) Histórico (XLS)</p>
        <p>Os dados são extraídos diretamente da tabela histórica da ANBIMA.</p>
      </div>
      {sheets.length === 0 ? (
        <ErrorDisplay message="Nenhuma aba encontrada no arquivo" />
      ) : (
        <div className="tables-grid">
          {sheets.map(sheetName => {
            const rows = data.data[sheetName]
            if (!Array.isArray(rows) || rows.length === 0) return null
            const cols = Object.keys(rows[0])
            return (
              <div key={sheetName} className="table-card">
                <h3 className="table-title">{sheetName.replace(/_/g, ' ').toUpperCase()}</h3>
                <div className="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        {cols.map(c => <th key={c}>{c}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.slice(0, 50).map((row: any, i: number) => (
                        <tr key={i}>
                          {cols.map(c => <td key={c}>{row[c] != null ? String(row[c]) : ''}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {rows.length > 50 && <p className="table-note">Mostrando 50 de {rows.length} registros</p>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default Titulos
