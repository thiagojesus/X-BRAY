import { useId, useState } from 'react'

interface KpiCardProps {
  readonly name: string
  readonly value: string
  readonly date: string
  readonly color: string
  readonly description?: string
}

export function KpiCard({ name, value, date, color, description }: KpiCardProps) {
  const [showTooltip, setShowTooltip] = useState(false)
  const tooltipId = useId()

  return (
    <div className="kpi-card" style={{ borderTopColor: color }}>
      <span
        className="kpi-label kpi-label-help"
      >
        {name}
        {description && (
          <button
            type="button"
            className="kpi-info-icon"
            aria-label={`Informações sobre ${name}`}
            aria-describedby={showTooltip ? tooltipId : undefined}
            onMouseEnter={() => setShowTooltip(true)}
            onMouseLeave={() => setShowTooltip(false)}
            onFocus={() => setShowTooltip(true)}
            onBlur={() => setShowTooltip(false)}
          >
            i
          </button>
        )}
        {showTooltip && description && (
          <span id={tooltipId} role="tooltip" className="kpi-tooltip">{description}</span>
        )}
      </span>
      <span className="kpi-value">{value || '—'}</span>
      <span className="kpi-date">{date}</span>
    </div>
  )
}
