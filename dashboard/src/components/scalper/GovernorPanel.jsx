import { Panel, PanelHeader, Progress, Badge } from '@/components/ui'
import { INR0 } from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

function fmtPnl(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  const n = Number(v)
  return (n >= 0 ? '+' : '−') + INR0(Math.abs(n))
}

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

/** A labelled progress strip: value / limit with a coloured bar. */
function MeterRow({ label, valueText, pct, color, sub }) {
  return (
    <div className="border-b border-border px-4 py-3 last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-muted-foreground">{label}</span>
        <span className="mono text-[12px] font-semibold text-foreground">{valueText}</span>
      </div>
      <Progress value={pct} color={color} className="mt-2" />
      {sub != null && (
        <div className="mono mt-1 text-[10px] text-faint">{sub}</div>
      )}
    </div>
  )
}

// ─── GovernorPanel ─────────────────────────────────────────────────────────────

/**
 * Daily Risk Governor — limits vs today's running totals, with progress strips.
 *
 * Prop: governor = /api/scalper/governor body or null:
 *   { available, state, limits:{ daily_loss_cap, profit_lock_arm, profit_lock_giveback,
 *     profit_lock_mode, max_trades, consec_loss_limit },
 *     totals:{ realized_pnl, trades_today, consec_losses, profit_floor,
 *     profit_hi_water, lock_armed } }
 *
 * Honest framing line below the strips: the governor reduces variance, it does NOT
 * guarantee a green day (per scalper_specs/04_daily_governor.md §0).
 */
export default function GovernorPanel({ governor }) {
  const available = !!(governor && governor.available)

  if (!available) {
    return (
      <Panel>
        <PanelHeader title="Daily Risk Governor" />
        <div className="mono px-4 py-6 text-[10px] text-faint">
          No governor data for today yet
        </div>
        <div className="mono border-t border-border px-4 py-3 text-[10px] leading-relaxed text-faint">
          The governor <span className="text-amber">reduces variance</span> and defends
          green days — it does <span className="text-amber">not</span> guarantee every day
          ends green.
        </div>
      </Panel>
    )
  }

  const limits = governor.limits ?? {}
  const totals = governor.totals ?? {}
  const state  = governor.state ?? null

  // ── Loss-cap meter (how far into the loss budget today's realized P&L is) ──
  const lossCap   = limits.daily_loss_cap != null ? Math.abs(Number(limits.daily_loss_cap)) : null
  const realized  = totals.realized_pnl != null ? Number(totals.realized_pnl) : null
  const lossUsed  = (lossCap && realized != null && realized < 0) ? Math.min(100, (Math.abs(realized) / lossCap) * 100) : 0
  const lossSub   = lossCap != null
    ? `realized ${fmtPnl(realized)} of −${INR0(lossCap)} cap`
    : 'no loss cap set'

  // ── Profit-lock meter (progress toward arming the lock) ───────────────────
  const armAt     = limits.profit_lock_arm != null ? Number(limits.profit_lock_arm) : null
  const armPct    = (armAt && realized != null && realized > 0) ? Math.min(100, (realized / armAt) * 100) : 0
  const armed     = !!totals.lock_armed
  const floor     = totals.profit_floor != null ? Number(totals.profit_floor) : null
  const hiWater   = totals.profit_hi_water != null ? Number(totals.profit_hi_water) : null
  const lockSub   = armAt != null
    ? (armed
        ? <>ARMED · floor <span className="text-profit">{fmtPnl(floor)}</span> · hi-water {fmtPnl(hiWater)}</>
        : `arms at +${INR0(armAt)} · mode ${limits.profit_lock_mode ?? '—'}`)
    : 'no profit-lock set'

  // ── Trades meter ──────────────────────────────────────────────────────────
  const maxTrades = limits.max_trades != null ? Number(limits.max_trades) : null
  const tradesDone = totals.trades_today != null ? Number(totals.trades_today) : 0
  const tradesPct = (maxTrades && maxTrades > 0) ? Math.min(100, (tradesDone / maxTrades) * 100) : 0
  const tradesSub = maxTrades != null ? `${tradesDone} of ${maxTrades} ladders` : 'no trade cap set'

  // ── Consecutive-loss meter ────────────────────────────────────────────────
  const consecLimit = limits.consec_loss_limit != null ? Number(limits.consec_loss_limit) : null
  const consec = totals.consec_losses != null ? Number(totals.consec_losses) : 0
  const consecPct = (consecLimit && consecLimit > 0) ? Math.min(100, (consec / consecLimit) * 100) : 0
  const consecSub = consecLimit != null ? `${consec} of ${consecLimit} before cooldown` : 'no streak limit set'

  return (
    <Panel>
      <PanelHeader
        title="Daily Risk Governor"
        meta={state ? <Badge variant={stateVariant(state)}>{state.replace(/_/g, ' ')}</Badge> : undefined}
      />

      <MeterRow
        label="Loss budget used"
        valueText={fmtPnl(realized)}
        pct={lossUsed}
        color="hsl(var(--loss))"
        sub={lossSub}
      />
      <MeterRow
        label="Profit-lock"
        valueText={armAt == null ? '—' : armed ? 'ARMED' : `${armPct.toFixed(0)}%`}
        pct={armed ? 100 : armPct}
        color={armed ? 'hsl(var(--profit))' : 'hsl(var(--sky))'}
        sub={lockSub}
      />
      <MeterRow
        label="Ladders today"
        valueText={`${tradesDone}${maxTrades != null ? ` / ${maxTrades}` : ''}`}
        pct={tradesPct}
        color="hsl(var(--sky))"
        sub={tradesSub}
      />
      <MeterRow
        label="Loss streak"
        valueText={`${consec}${consecLimit != null ? ` / ${consecLimit}` : ''}`}
        pct={consecPct}
        color="hsl(var(--amber))"
        sub={consecSub}
      />

      <div className="mono border-t border-border px-4 py-3 text-[10px] leading-relaxed text-faint">
        The governor <span className="text-amber">reduces variance</span> and defends green
        days — it does <span className="text-amber">not</span> guarantee every day ends green.
      </div>
    </Panel>
  )
}
