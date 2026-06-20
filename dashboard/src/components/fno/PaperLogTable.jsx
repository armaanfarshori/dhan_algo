/**
 * PaperLogTable — iron-condor paper-trade log for the F&O dashboard.
 *
 * Props:
 *   paper — /api/fno/paper response body, or null:
 *     {
 *       summary: { resolved, open, net_pnl, win_rate, wins, avg_pnl },
 *       equity_curve: [{ date, cum_pnl }],
 *       trades: [{
 *         id, status, entry_date, expiry_date, gate_decision,
 *         straddle_iv, realized_vol_20d,
 *         short_put_k, short_call_k, long_put_k, long_call_k,
 *         credit, max_loss, net_pnl, win
 *       }]
 *     }
 */

import { Panel, PanelHeader } from '@/components/ui'
import { Tag, Pill }          from '@/components/ui'
import { INR0 }               from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Format a currency value with sign, or "—" if null. */
function fmtPnl(v) {
  if (v == null) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return (n >= 0 ? '+' : '−') + INR0(Math.abs(n))
}

/** "2026-06-20" → "20 Jun" */
function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

/** Compact strike display: "shortPut/longPut — shortCall/longCall" */
function fmtStrikes(t) {
  const sp = t?.short_put_k  ?? '?'
  const lp = t?.long_put_k   ?? '?'
  const sc = t?.short_call_k ?? '?'
  const lc = t?.long_call_k  ?? '?'
  return `${sp}/${lp} — ${sc}/${lc}`
}

/** Tag tone for a trade row. */
function rowTagTone(t) {
  if (t?.status !== 'RESOLVED') return 'default'
  return t?.win ? 'long' : 'short'
}

/** Tag label for a trade row. */
function rowTagLabel(t) {
  if (t?.status !== 'RESOLVED') return 'OPEN'
  return t?.win ? 'WIN' : 'LOSS'
}

// ─── Aggregate strip ─────────────────────────────────────────────────────────

