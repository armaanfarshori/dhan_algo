import { Panel, PanelHeader, Badge } from '@/components/ui'
import { INR0 } from '@/tokens'

// ─── The verdict this card must tell ─────────────────────────────────────────
// The June-2026 condor backtest (+₹36,400, 90.9% win, "GO preliminary") used
// India VIX as a stand-in for weekly option IV. The follow-up study re-ran the
// clean 2×2 (vol gate × IV source) on REAL Dhan option IV and the over-credit
// vanished: both condor variants flipped GO → NO-GO. Only a thin far-OTM
// wide-wing corner barely survived — not a business.
//
// Source of truth: CLAUDE.md "RESEARCH CONCLUSION (2026-06-21)" + memory
// `real-iv-condor-verdict`. If that record ever changes, change it here too —
// this card must never render the falsified proxy run as the headline result.

const REAL_IV_FLIP = [
  { label: 'condor v1', proxy: '+16.1%', real: '−3.3%' },
  { label: 'condor v2', proxy: '+13.2%', real: '−1.5%' },
]

const REAL_IV_NOTE =
  'Return on margin, vol-gated. The proxy column is the falsified run; the ' +
  'real column re-prices the identical strategy on Dhan rollingoption IV.'

const FALSIFICATION =
  'VIX (~30d) as a weekly (~4–7 DTE) IV proxy over-credited every short leg. ' +
  'On real option IV the edge is gone.'

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

// ─── Section label ───────────────────────────────────────────────────────────

function SectionLabel({ children }) {
  // Static class string — Tailwind scans source text, so no interpolation here.
  return (
    <div className="border-b border-border px-4 pb-1.5 pt-3 text-[10px] font-semibold uppercase tracking-[.09em] text-faint">
      {children}
    </div>
  )
}

// ─── Verdict block ───────────────────────────────────────────────────────────
// Loss-tinted, at the very top: the real-IV verdict is the headline, not the
// proxy P&L that sits further down the card.

function VerdictBlock() {
  return (
    <div className="border-b border-border bg-loss/[.08] px-4 py-3">
      <div className="mb-1.5 flex flex-wrap items-center gap-2.5">
        <Badge variant="loss">NO-GO — real IV</Badge>
        <span className="mono text-[10px] text-faint">falsified 2026-06-21</span>
      </div>
      <div className="text-[11px] leading-relaxed text-loss">{FALSIFICATION}</div>
    </div>
  )
}

// ─── Proxy-vs-real flip table ────────────────────────────────────────────────

function FlipTable() {
  return (
    <>
      <div
        className="grid items-center gap-2 border-b border-border px-4 py-[7px] text-[9.5px] font-semibold uppercase tracking-[.07em] text-faint"
        style={{ gridTemplateColumns: 'minmax(0,1fr) 72px 72px' }}
      >
        <span>return on margin</span>
        <span className="text-right">VIX proxy</span>
        <span className="text-right">real IV</span>
      </div>
      {REAL_IV_FLIP.map(({ label, proxy, real }) => (
        <div
          key={label}
          className="grid items-center gap-2 border-b border-border px-4 py-[9px]"
          style={{ gridTemplateColumns: 'minmax(0,1fr) 72px 72px' }}
        >
          <span className="truncate text-[12px] text-muted-foreground">{label}</span>
          {/* The proxy column is a falsified number — never paint it green. */}
          <span className="mono text-right text-[12px] text-faint line-through">{proxy}</span>
          <span className="mono text-right text-[12px] text-loss">{real}</span>
        </div>
      ))}
      <div className="mono border-b border-border px-4 pb-2.5 pt-2 text-[10px] leading-relaxed text-faint">
        {REAL_IV_NOTE}
      </div>
    </>
  )
}

// ─── CaveatBlock ─────────────────────────────────────────────────────────────
// Amber-tinted block above the (falsified) proxy numbers.
// Matches the amber variant from badge.jsx: border-amber/30 bg-amber/10 text-amber.

function CaveatBlock({ caveats }) {
  const items =
    Array.isArray(caveats) && caveats.length > 0
      ? caveats
      : ['Backtest only — superseded by the real-IV rerun above.']

  return (
    <div className="border-b border-border bg-amber/10 px-4 py-3">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[.09em] text-amber">
        Caveats (as written for the proxy run)
      </div>
      {/* Scroll-capped: the card already leads with the verdict, and the full
          caveat list would otherwise stretch the whole equal-height row. */}
      <ul className="m-0 max-h-[132px] list-none space-y-[3px] overflow-auto p-0">
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
 * Presentational card for the condor backtest record.
 *
 * Prop contract (GET /api/fno/backtest — the June-2026 VIX-proxy run):
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
 *     go:            boolean | null,   // the run's OWN verdict — SUPERSEDED
 *     go_reason:     string | null,
 *   } | null
 *
 * Honesty guarantee: the real-IV NO-GO verdict is the headline and renders even
 * when the payload is missing; the payload's own `go` flag is displayed only as
 * a superseded footnote, never as the card's verdict badge.
 */
export default function BacktestSummaryCard({ backtest }) {
  const unavailable = !backtest || backtest.available === false

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

  // ── Net P&L display ───────────────────────────────────────────────────────
  const pnlDisplay = net_pnl != null
    ? (net_pnl >= 0
        ? `+${INR0(net_pnl)}`
        : `−${INR0(Math.abs(net_pnl))}`)
    : '—'

  return (
    <Panel>
      <PanelHeader
        title="Condor Backtest — falsified"
        meta={as_of ? <span className="mono">proxy run {as_of}</span> : undefined}
      />

      {/* ── HEADLINE: the real-IV verdict, above every number ── */}
      <VerdictBlock />
      <FlipTable />

      {unavailable ? (
        <div className="mono px-4 py-4 text-[10px] text-faint">
          Proxy-run summary unavailable — the real-IV verdict above stands regardless.
        </div>
      ) : (
        <>
          <CaveatBlock caveats={caveats} />

          <SectionLabel>Falsified proxy run (VIX-as-weekly-IV)</SectionLabel>

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
            {/* Faint + struck through: a number that did not survive re-pricing. */}
            <span className="text-faint line-through">{pnlDisplay}</span>
          </StatRow>

          <StatRow label="Move mult">
            {move_mult != null ? `${Number(move_mult).toFixed(2)}×` : '—'}
          </StatRow>

          {vrp_note && (
            <div className="mono border-t border-border px-4 pb-3 pt-2.5 text-[10px] text-faint">
              {vrp_note}
            </div>
          )}

          {/* ── The proxy run's own verdict — kept, clearly superseded ── */}
          <div className="flex flex-wrap items-center gap-2.5 border-t border-border px-4 py-3">
            <Badge variant="default">
              superseded: {go === true ? 'GO (preliminary)' : go === false ? 'NO-GO' : 'PENDING'}
            </Badge>
            {go_reason && (
              <span className="mono text-[10px] text-faint line-through">{go_reason}</span>
            )}
          </div>
        </>
      )}
    </Panel>
  )
}
