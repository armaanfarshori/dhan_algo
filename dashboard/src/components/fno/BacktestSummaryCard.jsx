import { Panel, PanelHeader, Badge } from '@/components/ui'
import { INR0 } from '@/tokens'

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Format a decimal win-rate (0–1) as a percentage string, e.g. 0.63 → "63.0%" */
function fmtWinRate(v) {
  if (v == null) return '—'
  return `${(Number(v) * 100).toFixed(1)}%`
}

/** Format profit factor to 2 dp, or "—" if null/undefined. */
function fmtPF(v) {
  if (v == null) return '—'
  return Number(v).toFixed(2)
}

// ─── StatRow ─────────────────────────────────────────────────────────────────
// Matches the StatRow pattern from SystemTab: label left, mono value right,
// thin bottom border, last:border-b-0.

function StatRow({ label, children }) {
  return (
    <div className="flex items-center justify-between border-b border-border px-4 py-[9px] last:border-b-0">
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span className="mono text-[12px] text-foreground">{children}</span>
    </div>
  )
}

// ─── CaveatBlock ─────────────────────────────────────────────────────────────
// Amber-tinted block at the TOP of the card body (above any numbers).
// Matches the amber variant from badge.jsx: border-amber/30 bg-amber/10 text-amber.

function CaveatBlock({ caveats }) {
  const items =
    Array.isArray(caveats) && caveats.length > 0
      ? caveats
      : ['PRELIMINARY — backtest only, not a validated live edge.']

  return (
    <div className="border-b border-border bg-amber/10 px-4 py-3">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[.09em] text-amber">
        Caveats
      </div>
      <ul className="m-0 list-none space-y-[3px] p-0">
        {items.map((c, i) => (
          <li key={i} className="mono flex gap-1.5 text-[10px] text-amber">
            <span className="mt-[1px] shrink-0 select-none opacity-60">▸</span>
            <span>{c}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ─── BacktestSummaryCard ──────────────────────────────────────────────────────

/**
 * Presentational card summarising the condor backtest result.
 *
 * Prop contract:
 *   backtest: {
 *     available:     boolean,
 *     as_of:         string | null,    // ISO date or human label
 *     n_trades:      number | null,
 *     win_rate:      number | null,    // 0–1 decimal
 *     profit_factor: number | null,
 *     net_pnl:       number | null,    // ₹
 *     move_mult:     number | null,    // VIX-move multiplier used
 *     period:        string | null,    // e.g. "2024-01 – 2025-12"
 *     vrp_note:      string | null,    // faint sub-line (VRP context)
 *     caveats:       string[],         // honesty bullet list
 *     go:            boolean | null,
 *     go_reason:     string | null,
 *   } | null
 *
 * Honesty guarantee: caveat block always renders above the numbers.
 * Verdict badge is ALWAYS amber — never profit-green — to avoid implying
 * a proven live edge.
 */
export default function BacktestSummaryCard({ backtest }) {
  // ── Unavailable / null state ───────────────────────────────────────────────
  const unavailable = !backtest || backtest.available === false

  if (unavailable) {
    return (
      <Panel>
        <PanelHeader title="Condor Backtest (preliminary)" />
        <div className="mono px-4 py-5 text-[10px] text-faint">
          Backtest summary unavailable
        </div>
      </Panel>
    )
  }

  // ── Destructure with safe fallbacks ───────────────────────────────────────
  const {
    as_of         = null,
    n_trades      = null,
    win_rate      = null,
    profit_factor = null,
    net_pnl       = null,
    move_mult     = null,
    period        = null,
    vrp_note      = null,
    caveats       = [],
    go            = null,
    go_reason     = null,
  } = backtest ?? {}

  // ── Verdict label ──────────────────────────────────────────────────────────
  // Never use profit-green: verdict is always amber to signal "preliminary only"
  const verdictText =
    go === true  ? 'GO (preliminary)' :
    go === false ? 'NO-GO'            :
                   'PENDING'

  // ── Net P&L display ───────────────────────────────────────────────────────
  const pnlDisplay = net_pnl != null
    ? (net_pnl >= 0
        ? `+${INR0(net_pnl)}`
        : `−${INR0(Math.abs(net_pnl))}`)
    : '—'

  // Neutral on a positive (this card is explicitly PRELIMINARY — green would
  // imply a proven edge); still flag a negative in loss-red.
  const pnlClass = net_pnl != null && net_pnl < 0 ? 'text-loss' : 'text-foreground'

  return (
    <Panel>
      <PanelHeader
        title="Condor Backtest (preliminary)"
        meta={as_of ? <span className="mono">{as_of}</span> : undefined}
      />

      {/* ── HONESTY FIRST: caveat block above numbers (spec requirement) ── */}
      <CaveatBlock caveats={caveats} />

      {/* ── Stats ─────────────────────────────────────────────────────────── */}
      <StatRow label="Period">
        {period ?? '—'}
      </StatRow>

      <StatRow label="Trades">
        {n_trades != null ? n_trades.toLocaleString('en-IN') : '—'}
      </StatRow>

      <StatRow label="Win rate">
        {fmtWinRate(win_rate)}
      </StatRow>

      <StatRow label="Profit factor">
        {fmtPF(profit_factor)}
      </StatRow>

      <StatRow label="Net P&L">
        <span className={pnlClass}>{pnlDisplay}</span>
      </StatRow>

      <StatRow label="Move mult">
        {move_mult != null ? `${Number(move_mult).toFixed(2)}×` : '—'}
      </StatRow>

      {/* VRP note: faint sub-line below the last stat row, no border-b needed */}
      {vrp_note && (
        <div className="mono border-t border-border px-4 pb-3 pt-2.5 text-[10px] text-faint">
          {vrp_note}
        </div>
      )}

      {/* ── Verdict ───────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2.5 border-t border-border px-4 py-3">
        {/* Always amber — never profit-green — per spec */}
        <Badge variant="amber">{verdictText}</Badge>
        {go_reason && (
          <span className="mono text-[10px] text-faint">{go_reason}</span>
        )}
      </div>
    </Panel>
  )
}
