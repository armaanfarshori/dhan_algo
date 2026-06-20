import { StatCard, Badge, Pill } from '@/components/ui'
import { INR0 } from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Signed ₹ with sign glyph, or "—" when null. */
function fmtPnl(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '−') + INR0(Math.abs(n))
}

/** Map a governor state to a Badge variant. */
function stateVariant(state) {
  switch (state) {
    case 'ACTIVE':     return 'profit'
    case 'LOCKED':     return 'sky'
    case 'TRAIL_ONLY': return 'amber'
    case 'COOLDOWN':   return 'amber'
    case 'STOOD_DOWN': return 'loss'
    default:           return 'default'
  }
}

// ─── ScalperKpiRow ─────────────────────────────────────────────────────────────

/**
 * Top KPI strip for the Scalper tab.
 *
 * Props:
 *   live     — /api/scalper/live body (or null). { available, trader_alive, state }
 *   governor — /api/scalper/governor body (or null). { available, state, totals, limits }
 *   trades   — /api/scalper/trades body (or null). { summary }
 *
 * Every field degrades to "—" when data is absent (scalper may be OFF).
 */
export default function ScalperKpiRow({ live, governor, trades }) {
  const liveOn   = !!(live && live.available)
  const govOn    = !!(governor && governor.available)
  const summary  = trades?.summary ?? null

  // ── Card 1: Scalper status ─────────────────────────────────────────────────
  const traderAlive = live?.trader_alive ?? false
  const scalperState = liveOn ? (live.state?.state ?? live.state?.direction ?? null) : null
  const statusValue = liveOn
    ? <Badge variant="profit">RUNNING</Badge>
    : <Badge variant="default">OFF</Badge>
  const statusSub = liveOn
    ? (traderAlive ? 'live feed' : 'heartbeat stale')
    : 'scalper not running'

  // ── Card 2: Governor state ─────────────────────────────────────────────────
  const govState = govOn ? (governor.state ?? null) : null
  const govValue = govState
    ? <Badge variant={stateVariant(govState)}>{govState.replace(/_/g, ' ')}</Badge>
    : <span className="text-faint text-[20px]">—</span>
  const govSub = govOn
    ? `${governor.totals?.trades_today ?? 0} trades · ${governor.totals?.consec_losses ?? 0} streak`
    : 'no governor data yet'

  // ── Card 3: Realized P&L (today) ───────────────────────────────────────────
  const realized = govOn ? governor.totals?.realized_pnl : null
  const realizedValue = govOn ? fmtPnl(realized) : '—'
  const realizedClass = realized != null && Number(realized) < 0
    ? 'text-loss'
    : realized != null && Number(realized) > 0
      ? 'text-profit'
      : undefined
  const floor = govOn ? governor.totals?.profit_floor : null
  const realizedSub = govOn
    ? (governor.totals?.lock_armed && floor != null
        ? <>floor <span className="text-profit">{fmtPnl(floor)}</span></>
        : 'today · realized')
    : 'no governor data yet'

  // ── Card 4: Trade log P&L (net) ────────────────────────────────────────────
  const logNet = summary?.net_pnl ?? null
  const logValue = summary ? fmtPnl(logNet) : '—'
  const logClass = logNet != null && Number(logNet) < 0
    ? 'text-loss'
    : logNet != null && Number(logNet) > 0
      ? 'text-profit'
      : undefined
  const closed = summary?.closed ?? 0
  const winRate = summary?.win_rate
  const logSub = summary
    ? `${closed} closed · ${closed > 0 && winRate != null && Number.isFinite(Number(winRate)) ? (Number(winRate) * 100).toFixed(0) + '% win' : '—'}`
    : 'no scalper trades yet'

  return (
    <div className="mb-3.5 grid grid-cols-2 gap-3.5 sm:grid-cols-4">
      <StatCard label="Scalper" value={statusValue} sub={statusSub}
        right={liveOn && scalperState ? <Pill tone="neu">{scalperState}</Pill> : null} />
      <StatCard label="Governor State" value={govValue} sub={govSub} />
      <StatCard label="Realized P&L (today)" value={realizedValue}
        valueClassName={realizedClass} sub={realizedSub} />
      <StatCard label="Trade Log (net)" value={logValue}
        valueClassName={logClass} sub={logSub}
        right={summary && closed > 0 && logNet != null && Number.isFinite(Number(logNet))
          ? <Pill tone={Number(logNet) >= 0 ? 'up' : 'down'}>{Number(logNet) >= 0 ? 'net ↑' : 'net ↓'}</Pill>
          : null} />
    </div>
  )
}
