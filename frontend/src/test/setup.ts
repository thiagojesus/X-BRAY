import '@testing-library/jest-dom/vitest'
import { vi } from 'vitest'
import { createElement as h, Fragment } from 'react'

globalThis.fetch = vi.fn()

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as any

vi.mock('recharts', () => {
  const PassThrough = ({ children, ...props }: any) =>
    h('div', { 'data-testid': 'recharts-container', 'data-props': JSON.stringify(Object.keys(props).filter(k => k !== 'children' && k !== 'style' && k !== 'className')) }, children)

  return {
    ResponsiveContainer: ({ children }: any) =>
      h('div', { 'data-testid': 'responsive-container', style: { width: 800, height: 400 } }, children),
    LineChart: (props: any) => h(PassThrough, { ...props }),
    BarChart: (props: any) => h(PassThrough, { ...props }),
    Line: () => null,
    Bar: () => null,
    XAxis: () => null,
    YAxis: () => null,
    CartesianGrid: () => null,
    Tooltip: () => null,
    Legend: () => null,
  }
})
