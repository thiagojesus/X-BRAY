import { useMemo, useState } from 'react'
import { geoIdentity, geoPath } from 'd3-geo'
import brazilStates from '../data/brazil-states.json'

export interface UFData {
  uf: string
  candidates: { name: string; price: number; volume: number }[]
  history: Record<string, Record<string, number>>
}

export interface StatesPayload {
  source: string
  updated_at: string
  days: string[]
  ufs: UFData[]
}

const FALLBACK_COLORS = [
  '#ff6b6b', '#ffa502', '#4ecdc4', '#a29bfe', '#fd79a8', '#74b9ff',
  '#55efc4', '#fdcb6e', '#e17055', '#00cec9', '#dfe6e9', '#b2bec3',
]

const ufByName: Record<string, string> = {}
for (const f of (brazilStates as any).features) {
  ufByName[f.properties.sigla] = f.properties.name
}

function toColor(seed: string, i: number): string {
  return FALLBACK_COLORS[i % FALLBACK_COLORS.length]
}

export function buildCandidateColors(ufs: UFData[]): Map<string, string> {
  const byVolume = new Map<string, number>()
  for (const s of ufs) {
    for (const c of s.candidates) {
      byVolume.set(c.name, (byVolume.get(c.name) ?? 0) + c.volume)
    }
  }
  const sorted = Array.from(byVolume.entries()).sort((a, b) => b[1] - a[1])
  return new Map(sorted.map(([name, _], i) => [name, toColor(name, i)]))
}

export interface UFColor {
  uf: string
  name: string
  color: string | null
  opacity: number
  leader: string | null
  leaderPct: number
  runnerUpPct: number
  margin: number
}

export function computeUFsForDay(ufs: UFData[], day: string, colors: Map<string, string>): UFColor[] {
  return ufs.map(s => {
    const h = s.history[day]
    if (!h || Object.keys(h).length === 0) {
      return { uf: s.uf, name: ufByName[s.uf] ?? s.uf, color: null, opacity: 0.12, leader: null, leaderPct: 0, runnerUpPct: 0, margin: 0 }
    }
    const entries = Object.entries(h).sort((a, b) => b[1] - a[1])
    const [leader, leaderPct] = entries[0]
    const runnerUpPct = entries.length > 1 ? entries[1][1] : 0
    const margin = Math.max(0, leaderPct - runnerUpPct)
    return {
      uf: s.uf,
      name: ufByName[s.uf] ?? s.uf,
      color: colors.get(leader) ?? '#999',
      opacity: 0.25 + Math.min(0.75, margin / 60) * 0.75,
      leader,
      leaderPct,
      runnerUpPct,
      margin,
    }
  })
}

export function formatDay(pt: string): string {
  const [y, m, d] = pt.split('-')
  return `${d}/${m}/${y}`
}

interface BrasilMapProps {
  payload: StatesPayload
}

function BrasilMap({ payload }: BrasilMapProps) {
  const [dayIdx, setDayIdx] = useState(() => Math.max(0, payload.days.length - 1))
  const [hoverUf, setHoverUf] = useState<string | null>(null)

  const day = payload.days[dayIdx] ?? payload.days[payload.days.length - 1]

  const { projection, path } = useMemo(() => {
    const proj = geoIdentity().reflectY(true).fitExtent(
      [[4, 4], [756, 660]],
      { type: 'FeatureCollection', features: (brazilStates as any).features } as any
    )
    return { projection: proj, path: geoPath(proj) }
  }, [])

  const colors = useMemo(() => buildCandidateColors(payload.ufs), [payload.ufs])
  const ufColors = useMemo(
    () => computeUFsForDay(payload.ufs, day, colors),
    [payload.ufs, day, colors]
  )
  const colorByUf = useMemo(() => new Map(ufColors.map(u => [u.uf, u])), [ufColors])

  const leaderNames = useMemo(() => {
    const names = new Set(ufColors.filter(u => u.leader).map(u => u.leader as string))
    return Array.from(colors.keys()).filter(name => names.has(name))
  }, [ufColors, colors])
  const shownUFs = ufColors.filter(u => u.leader)

  return (
    <div className="mapa-brasil">
      <div className="mapa-header">
        <h3 className="chart-title">Mapa — 1º lugar no 1º turno por estado</h3>
        <div className="mapa-date">
          <span>{day ? formatDay(day) : '—'}</span>
          <input
            type="range"
            className="date-slider"
            min={0}
            max={payload.days.length - 1}
            value={dayIdx}
            onChange={e => setDayIdx(Number(e.target.value))}
          />
        </div>
      </div>

      <div className="mapa-body">
        <svg viewBox="0 0 760 668" className="mapa-svg" role="img" aria-label="Mapa do Brasil por estado">
          {(brazilStates as any).features.map((f: any) => {
            const sigla = f.properties.sigla as string
            const info = colorByUf.get(sigla)
            return (
              <path
                key={sigla}
                d={path(f) ?? undefined}
                className="uf-path"
                fill={info?.color ?? '#2a2a2a'}
                fillOpacity={info?.opacity ?? 0.12}
                stroke="#111"
                strokeWidth={0.6}
                data-uf={sigla}
                onMouseEnter={() => setHoverUf(sigla)}
                onMouseLeave={() => setHoverUf(null)}
              />
            )
          })}
          {hoverUf && colorByUf.get(hoverUf)?.leader && (
            <g transform="translate(12, 12)">
              <rect width={210} height={78} rx={6} fill="#1e1e1e" stroke="#444" />
              <text x={10} y={20} fill="#eee" fontSize={13} fontWeight={600}>
                {colorByUf.get(hoverUf)?.name}
              </text>
              <text x={10} y={38} fill={colorByUf.get(hoverUf)?.color ?? '#999'} fontSize={12}>
                {colorByUf.get(hoverUf)?.leader} — {colorByUf.get(hoverUf)?.leaderPct.toFixed(1)}%
              </text>
              <text x={10} y={55} fill="#aaa" fontSize={11}>
                2º: {colorByUf.get(hoverUf)?.runnerUpPct.toFixed(1)}%
              </text>
              <text x={10} y={70} fill="#aaa" fontSize={11}>
                Margem: {colorByUf.get(hoverUf)?.margin.toFixed(1)} p.p.
              </text>
            </g>
          )}
        </svg>

        <div className="mapa-legend">
          <div className="mapa-legend-title">Candidato líder</div>
          <div className="mapa-legend-grid">
            {leaderNames.map((name, i) => (
              <div key={name} className="mapa-legend-item">
                <span className="mapa-legend-dot" style={{ background: colors.get(name) }} />
                <span>{name}</span>
              </div>
            ))}
          </div>
          <div className="mapa-legend-margin">
            <span className="mapa-margin-hint">Opacidade = margem sobre o 2º colocado</span>
            <div className="mapa-margin-bar">
              <span style={{ background: '#fff', opacity: 0.3 }} />
              <span style={{ background: '#fff', opacity: 1 }} />
            </div>
          </div>
          {shownUFs.length === 0 && <div className="mapa-empty">Sem dados para este dia</div>}
        </div>
      </div>
    </div>
  )
}

export default BrasilMap