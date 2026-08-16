import { useState } from 'react'

interface KpiCardProps {
  name: string
  value: string
  date: string
  color: string
  description?: string
}

export function KpiCard({ name, value, date, color, description }: KpiCardProps) {
  const [showTooltip, setShowTooltip] = useState(false)

  return (
    <div className="kpi-card" style={{ borderTopColor: color }}>
      <span
        className="kpi-label kpi-label-help"
        onMouseEnter={() => setShowTooltip(true)}
        onMouseLeave={() => setShowTooltip(false)}
      >
        {name}
        {description && <span className="kpi-info-icon">i</span>}
        {showTooltip && description && (
          <span className="kpi-tooltip">{description}</span>
        )}
      </span>
      <span className="kpi-value">{value || '—'}</span>
      <span className="kpi-date">{date}</span>
    </div>
  )
}
