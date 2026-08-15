export interface SgsPoint {
  data: string
  valor: string
}

export function parseSgsData(raw: SgsPoint[]): { date: string; value: number }[] {
  return raw
    .map(p => ({
      date: p.data,
      value: parseFloat(p.valor.replace(',', '.')),
    }))
    .filter(p => !isNaN(p.value))
    .sort((a, b) => {
      const [da, ma, ya] = a.date.split('/').map(Number)
      const [db, mb, yb] = b.date.split('/').map(Number)
      return ya * 10000 + ma * 100 + da - (yb * 10000 + mb * 100 + db)
    })
}

export function formatDate(dateStr: string): string {
  const [d, m, y] = dateStr.split('/')
  return `${y}-${m.padStart(2, '0')}-${d.padStart(2, '0')}`
}

export function shortDate(dateStr: string): string {
  const [d, m, y] = dateStr.split('/')
  return `${m}/${y.slice(2)}`
}
