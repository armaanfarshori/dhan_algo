import { StatCard, Badge, Pill } from '@/components/ui'

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Format a fraction as a percentage string with 1 decimal: 0.132 → "13.2%" */
function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

/** Format the VRP spread in basis-pt style: fraction → signed "±N.N pts" */
function fmtSpread(v) {
  if (v == null || !Number.isFinite(v)) return '—'
  const pts = (v * 100).toFixed(1)
  return v >= 0 ? `+${pts} pts` : `${pts} pts`
}

/**
 * Map a gate decision string to a Badge variant.
 * SELL_PREMIUM → profit (green)
 * BUY_PREMIUM  → amber
 * STAND_ASIDE  → default (muted)
 */
function gateVariant(decision) {
  if (decision === 'SELL_PREMIUM') return 'profit'
  if (decision === 'BUY_PREMIUM')  return 'amber'
  return 'default'
}

// ─── FnoKpiRow ────────────────────────────────────────────────────────────────

/**
 * Top KPI strip for the F&O tab.
 *
 * Props:
 *   vrp — the /api/fno/vrp body or null:
 *   {
 *     available, realized_vol, implied_vol, vix, spread, vrp_ratio,
 *     gate_decision, k, dte, implied_move_pct, implied_source,
 *     realized_as_of, unit:"fraction"
 *   }
 *
 * All vol fields are fractions; ×100 for display.
 * If vrp is falsy, available===false, or a field is null, renders "—".
 */
export default function FnoKpiRow({ vrp }) {
  // Determine whether we have real data to display
  const hasData = !!(vrp && vrp.available !== false)

  // ── Card 1: Vol Regime / Gate ──────────────────────────────────────────────

  const gateDecision  = hasData ? (vrp.gate_decision ?? null) : null
  const gateK         = hasData ? vrp.k : null
  const gateAsOf      = hasData ? vrp.realized_as_of : null

  const gateValue = gateDecision
    ? <Badge variant={gateVariant(gateDecision)}>{gateDecision.replace(/_/g, ' ')}</Badge>
    : <span className="text-faint text-[20px]">—</span>

  const gateSub = hasData && (gateK != null || gateAsOf)
    ? [
        gateK    != null ? `k=${gateK}` : null,
        gateAsOf          ? `as of ${gateAsOf}` : null,
      ].filter(Boolean).join(' · ')
    : 'no F&O data yet'

  // ── Card 2: India VIX ─────────────────────────────────────────────────────

  const vixValue = hasData ? fmtPct(vrp.vix) : '—'

  // ── Card 3: NIFTY Realized 20d ────────────────────────────────────────────

  const realizedValue = hasData ? fmtPct(vrp.realized_vol) : '—'

  // ── Card 4: VRP Edge ──────────────────────────────────────────────────────

  const spread      = hasData ? vrp.spread : null
  const spreadValue = hasData ? fmtSpread(spread) : '—'

  const spreadIsPositive = spread != null && Number.isFinite(spread) && spread > 0
  const spreadIsNegative = spread != null && Number.isFinite(spread) && spread < 0

  const spreadValueClassName = spreadIsPositive
    ? 'text-profit'
    : spreadIsNegative
    ? 'text-loss'
    : undefined

  const spreadPillTone = spreadIsPositive ? 'up' : spreadIsNegative ? 'down' : 'neu'
  const spreadPillLabel = spreadIsPositive ? 'edge ↑' : spreadIsNegative ? 'edge ↓' : 'flat'

  const impliedSrc = hasData && vrp.implied_source ? vrp.implied_source : null
  const spreadSub  = hasData
    ? `implied − realized${impliedSrc ? ` · src ${impliedSrc}` : ''}`
    : 'no F&O data yet'

  // ─── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4 mb-3.5">

      {/* 1 — Vol Regime / Gate */}
      <StatCard
        label="Vol Regime / Gate"
        value={gateValue}
        sub={gateSub}
      />

      {/* 2 — India VIX */}
      <StatCard
        label="India VIX"
        value={vixValue}
        valueClassName={hasData ? 'text-foreground' : undefined}
        right={<Pill tone="neu">implied (30d)</Pill>}
        sub={hasData ? 'implied (30d)' : 'no F&O data yet'}
      />

      {/* 3 — NIFTY Realized 20d */}
      <StatCard
        label="NIFTY Realized 20d"
        value={realizedValue}
        valueClassName={hasData ? 'text-foreground' : undefined}
        right={<Pill tone="neu">20d c2c</Pill>}
        sub={hasData ? 'annualised c2c' : 'no F&O data yet'}
      />

      {/* 4 — VRP Edge */}
      <StatCard
        label="VRP Edge"
        value={spreadValue}
        valueClassName={spreadValueClassName}
        right={hasData ? <Pill tone={spreadPillTone}>{spreadPillLabel}</Pill> : null}
        sub={spreadSub}
      />

    </div>
  )
}
