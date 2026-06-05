import { usePoller } from './usePoller'

export function useDashboardData() {
  // ── Fast (1s) — live trading data ─────────────────────────────────────────
  const status        = usePoller('/api/status',           1000)
  const risk          = usePoller('/api/risk',             1000)
  const signals       = usePoller('/api/signals',          1000)
  const scalper       = usePoller('/api/scalper',          1000)
  const fnoScanner    = usePoller('/api/scanner/fno',      1000)
  const equityScanner = usePoller('/api/scanner/equity',   1000)
  const scanner       = usePoller('/api/scanner',          1000)
  const logs          = usePoller('/api/logs?limit=60',    1000)

  // ── Medium (5s) — position + backfill progress ────────────────────────────
  const positions      = usePoller('/api/positions',        5000)
  const paperPositions = usePoller('/api/paper/positions',  1000)
  const payoff         = usePoller('/api/payoff',           5000)
  const tradelog       = usePoller('/api/trades?limit=500', 5000)
  const backfill       = usePoller('/api/backfill/status',  5000)

  // ── Slow (15-60s) — account / DB / AI data ────────────────────────────────
  const funds         = usePoller('/api/funds',            10000)
  const config        = usePoller('/api/config',           10000)
  const watchlist     = usePoller('/api/watchlist',        15000)
  const market        = usePoller('/api/market',           30000)
  const dbStats       = usePoller('/api/db/stats',         60000)   // cached 60s server-side
  const kronosSignals = usePoller('/api/kronos/signals',   30000)
  const kronosLive    = usePoller('/api/kronos/live',      10000)   // live scanner state
  const screener      = usePoller('/api/kronos/screener',  120000)  // ATR screener is expensive
  const hermes        = usePoller('/api/hermes/status',    60000)   // cached 30s server-side

  return {
    // existing
    status, risk, signals, funds, positions, paperPositions,
    scalper, payoff, config, watchlist, scanner,
    fnoScanner, equityScanner, market, tradelog, logs,
    // new
    dbStats, kronosSignals, kronosLive, screener, backfill, hermes,
  }
}
