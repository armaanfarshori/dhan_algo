import { useState, useEffect } from 'react'
import { Panel, PanelHeader, Tag, Pill, Badge } from '@/components/ui'

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Tag tone for a direction string. */
function dirTone(dir) {
  if (dir === 'LONG')  return 'long'
  if (dir === 'SHORT') return 'short'
  return 'default'
}

/** Seconds → "Mm Ss" / "Ss". */
function fmtHold(secs) {
  if (secs == null || !Number.isFinite(secs) || secs < 0) return '—'
  const m = Math.floor(secs / 60)
  const s = Math.floor(secs % 60)
  return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`
}

function fmtNum(v, dp = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(dp)
}

// ─── A small labelled cell ─────────────────────────────────────────────────────

function Cell({ label, children }) {
  return (
    <div className="min-w-0">
      <div className="mono text-[9.5px] font-semibold uppercase tracking-[.08em] text-faint">{label}</div>
      <div className="mono mt-1 truncate text-[13px] font-semibold text-foreground">{children}</div>
    </div>
  )
}

// ─── Open-position rows with client-side hold timer ────────────────────────────

function OpenPositions({ positions }) {
  // tick once per second so the hold timer counts up live
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  if (!positions?.length) {
    return (
      <div className="mono px-4 py-5 text-[10px] text-faint">
        No open scalper tranches
      </div>
    )
  }

  return (
    <div>
      {positions.map((p, i) => {
        const heldSecs = p.entry_time
          ? (now - new Date(p.entry_time).getTime()) / 1000
          : null
        return (
          <div
            key={p.id ?? `${p.option_type}-${p.strike}-${i}`}
            className="grid items-center gap-2 border-b border-border px-4 py-2.5 last:border-b-0 sm:grid-cols-[64px_1fr_72px_72px_72px]"
          >
            <Tag tone={dirTone(p.direction)}>{p.direction ?? '—'}</Tag>
            <span className="mono text-[12px] text-foreground">
              {(p.option_type ?? '?')}{' '}{p.strike ?? '?'}
              <span className="ml-2 text-faint">×{p.lots ?? '?'} lot</span>
            </span>
            <span className="mono text-[11px] text-muted-foreground">@{fmtNum(p.entry_premium)}</span>
            <span className="mono text-[11px] text-muted-foreground">u {fmtNum(p.entry_underlying, 0)}</span>
            <span className="mono text-[11px] text-sky">{fmtHold(heldSecs)}</span>
          </div>
        )
      })}
    </div>
  )
}

// ─── LiveSignalPanel ────────────────────────────────────────────────────────────

/**
 * Live scalper signal + open-position snapshot from /api/scalper/live (hb["scalper"]).
 *
 * Prop: live = body or null:
 *   { available, trader_alive, state: {
 *       direction, vwap, ladder_direction, ladder_option_type, ladder_strike,
 *       position_lots, rungs_requested, warm, standing_down,
 *       open_positions?: [{ id, direction, option_type, strike, lots,
 *                           entry_time, entry_premium, entry_underlying }] } }
 *
 * The open_positions array is optional (a richer heartbeat may not expose it);
 * the hold timer ticks client-side off entry_time.
 */
export default function LiveSignalPanel({ live }) {
  const available = !!(live && live.available)
  const st = available ? (live.state ?? {}) : {}

  if (!available) {
    return (
      <Panel>
        <PanelHeader title="Live Signal" />
        <div className="mono px-4 py-6 text-[10px] text-faint">
          Scalper is OFF — start it from the control bar to see the live signal.
        </div>
      </Panel>
    )
  }

  const direction = st.direction ?? 'FLAT'
  const positions = st.open_positions ?? []

  return (
    <Panel>
      <PanelHeader
        title="Live Signal"
        meta={
          <span className="flex items-center gap-2">
            {st.warm === false && <Pill tone="neu">warming up</Pill>}
            {st.standing_down && <Badge variant="loss">STOOD DOWN</Badge>}
            <Tag tone={dirTone(direction)}>{direction}</Tag>
          </span>
        }
      />

      {/* Signal context strip */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-border px-4 py-4 sm:grid-cols-4">
        <Cell label="Direction">
          <span className={direction === 'LONG' ? 'text-profit' : direction === 'SHORT' ? 'text-loss' : 'text-faint'}>
            {direction}
          </span>
        </Cell>
        <Cell label="VWAP">{fmtNum(st.vwap, 1)}</Cell>
        <Cell label="Position">
          {st.position_lots ? `${st.position_lots} lot` : '—'}
        </Cell>
        <Cell label="Rungs">
          {st.rungs_requested != null ? st.rungs_requested : '—'}
          {st.ladder_option_type ? ` · ${st.ladder_option_type} ${st.ladder_strike ?? ''}` : ''}
        </Cell>
      </div>

      {/* Open tranches with live hold timer */}
      <OpenPositions positions={positions} />
    </Panel>
  )
}
