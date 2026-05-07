import { usePoller } from './usePoller'

export function useDashboardData() {
  // Fast (1s) — live trading data
  const status       = usePoller('/api/status',          1000)
  const risk         = usePoller('/api/risk',            1000)
  const signals      = usePoller('/api/signals',         1000)
  const scalper      = usePoller('/api/scalper',         1000)
  const fnoScanner   = usePoller('/api/scanner/fno',     1000)
  const equityScanner= usePoller('/api/scanner/equity',  1000)
  const scanner      = usePoller('/api/scanner',         1000)

  // Medium (5s) — position data
  const positions    = usePoller('/api/positions',       5000)
  const payoff       = usePoller('/api/payoff',          5000)

  // Slow (10-30s) — account / market data
  const funds        = usePoller('/api/funds',           10000)
  const config       = usePoller('/api/config',          10000)
  const watchlist    = usePoller('/api/watchlist',       15000)
  const market       = usePoller('/api/market',          30000)

  return { status, risk, signals, funds, positions, scalper, payoff, config, watchlist, scanner, fnoScanner, equityScanner, market }
}
