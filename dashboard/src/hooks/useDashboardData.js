import { usePoller } from './usePoller'

export function useDashboardData() {
  const status    = usePoller('/api/status',    5000)
  const risk      = usePoller('/api/risk',      5000)
  const signals   = usePoller('/api/signals',   5000)
  const funds     = usePoller('/api/funds',     10000)
  const positions = usePoller('/api/positions', 5000)
  const scalper   = usePoller('/api/scalper',   5000)

  return { status, risk, signals, funds, positions, scalper }
}
