import { Panel, PanelHeader, Tag } from '@/components/ui'
import { INR0 } from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

function fmtPnl(v) {
  if (v == null) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return '—'
  return (n >= 0 ? '+' : '−') + INR0(Math.abs(n))
}

/** ISO timestamp → "HH:MM" in IST. */
function fmtClock(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })
}

function fmtPrem(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(2)
}

function rowTone(t) {
  if (t?.status !== 'CLOSED') return 'default'
  return t?.win ? 'long' : 'short'
}

function rowLabel(t) {
  if (t?.status !== 'CLOSED') return 'OPEN'
  return t?.win ? 'WIN' : 'LOSS'
}

function dirText(t) {
  const d = t?.direction ?? ''
  const ot = t?.option_type ?? ''
  const k = t?.strike ?? ''
  return `${d ? d + ' ' : ''}${ot}${k}`.trim() || '—'
}

// ─── Aggregate strip ────────────────────────────────────────────────────────────

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
  const closed   = summary?.closed   ?? 0
  const open     = summary?.open     ?? 0
  const net_pnl  = summary?.net_pnl  ?? null
  const win_rate = summary?.win_rate ?? null
  const wins     = summary?.wins     ?? 0

  const winRateDisplay = closed === 0
    ? '—'
    : win_rate != null ? (Number(win_rate) * 100).toFixed(0) + '%' : '—'

  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-border px-4 py-4 sm:grid-cols-4">
      <Cell label="Net P&L" sub="net of charges">
        {net_pnl != null
          ? <span className={Number(net_pnl) >= 0 ? 'text-profit' : 'text-loss'}>{fmtPnl(net_pnl)}</span>
          : '—'}
      </Cell>
      <Cell label="Win Rate" sub={`of ${closed} closed`}>{winRateDisplay}</Cell>
      <Cell label="# Open" sub="live tranches">{open}</Cell>
      <Cell
        label="# Closed"
        sub={summary?.avg_pnl != null
          ? <>avg <span className={Number(summary.avg_pnl) >= 0 ? 'text-profit' : 'text-loss'}>{fmtPnl(summary.avg_pnl)}</span></>
          : 'avg — / trade'}
      >
        {closed}{closed > 0 ? <span className="ml-1 text-[11px] text-faint">({wins}W)</span> : null}
      </Cell>
    </div>
  )
}

// ─── Trade rows ───────────────────────────────────────────────────────────────

const GRID_COLS = '56px 1fr 64px 64px 80px 82px'

function HeaderRow() {
  return (
    <div
      className="hidden border-b border-border px-4 py-2 sm:grid"
      style={{ gridTemplateColumns: GRID_COLS, gap: 10, alignItems: 'center' }}
    >
      {['Status', 'Contract', 'Entry', 'Exit', 'Premium', 'Net P&L'].map(h => (
        <span key={h} className="text-[10px] font-semibold uppercase tracking-[.08em] text-muted-foreground">{h}</span>
      ))}
    </div>
  )
}

function TradeRow({ t, isLast }) {
  const pnl = t?.net_pnl ?? null
  const pnlStr = fmtPnl(pnl)
  const pnlColor = pnl != null ? (Number(pnl) >= 0 ? 'hsl(var(--profit))' : 'hsl(var(--loss))') : undefined
  const prem = `${fmtPrem(t?.entry_premium)}→${fmtPrem(t?.exit_premium)}`
  const borderStyle = !isLast ? { borderBottom: '1px solid hsl(var(--border))' } : {}

  return (
    <>
      {/* Desktop */}
      <div className="hidden items-center px-4 py-2.5 sm:grid"
           style={{ gridTemplateColumns: GRID_COLS, gap: 10, ...borderStyle }}>
        <Tag tone={rowTone(t)}>{rowLabel(t)}</Tag>
        <span className="mono truncate text-[11px] text-foreground">{dirText(t)} <span className="text-faint">×{t?.lots ?? '?'}</span></span>
        <span className="mono text-[11px] text-faint">{fmtClock(t?.entry_time)}</span>
        <span className="mono text-[11px] text-faint">{fmtClock(t?.exit_time)}</span>
        <span className="mono text-[11px] text-muted-foreground">{prem}</span>
        <span className="mono text-[12px] font-semibold" style={{ color: pnlColor }}>{pnlStr}</span>
      </div>

      {/* Mobile */}
      <div className="px-4 py-3 sm:hidden" style={borderStyle}>
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Tag tone={rowTone(t)}>{rowLabel(t)}</Tag>
            <span className="mono truncate text-[11px] text-muted-foreground">{dirText(t)} ×{t?.lots ?? '?'}</span>
          </div>
          <span className="mono shrink-0 text-[12px] font-semibold" style={{ color: pnlColor }}>{pnlStr}</span>
        </div>
        <div className="mono mt-1 truncate text-[10px] text-faint">
          {fmtClock(t?.entry_time)}→{fmtClock(t?.exit_time)} · {prem}
        </div>
      </div>
    </>
  )
}

function Table({ trades }) {
  if (!trades?.length) {
    return <div className="mono px-4 py-6 text-[10px] text-faint">No scalper trades yet</div>
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

// ─── ScalperTradesTable ─────────────────────────────────────────────────────────

/**
 * Recent scalper trades — summary strip + newest-first trade list (net-of-charges).
 *
 * Prop: trades = /api/scalper/trades body or null:
 *   { summary:{ closed, open, net_pnl, win_rate, wins, avg_pnl },
 *     trades:[{ id, status, direction, option_type, strike, lots,
 *               entry_time, entry_premium, exit_time, exit_premium, net_pnl, win }] }
 */
export default function ScalperTradesTable({ trades }) {
  const summary = trades?.summary ?? null
  const list    = trades?.trades  ?? []
  const closed  = summary?.closed ?? 0
  const open    = summary?.open   ?? 0

  const metaParts = []
  if (closed > 0) metaParts.push(`${closed} closed`)
  if (open   > 0) metaParts.push(`${open} open`)
  const metaStr = metaParts.length ? metaParts.join(' · ') : 'no trades'

  return (
    <Panel>
      <PanelHeader title="Recent Scalper Trades" meta={metaStr} />
      <AggStrip summary={summary} />
      <div className="px-0 pb-0">
        <Table trades={list} />
      </div>
    </Panel>
  )
}
