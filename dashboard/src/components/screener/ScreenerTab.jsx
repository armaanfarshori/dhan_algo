/**
 * ScreenerTab — daily screened-stock audit log.
 *
 * Browses the per-trading-day record of what the ATR% screener produced and
 * which names made the tradeable watchlist (`selected`). Data: /api/screen/days
 * (the day dropdown) + /api/screen?date=YYYY-MM-DD (one day's rows). The audit
 * answers "why was security X in the universe on day D?".
 */
import { useState } from 'react'
import { Panel, PanelHeader, StatCard, Pill, Tag } from '@/components/ui'
import { usePoller } from '@/hooks/usePoller'
import { INR0 } from '@/tokens'

function compactVol(v) {
  if (v == null) return '—'
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)}Cr`
  if (v >= 1e5) return `${(v / 1e5).toFixed(1)}L`
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`
  return String(v)
}

function fmtDay(iso) {
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short' })
}

// Mobile shows rank/ticker/ATR%/status only (4 cols) to fit narrow screens;
// ≥sm restores price + volume (6 cols).
const COLS = 'grid grid-cols-[28px_minmax(0,1fr)_56px_auto] sm:grid-cols-[34px_minmax(0,1fr)_64px_84px_72px_auto] items-center gap-2 sm:gap-2.5 px-3 sm:px-4'

export default function ScreenerTab() {
  const daysResp = usePoller('/api/screen/days', 60000)
  const days = daysResp.data?.days ?? []

  const [picked, setPicked] = useState(null)
  const date = picked ?? days[0] ?? ''                 // default = newest day
  const screen = usePoller(date ? `/api/screen?date=${date}` : '/api/screen', 30000)
  const rows = screen.data?.rows ?? []
  const selected = rows.filter(r => r.selected).length

  return (
    <div className="px-5 py-5 pb-16 lg:px-6">
      {/* KPI row */}
      <div className="mb-3.5 grid grid-cols-2 gap-3.5 sm:grid-cols-3">
        <StatCard
          label="Screened"
          value={<span className="text-foreground">{rows.length || '—'}</span>}
          sub={`${days.length} trading day${days.length !== 1 ? 's' : ''} logged`}
        />
        <StatCard
          label="Selected"
          right={<Pill tone="up">watchlist</Pill>}
          value={<span className="text-foreground">{rows.length ? selected : '—'}</span>}
          sub="made the tradeable set"
        />
        <StatCard
          label="Trading day"
          value={<span className="text-foreground mono text-[18px]">{date ? fmtDay(date) : '—'}</span>}
          sub={date || 'no screen logged yet'}
        />
      </div>

      <Panel>
        <PanelHeader
          title="Daily Screen"
          meta={
            days.length ? (
              <select
                value={date}
                onChange={(e) => setPicked(e.target.value)}
                className="mono rounded-[var(--radius-sm)] border border-border2 bg-card px-2 py-1 text-[11px] text-foreground outline-none focus-visible:border-faint"
                aria-label="Trading day"
              >
                {days.map(d => <option key={d} value={d}>{fmtDay(d)}</option>)}
              </select>
            ) : null
          }
        />

        {/* column header */}
        <div className={`${COLS} border-b border-border py-2 text-[9.5px] uppercase tracking-[.07em] text-faint`}>
          <span>#</span><span>ticker</span><span className="text-right">ATR%</span>
          <span className="hidden text-right sm:block">price</span>
          <span className="hidden text-right sm:block">volume</span>
          <span className="text-right">status</span>
        </div>

        {rows.length === 0 ? (
          <div className="px-4 py-10 text-center text-[12px] text-muted-foreground">
            {screen.loading ? 'Loading…' : 'No screen recorded for this day'}
          </div>
        ) : (
          rows.map((r, i) => (
            <div
              key={r.security_id}
              className={`${COLS} border-b border-border py-[9px] last:border-b-0 ${r.selected ? '' : 'opacity-70'}`}
            >
              <span className="mono text-[11px] text-faint">{r.rank ?? i + 1}</span>
              <span className="truncate text-[12px] font-medium text-foreground">{r.ticker}</span>
              <span className="mono text-right text-[11.5px] text-foreground">
                {r.score != null ? `${(r.score * 100).toFixed(2)}%` : '—'}
              </span>
              <span className="mono hidden text-right text-[11.5px] text-muted-foreground sm:block">
                {r.price != null ? INR0(r.price) : '—'}
              </span>
              <span className="mono hidden text-right text-[11px] text-faint sm:block">{compactVol(r.volume)}</span>
              <span className="flex justify-end">
                {r.selected
                  ? <Tag tone="long"><span className="sm:hidden">✓</span><span className="hidden sm:inline">✓ watchlist</span></Tag>
                  : <span className="text-[10px] text-faint">·</span>}
              </span>
            </div>
          ))
        )}
      </Panel>
    </div>
  )
}