// Panel-native stat cell (no nested Card chrome — this strip lives inside a Panel)
function Cell({ label, children, sub }) {
  return (
    <div className="min-w-0">
      <div className="mono text-[9.5px] font-semibold uppercase tracking-[.08em] text-faint">{label}</div>
      <div className="mono mt-1 text-[15px] font-semibold text-foreground">{children}</div>
      {sub != null && <div className="mono mt-0.5 truncate text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

function AggStrip({ summary }) {
  const resolved = summary?.resolved ?? 0
  const open     = summary?.open     ?? 0
  const net_pnl  = summary?.net_pnl  ?? null
  const win_rate = summary?.win_rate ?? null

  // Win-rate shows "—" if no resolved trades
  const winRateDisplay = resolved === 0
    ? '—'
    : win_rate != null
      ? (Number(win_rate) * 100).toFixed(0) + '%'
      : '—'

  const wins = summary?.wins ?? 0

  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-border px-4 py-4 sm:grid-cols-4">
      <Cell label="Total Net P&L" sub="realised on resolved">
        {net_pnl != null
          ? <span className={Number(net_pnl) >= 0 ? 'text-profit' : 'text-loss'}>{fmtPnl(net_pnl)}</span>
          : '—'}
      </Cell>

      <Cell label="Win Rate" sub={`of ${resolved} resolved`}>
        {winRateDisplay}
        {resolved > 0 && (
          <span className="ml-1.5 align-middle">
            <Pill tone={win_rate != null && Number(win_rate) >= 0.5 ? 'up' : 'down'}>
              {wins}W / {resolved - wins}L
            </Pill>
          </span>
        )}
      </Cell>

      <Cell label="# Open" sub="live condors">{open}</Cell>

      <Cell
        label="# Resolved"
        sub={summary?.avg_pnl != null
          ? <>avg <span className={Number(summary.avg_pnl) >= 0 ? 'text-profit' : 'text-loss'}>{fmtPnl(summary.avg_pnl)}</span> / trade</>
          : 'avg — / trade'}
      >
        {resolved}
      </Cell>
    </div>
  )
}

// ─── Trade rows ───────────────────────────────────────────────────────────────

// Grid column template (desktop): [tag] [entry] [expiry] [strikes] [credit] [net P&L]
const GRID_COLS = '56px 64px 64px 1fr 80px 82px'

function HeaderRow() {
  return (
    <div
      className="hidden sm:grid border-b border-border px-4 py-2"
      style={{ gridTemplateColumns: GRID_COLS, gap: 10, alignItems: 'center' }}
    >
      {['Status', 'Entry', 'Expiry', 'Strikes', 'Credit (pts)', 'Net P&L'].map(h => (
        <span
          key={h}
          className="text-[10px] font-semibold uppercase tracking-[.08em] text-muted-foreground"
        >
          {h}
        </span>
      ))}
    </div>
  )
}

function TradeRow({ t, isLast }) {
  const tone     = rowTagTone(t)
  const label    = rowTagLabel(t)
  const strikes  = fmtStrikes(t)
  const pnl      = t?.net_pnl ?? null
  const pnlStr   = pnl != null ? fmtPnl(pnl) : '—'
  const pnlColor = pnl != null
    ? (Number(pnl) >= 0 ? 'hsl(var(--profit))' : 'hsl(var(--loss))')
    : undefined

  const credit     = t?.credit != null ? Number(t.credit).toFixed(1) : '—'
  const entryDate  = fmtDate(t?.entry_date)
  const expiryDate = fmtDate(t?.expiry_date)

  const borderStyle = !isLast ? { borderBottom: '1px solid hsl(var(--border))' } : {}

  return (
    <>
      {/* ── Desktop row (sm+) ────────────────────────────────────────────── */}
      <div
        className="hidden sm:grid items-center px-4 py-2.5"
        style={{ gridTemplateColumns: GRID_COLS, gap: 10, ...borderStyle }}
      >
        {/* Status tag */}
        <Tag tone={tone}>{label}</Tag>

        {/* Entry date */}
        <span className="mono text-[11px] text-faint">{entryDate}</span>

        {/* Expiry date */}
        <span className="mono text-[11px] text-faint">{expiryDate}</span>

        {/* Strikes */}
        <span className="mono text-[11px] text-foreground">{strikes}</span>

        {/* Credit (pts) */}
        <span className="mono text-[11px] text-muted-foreground">{credit}</span>

        {/* Net P&L */}
        <span
          className="mono text-[12px] font-semibold"
          style={{ color: pnlColor }}
        >
          {pnlStr}
        </span>
      </div>

      {/* ── Mobile card (< sm) — 2-line layout ──────────────────────────── */}
      <div
        className="sm:hidden px-4 py-3"
        style={borderStyle}
      >
        {/* Line 1: expiry + status + net P&L */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Tag tone={tone}>{label}</Tag>
            <span className="mono text-[11px] text-muted-foreground truncate">
              exp {expiryDate}
            </span>
          </div>
          <span
            className="mono text-[12px] font-semibold shrink-0"
            style={{ color: pnlColor }}
          >
            {pnlStr}
          </span>
        </div>
        {/* Line 2: strikes + credit (faint) */}
        <div className="mt-1 mono text-[10px] text-faint truncate">
          {strikes} · {credit} pts
        </div>
      </div>
    </>
  )
}

// ─── Trade table ─────────────────────────────────────────────────────────────

function TradeTable({ trades }) {
  if (!trades?.length) {
    return (
      <div className="mono px-4 py-6 text-[10px] text-faint">
        No condor paper trades yet
      </div>
    )
  }

  return (
    <div style={{ maxHeight: 380, overflowY: 'auto' }}>
      <HeaderRow />
      {trades.map((t, i) => (
        <TradeRow key={t?.id ?? i} t={t} isLast={i === trades.length - 1} />
      ))}
    </div>
  )
}

// ─── Root export ─────────────────────────────────────────────────────────────

export default function PaperLogTable({ paper }) {
  const summary  = paper?.summary  ?? null
  const trades   = paper?.trades   ?? []
  const resolved = summary?.resolved ?? 0
  const open     = summary?.open     ?? 0

  const metaParts = []
  if (resolved > 0) metaParts.push(`${resolved} resolved`)
  if (open     > 0) metaParts.push(`${open} open`)
  const metaStr = metaParts.length ? metaParts.join(' · ') : 'no trades'

  return (
    <Panel>
      <PanelHeader
        title="Condor Paper-Log (real IV)"
        meta={metaStr}
      />

      {/* Aggregate strip */}
      <AggStrip summary={summary} />

      {/* Trade table */}
      <div className="px-0 pb-0">
        <TradeTable trades={trades} />
      </div>
    </Panel>
  )
}
