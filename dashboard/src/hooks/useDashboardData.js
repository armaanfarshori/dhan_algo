import { usePoller } from './usePoller'

export function useDashboardData() {
  const status    = usePoller('/api/status',           5000)
  const risk      = usePoller('/api/risk',             5000)
  const signals   = usePoller('/api/signals',          5000)
  const funds     = usePoller('/api/funds',            10000)
  const positions = usePoller('/api/positions',        5000)
  const scalper   = usePoller('/api/scalper',          5000)
  const payoff    = usePoller('/api/payoff',           5000)
  const config    = usePoller('/api/config',           10000)
  const watchlist = usePoller('/api/watchlist',        15000)
  const scanner   = usePoller('/api/scanner',          5000)
  const market    = usePoller('/api/market',           30000)
  return { status, risk, signals, funds, positions, scalper, payoff, config, watchlist, scanner, market }
}
