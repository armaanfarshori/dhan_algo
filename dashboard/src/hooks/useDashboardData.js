import { usePoller } from './usePoller'

export function useDashboardData() {
  // ── Fast loop: ONE snapshot every 2s (heartbeat file read, no DB) ──────────
  // Replaces the old 1s pollers for status / risk / paper positions.
  const snapshot = usePoller('/api/snapshot', 2000)
  const trader   = snapshot.data?.trader ?? null
  const alive    = snapshot.data?.alive ?? false
  const limits   = snapshot.data?.limits ?? {}

  // ── Medium ─────────────────────────────────────────────────────────────────
  const signals  = usePoller('/api/signals',          5000)
  const logs     = usePoller('/api/logs?limit=60',    3000)
  const tradelog = usePoller('/api/trades?limit=500', 10000)
  const equity   = usePoller('/api/equity',           20000)
  const backfill = usePoller('/api/backfill/status',  10000)
  const positions = usePoller('/api/positions',       10000)

  // ── Slow ───────────────────────────────────────────────────────────────────
  const gate          = usePoller('/api/kronos/gate',    30000)
  const funds         = usePoller('/api/funds',          15000)
  const watchlist     = usePoller('/api/watchlist',      30000)
  const market        = usePoller('/api/market',         30000)
  const dbStats       = usePoller('/api/db/stats',       60000)
  const kronosSignals = usePoller('/api/kronos/signals', 30000)
  const kronosLive    = usePoller('/api/kronos/live',    15000)
  const screener      = usePoller('/api/kronos/screener', 300000)  // server caches 5 min
  const systemHealth  = usePoller('/api/system/health',  30000)
  const rateLimitsData = usePoller('/api/rate-limits',   12000)    // account-wide across all processes

  // ── Legacy-shape adapters (older panels expect /api/status & /api/risk) ───
  const first = trader?.strategies?.[0]
  const status = {
    loading: snapshot.loading, error: snapshot.error,
    data: trader ? {
      mode: trader.mode,
      paper_trading: trader.mode !== 'LIVE',
      trader_alive: alive,
      uptime_seconds: trader.uptime_seconds,
      kronos_gate: trader.kronos_gate,
      strategy_name: first ? `ORB ${first.ticker ?? first.security_id}` : 'none',
      strategy_running: alive && !!first?.running,
    } : null,
  }
  const risk = {
    loading: snapshot.loading, error: snapshot.error,
    data: trader?.risk ?? null,
  }
  const paperPositions = {
    loading: snapshot.loading, error: snapshot.error,
    data: {
      ok: true,
      data: (trader?.portfolio?.open_positions ?? []).map(p => ({
        security_id: p.security_id, ticker: p.ticker, qty: p.qty,
        entry_price: p.avg_price, strategy: p.strategy, in_position: true,
      })),
    },
  }

  return {
    snapshot, trader, alive, limits, gate,
    status, risk, paperPositions,
    signals, funds, positions, watchlist, market, tradelog, logs, equity,
    dbStats, kronosSignals, kronosLive, screener, backfill, systemHealth, rateLimitsData,
  }
}
