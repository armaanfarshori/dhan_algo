import { useState, useEffect, Component } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { T, INR, INR0, colorPnl, fmtTime, fmtUptime } from './tokens'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import FloatingKillSwitch from './components/cockpit/FloatingKillSwitch'

// ─────────────────────────────────────────────────────────────────
// Error boundary — prevents blank screen on render errors
// ─────────────────────────────────────────────────────────────────
class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(e) { return { error: e } }
  componentDidCatch(e, info) { console.error('DhanAI render error:', e, info) }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 32, fontFamily: "'JetBrains Mono', monospace", color: '#e7ebf2', background: '#07080a', minHeight: '100vh' }}>
          <div style={{ color: 'oklch(0.68 0.22 25)', fontSize: 13, marginBottom: 12 }}>⚠ RENDER ERROR</div>
          <pre style={{ fontSize: 11, color: '#6b7589', whiteSpace: 'pre-wrap', maxWidth: 800 }}>
            {this.state.error?.message}
          </pre>
          <button
            onClick={() => this.setState({ error: null })}
            style={{ marginTop: 16, fontFamily: "'JetBrains Mono', monospace", fontSize: 10,
              background: 'none', border: '1px solid #1f242e', color: '#a9b2c2',
              padding: '6px 14px', cursor: 'pointer' }}
          >
            RETRY
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

// ─────────────────────────────────────────────────────────────────
// Clock
// ─────────────────────────────────────────────────────────────────
function ClockIST() {
  const [t, setT] = useState(new Date())
  useEffect(() => { const i = setInterval(() => setT(new Date()), 1000); return () => clearInterval(i) }, [])
  return (
    <span style={{ fontFamily: T.mono, fontSize: 11, color: T.ink3 }}>
      {t.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST
    </span>
  )
}

// ─────────────────────────────────────────────────────────────────
// Header
// ─────────────────────────────────────────────────────────────────
function Badge({ label, tone, pulse }) {
  const tones = {
    live:   { bg: T.redD,  fg: T.red,   bd: T.red },
    paper:  { bg: T.bg3,   fg: T.ink1,  bd: T.line2 },
    shadow: { bg: T.bg3,   fg: T.amber, bd: T.amber },
    armed:  { bg: T.bg3,   fg: T.cyan,  bd: T.cyan },
    ok:     { bg: T.bg3,   fg: T.green, bd: T.greenD },
    down:   { bg: T.redD,  fg: T.red,   bd: T.red },
    dim:    { bg: T.bg3,   fg: T.ink3,  bd: T.line },
  }
  const c = tones[tone] ?? tones.dim
  return (
    <span style={{
      fontFamily: T.mono, fontSize: 9, letterSpacing: '0.18em',
      padding: '3px 8px', whiteSpace: 'nowrap',
      background: c.bg, color: c.fg, border: `1px solid ${c.bd}`,
      animation: pulse ? 'pulse 1s ease-in-out infinite' : 'none',
    }}>{label}</span>
  )
}

// The status spine answers "is everything OK?" in one glance, on every tab:
// mode · gate · engine · feed · balance · day P&L · halt — all from the
// 2s snapshot (trader heartbeat), so a dead engine shows up within seconds.
function StatusSpine({ data }) {
  const t       = data.trader
  const alive   = data.alive
  const mode    = t?.mode ?? 'PAPER'
  const gate    = (t?.kronos_gate ?? 'shadow').toUpperCase()
  const feedOk  = !!t?.feed?.connected
  const pnl     = t?.portfolio?.total_pnl ?? 0
  const halted  = !!t?.risk?.halted
  const balance = data.funds?.data?.data?.availabelBalance ?? 0

  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 100,
      background: T.bg0,
      borderBottom: `1px solid ${T.line}`,
      display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
      padding: '0 24px', minHeight: 44,
    }}>
      <span style={{ fontFamily: T.mono, fontSize: 13, fontWeight: 700, color: T.ink0, letterSpacing: '0.1em', marginRight: 4 }}>
        DHAN<span style={{ color: T.cyan }}>AI</span>
      </span>

      <Badge label={mode} tone={mode === 'LIVE' ? 'live' : 'paper'} />
      <Badge label={`GATE ${gate}`} tone={gate === 'SHADOW' ? 'shadow' : 'armed'} />

      {/* Engine heartbeat */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={{
          width: 6, height: 6, borderRadius: '50%',
          background: alive ? T.green : T.red,
          boxShadow: `0 0 8px ${alive ? T.green : T.red}`,
          animation: alive ? 'none' : 'pulse 1s ease-in-out infinite',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 10, color: alive ? T.ink3 : T.red }}>
          {alive ? `ENGINE ${fmtUptime(t?.uptime_seconds ?? 0)}` : 'ENGINE OFFLINE'}
        </span>
      </div>

      {/* WS feed */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%',
          background: feedOk ? T.cyan : T.ink3,
          boxShadow: feedOk ? `0 0 6px ${T.cyan}` : 'none' }} />
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          {feedOk ? `FEED ${t?.feed?.subscribed ?? 0}` : 'FEED —'}
        </span>
      </div>

      <div style={{ flex: 1 }} />

      <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>{INR(balance)}</span>

      <span style={{
        fontFamily: T.dot, fontSize: 22,
        color: colorPnl(pnl), minWidth: 100, textAlign: 'right',
      }}>
        {pnl >= 0 ? '+' : ''}{INR0(pnl)}
      </span>

      {halted && <Badge label="⛔ HALTED" tone="down" pulse />}
      <ClockIST />
    </header>
  )
}

// Full-width alarm strip when the engine or the API itself is gone. The
// worst dashboard failure is confidently showing stale numbers — this makes
// staleness impossible to miss.
function OfflineBanner({ data }) {
  const apiDown = !!data.snapshot.error
  const stale   = !apiDown && data.snapshot.data != null && !data.alive
  if (!apiDown && !stale) return null
  const msg = apiDown
    ? 'DASHBOARD API UNREACHABLE — data frozen · check dhan-api service'
    : `TRADING ENGINE OFFLINE — last heartbeat ${fmtTime(data.trader?.ts)} · positions are NOT being managed`
  return (
    <div style={{
      background: T.redD, borderBottom: `1px solid ${T.red}`,
      padding: '8px 24px', display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <span style={{ fontFamily: T.mono, fontSize: 11, color: T.red, fontWeight: 700 }}>⛔</span>
      <span style={{ fontFamily: T.mono, fontSize: 10, color: T.red, letterSpacing: '0.08em' }}>{msg}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Tab bar
// ─────────────────────────────────────────────────────────────────
const TABS = ['Signals', 'Portfolio', 'System']

function TabBar({ active, onChange }) {
  return (
    <div style={{
      display: 'flex', gap: 0,
      borderBottom: `1px solid ${T.line}`,
      padding: '0 24px',
      background: T.bg0,
    }}>
      {TABS.map(t => {
        const on = t === active
        return (
          <button key={t} onClick={() => onChange(t)} style={{
            background: 'none', border: 'none', cursor: 'pointer',
            fontFamily: T.mono, fontSize: 10, letterSpacing: '0.2em',
            textTransform: 'uppercase',
            color:   on ? T.cyan : T.ink3,
            padding: '10px 18px 9px',
            borderBottom: on ? `2px solid ${T.cyan}` : '2px solid transparent',
            transition: 'color 0.15s, border-color 0.15s',
          }}>
            {t}
          </button>
        )
      })}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Kronos signal card
// ─────────────────────────────────────────────────────────────────
const SIDE_CFG = {
  BUY:  { label: '📈 BUY',  color: T.green, bg: T.greenD, border: T.green },
  SELL: { label: '📉 SELL', color: T.red,   bg: T.redD,   border: T.red   },
  HOLD: { label: '➖ HOLD', color: T.ink3,  bg: T.bg3,    border: T.line  },
}

function _atrPct(reason) {
  if (!reason) return null
  const m = reason.match(/ATR%=([\d.]+)/)
  return m ? parseFloat(m[1]) : null
}

// Card for the live Kronos scanner — uses ticker NAME, not ID
function SignalCard({ sig }) {
  const cfg  = SIDE_CFG[sig.side] ?? SIDE_CFG.HOLD
  const conf = Math.round((sig.confidence ?? 0) * 100)
  const ret  = (sig.forecast_return ?? 0) * 100
  const atrPct = _atrPct(sig.atr_reason)
  const name = sig.ticker || sig.security_id

  return (
    <div style={{
      background: T.bg1,
      border: `1px solid ${T.line}`,
      borderLeft: `3px solid ${cfg.border}`,
      padding: 16,
      display: 'flex', flexDirection: 'column', gap: 8,
      position: 'relative', overflow: 'hidden',
    }}>
      {/* Ticker name + badge */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ fontFamily: T.mono, fontSize: 14, fontWeight: 700, color: T.ink0 }}>
            {name}
          </div>
          {sig.name && (
            <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 2,
              maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {sig.name}
            </div>
          )}
        </div>
        <span style={{
          fontFamily: T.mono, fontSize: 9, letterSpacing: '0.15em',
          padding: '2px 7px', flexShrink: 0,
          background: cfg.bg, color: cfg.color,
          border: `1px solid ${cfg.border}`,
        }}>
          {cfg.label}
        </span>
      </div>

      {/* Confidence bar */}
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.1em' }}>CONFIDENCE</span>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: cfg.color }}>{conf}%</span>
        </div>
        <div style={{ height: 3, background: T.bg3, overflow: 'hidden' }}>
          <div style={{
            height: '100%', width: `${conf}%`,
            background: cfg.color,
            opacity: conf < 40 ? 0.4 : 1,
            transition: 'width 0.6s ease',
          }} />
        </div>
      </div>

      {/* Numbers */}
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 2 }}>FORECAST Δ</div>
          <div style={{ fontFamily: T.dot, fontSize: 18, color: ret >= 0 ? T.green : T.red }}>
            {ret >= 0 ? '+' : ''}{ret.toFixed(3)}%
          </div>
        </div>
        {atrPct !== null && (
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 2 }}>ATR%</div>
            <div style={{ fontFamily: T.dot, fontSize: 18, color: T.amber }}>{atrPct.toFixed(2)}%</div>
          </div>
        )}
      </div>

      {conf >= 60 && (
        <div style={{
          position: 'absolute', inset: 0, pointerEvents: 'none',
          background: `radial-gradient(ellipse at top right, ${cfg.color}08, transparent 70%)`,
        }} />
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Kronos board
// ─────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────
// Market session status
// ─────────────────────────────────────────────────────────────────
function SessionBar({ status }) {
  const [now, setNow] = useState(new Date())
  useEffect(() => { const i = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(i) }, [])

  const ist    = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }))
  const h = ist.getHours(), m = ist.getMinutes(), wd = ist.getDay()
  const mins   = h * 60 + m
  const isWeekday = wd >= 1 && wd <= 5
  const preOpen = isWeekday && mins >= 9*60 && mins < 9*60+15
  const open    = isWeekday && mins >= 9*60+15 && mins < 15*60+30
  const postClose = isWeekday && mins >= 15*60+30

  const label = !isWeekday ? 'WEEKEND' : preOpen ? 'PRE-OPEN' : open ? 'MARKET OPEN' : postClose ? 'MARKET CLOSED' : 'PRE-MARKET'
  const color = open ? T.green : preOpen ? T.amber : T.ink3
  const nextEvent = open
    ? `Closes in ${15*60+30 - mins}m`
    : (!isWeekday || postClose) ? 'Next: Mon 09:15 IST'
    : `Opens in ${9*60+15 - mins}m`

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 14,
      padding: '8px 0', marginBottom: 16,
      borderBottom: `1px solid ${T.line}`,
    }}>
      <div style={{ width: 8, height: 8, borderRadius: '50%', background: color,
        boxShadow: open ? `0 0 8px ${T.green}` : 'none', flexShrink: 0 }} />
      <span style={{ fontFamily: T.mono, fontSize: 10, color, letterSpacing: '0.15em', fontWeight: 600 }}>
        NSE {label}
      </span>
      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{nextEvent}</span>
      <span style={{ marginLeft: 'auto', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
        {ist.toLocaleTimeString('en-IN', { hour12: false })} IST
      </span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Signal card (compact)
// ─────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────
// Screened-today panel — the day's full screened universe
// ─────────────────────────────────────────────────────────────────
function ScreenedToday({ kronosLive }) {
  const screened = kronosLive?.data?.screened_today?.securities ?? {}
  const entries  = Object.entries(screened)
    .map(([sid, v]) => ({ sid, ...v }))
    .sort((a, b) => (b.times_seen ?? 0) - (a.times_seen ?? 0))

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.cyan, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          SCREENED TODAY
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {entries.length} unique securities · ATR-ranked
        </span>
      </div>
      {entries.length === 0 ? (
        <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          No securities screened yet today
        </div>
      ) : (
        <div style={{ maxHeight: 200, overflowY: 'auto' }}>
          {entries.map((e, i) => (
            <div key={e.sid} style={{
              display: 'grid', gridTemplateColumns: '80px 1fr 70px 50px', gap: 10,
              padding: '6px 16px', alignItems: 'center',
              background: i % 2 === 0 ? T.bg1 : T.bg2,
              borderBottom: `1px solid ${T.line}`,
            }}>
              <span style={{ fontFamily: T.mono, fontSize: 11, fontWeight: 700, color: T.ink0 }}>{e.ticker}</span>
              <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.name}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.amber }}>{e.atr_reason?.match(/ATR%=[\d.]+%?/)?.[0] ?? ''}</span>
              <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, textAlign: 'right' }}>{e.first_seen}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Action watchlist — persistent (securities with live trades/positions)
// ─────────────────────────────────────────────────────────────────
function ActionWatchlist({ positions, paperPositions, tradelog, kronosSignals }) {
  const live   = (positions?.data?.data ?? []).filter(p => p.netQty !== 0)
  const paper  = (paperPositions?.data?.data?.data ?? paperPositions?.data?.data ?? []).filter(p => p.in_position)
  const recent = (tradelog?.data?.trades ?? [])
    .filter(t => t.status === 'OPEN')
    .slice(-5)
    .map(t => t.symbol ?? t.security_id ?? '?')
  const active = live.length > 0 ? live : paper

  const sigMap = {}
  ;(kronosSignals?.data?.signals ?? []).forEach(s => { sigMap[s.security_id] = s })

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.amber, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          ACTION WATCHLIST
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          persistent · positions + recent trades
        </span>
      </div>
      {active.length === 0 ? (
        <div style={{ padding: '16px', fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          No active positions — securities will appear here when trades are taken
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {active.map((p, i) => {
            const sid   = String(p.securityId ?? p.security_id ?? '')
            const qty   = p.netQty ?? p.qty ?? 0
            const entry = p.buyAvg ?? p.entry_price ?? 0
            const upnl  = p.unrealisedProfit ?? 0
            const sig   = sigMap[sid]
            return (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 14,
                padding: '10px 16px',
                borderBottom: `1px solid ${T.line}`,
                borderLeft: `3px solid ${qty > 0 ? T.green : T.red}`,
                background: i % 2 === 0 ? T.bg1 : T.bg2,
              }}>
                <span style={{ display: 'flex', flexDirection: 'column', minWidth: 90 }}>
                  <span style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 700, color: T.ink0 }}>{p.ticker ?? sig?.ticker ?? sid}</span>
                  {sig?.name && <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 120 }}>{sig.name}</span>}
                </span>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: qty > 0 ? T.green : T.red }}>{qty > 0 ? 'LONG' : 'SHORT'}</span>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>{Math.abs(qty)} × ₹{entry?.toFixed?.(2)}</span>
                {sig && (
                  <span style={{ fontFamily: T.mono, fontSize: 9, padding: '2px 6px',
                    background: SIDE_CFG[sig.side]?.bg ?? T.bg3,
                    color: SIDE_CFG[sig.side]?.color ?? T.ink3 }}>
                    Kronos: {sig.side} {Math.round((sig.confidence??0)*100)}%
                  </span>
                )}
                <span style={{ marginLeft: 'auto', fontFamily: T.dot, fontSize: 20, color: colorPnl(upnl) }}>
                  {upnl >= 0 ? '+' : ''}₹{Math.round(upnl).toLocaleString('en-IN')}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Right panel — PnL summary + live signals feed
// ─────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────
// ORB cockpit — one card per runner, straight from the trader heartbeat.
// ─────────────────────────────────────────────────────────────────
const VERDICT_TONE = { ALLOW: T.green, BLOCK: T.red }

function RangeLadder({ s }) {
  const lo = s.or_low, hi = s.or_high, px = s.last_price
  if (!lo || !hi || hi <= lo) {
    return (
      <div style={{ height: 34, display: 'flex', alignItems: 'center' }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {s.or_locked ? 'zero-width range — no trade' : 'building opening range…'}
        </span>
      </div>
    )
  }
  const range = hi - lo
  const pad   = Math.max(range * 0.6, hi * 0.002)
  const wLo   = lo - pad, span = (hi + pad) - wLo
  const pct   = v => Math.min(98.5, Math.max(1.5, ((v - wLo) / span) * 100))
  const inPos = s.position !== 0

  return (
    <div style={{ padding: '6px 0 2px' }}>
      <div style={{ position: 'relative', height: 18, background: T.bg3 }}>
        <div style={{ position: 'absolute', top: 0, bottom: 0,
          left: `${pct(lo)}%`, width: `${pct(hi) - pct(lo)}%`,
          background: T.bg2, borderLeft: `2px solid ${T.redD}`, borderRight: `2px solid ${T.greenD}` }} />
        {inPos && s.entry_price > 0 && (
          <div style={{ position: 'absolute', top: 2, bottom: 2, left: `${pct(s.entry_price)}%`,
            width: 1, background: T.ink1 }} />
        )}
        {px > 0 && (
          <div style={{ position: 'absolute', top: -2, bottom: -2, left: `calc(${pct(px)}% - 1px)`,
            width: 3, background: px > hi ? T.green : px < lo ? T.red : T.cyan,
            boxShadow: `0 0 8px ${px > hi ? T.green : px < lo ? T.red : T.cyan}` }} />
        )}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
        <span style={{ fontFamily: T.dot, fontSize: 14, color: T.red }}>{lo.toFixed(2)}</span>
        <span style={{ fontFamily: T.dot, fontSize: 16, color: T.ink0 }}>
          {px > 0 ? px.toFixed(2) : '—'}
        </span>
        <span style={{ fontFamily: T.dot, fontSize: 14, color: T.green }}>{hi.toFixed(2)}</span>
      </div>
    </div>
  )
}

function ORBCard({ s, gateDec, maxEntries }) {
  const inPos = s.position !== 0
  const side  = s.position > 0 ? 'LONG' : 'SHORT'
  const state = !s.or_locked ? 'BUILDING' : inPos ? side : 'WATCHING'
  const stateColor = !s.or_locked ? T.amber : inPos ? (s.position > 0 ? T.green : T.red) : T.ink2

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`,
      borderTop: `2px solid ${stateColor}`, padding: '12px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 4 }}>
        <span style={{ fontFamily: T.mono, fontSize: 13, fontWeight: 700, color: T.ink0 }}>
          {s.ticker ?? s.security_id}
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, letterSpacing: '0.15em', color: stateColor }}>
          {state}{inPos ? ` ${Math.abs(s.position)} @ ₹${(s.entry_price ?? 0).toFixed(2)}` : ''}
        </span>
        <span style={{ marginLeft: 'auto', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          entries {s.entries_today ?? 0}/{maxEntries}
        </span>
      </div>

      <RangeLadder s={s} />

      <div style={{ marginTop: 8, minHeight: 16 }}>
        {gateDec ? (
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>
            <span style={{ color: T.amber }}>{gateDec.shadow ? '[SHADOW] ' : ''}</span>
            kronos {gateDec.model_side} {Math.round(gateDec.confidence * 100)}%
            {' → '}
            <span style={{ color: VERDICT_TONE[gateDec.verdict] ?? T.ink2 }}>
              {gateDec.shadow ? `would ${gateDec.verdict}` : gateDec.verdict}
            </span>
            {gateDec.data_age_min != null && (
              <span style={{ color: gateDec.stale ? T.red : T.ink3 }}> · data {gateDec.data_age_min}m</span>
            )}
          </span>
        ) : (
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>no gate decision yet</span>
        )}
      </div>
    </div>
  )
}

function ORBCockpit({ data }) {
  const strategies = data.trader?.strategies ?? []
  const maxEntries = data.limits?.max_orders_per_session ?? 4
  const gateBySid  = {}
  ;(data.gate?.data?.decisions ?? []).forEach(d => {
    if (!gateBySid[d.security_id]) gateBySid[d.security_id] = d   // newest first
  })

  return (
    <section style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={{ fontFamily: T.mono, fontSize: 11, letterSpacing: '0.22em', color: T.ink0 }}>
          ORB COCKPIT
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {strategies.length} runners · opening range vs price
        </span>
      </div>
      {strategies.length === 0 ? (
        <div style={{ padding: 20, border: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          {data.alive ? 'No runners — screener returned 0 securities' : 'Engine offline — no live strategy state'}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(290px, 1fr))', gap: 12 }}>
          {strategies.map(s => (
            <ORBCard key={s.security_id} s={s} gateDec={gateBySid[s.security_id]} maxEntries={maxEntries} />
          ))}
        </div>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// Kronos gate panel — today's verdicts + the calibration countdown.
// ─────────────────────────────────────────────────────────────────
function GatePanel({ gate }) {
  const decisions = gate?.data?.decisions ?? []
  const cal       = gate?.data?.calibration

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.amber, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          KRONOS GATE
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {decisions.length} decisions today
        </span>
      </div>

      {cal && (
        <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
          fontFamily: T.mono, fontSize: 9, color: T.ink2, lineHeight: 1.7 }}>
          <span style={{ color: T.ink3, letterSpacing: '0.12em' }}>CALIBRATION · </span>
          {cal.recommendation}
          {cal.fresh_n != null && (
            <span style={{ color: T.ink3 }}> ({cal.fresh_n} fresh outcomes{cal.fresh_accuracy != null ? `, acc ${cal.fresh_accuracy}` : ''})</span>
          )}
        </div>
      )}

      {decisions.length === 0 ? (
        <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          No gate decisions today — they fire on ORB breakouts
        </div>
      ) : (
        <div style={{ maxHeight: 220, overflowY: 'auto' }}>
          {decisions.map((d, i) => (
            <div key={i} style={{
              display: 'grid', gridTemplateColumns: '52px 70px 44px 1fr 60px', gap: 8,
              padding: '6px 16px', alignItems: 'center',
              background: i % 2 === 0 ? T.bg1 : T.bg2,
              borderLeft: `2px solid ${VERDICT_TONE[d.verdict] ?? T.line}`,
            }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{fmtTime(d.ts)}</span>
              <span style={{ fontFamily: T.mono, fontSize: 10, fontWeight: 700, color: T.ink0 }}>{d.ticker}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9,
                color: d.requested_direction === 'BUY' ? T.green : T.red }}>{d.requested_direction}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>
                model {d.model_side} {Math.round(d.confidence * 100)}%
                {d.data_age_min != null && <span style={{ color: d.stale ? T.red : T.ink3 }}> · {d.data_age_min}m</span>}
              </span>
              <span style={{ fontFamily: T.mono, fontSize: 9, textAlign: 'right',
                color: VERDICT_TONE[d.verdict] ?? T.ink3 }}>
                {d.shadow ? `~${d.verdict}` : d.verdict}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TodayPnlCard({ risk, limits }) {
  const rpnl  = risk?.data?.realised_pnl   ?? 0
  const upnl  = risk?.data?.unrealised_pnl ?? 0
  const total = risk?.data?.total_pnl      ?? 0
  const limit = limits?.max_daily_loss ?? 5000
  const losspct = Math.min(Math.abs(Math.min(total, 0)) / limit * 100, 100)

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>
        Today P&L
      </div>
      <div style={{ fontFamily: T.dot, fontSize: 36, color: colorPnl(total), lineHeight: 1, marginBottom: 12 }}>
        {total >= 0 ? '+' : ''}₹{Math.round(total).toLocaleString('en-IN')}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
        {[['REALISED', rpnl], ['UNREALISED', upnl]].map(([l, v]) => (
          <div key={l} style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 10px' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.14em', marginBottom: 3 }}>{l}</div>
            <div style={{ fontFamily: T.dot, fontSize: 20, color: colorPnl(v) }}>
              {v >= 0 ? '+' : ''}₹{Math.abs(Math.round(v)).toLocaleString('en-IN')}
            </div>
          </div>
        ))}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 4 }}>
        DAILY LOSS LIMIT · ₹{Number(limit).toLocaleString('en-IN')}
      </div>
      <div style={{ height: 4, background: T.bg3, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${losspct}%`,
          background: `linear-gradient(90deg, ${T.green}, ${T.amber} 60%, ${T.red})`,
          transition: 'width 0.5s' }} />
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: losspct > 75 ? T.red : T.ink3, marginTop: 4 }}>
        {losspct.toFixed(1)}% consumed
      </div>
    </div>
  )
}

function ExecutionsFeed({ signals }) {
  const raw  = signals?.data
  const feed = (Array.isArray(raw) ? raw : []).slice(0, 20)
  const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber, HOLD: T.ink3 }

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.amber,
          boxShadow: `0 0 6px ${T.amber}` }} />
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          Executions — today
        </span>
      </div>
      <div style={{ maxHeight: 260, overflowY: 'auto' }}>
        {feed.length === 0
          ? <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
              No executions today — entries appear here when ORB fires
            </div>
          : feed.map((s, i) => (
            <div key={i} style={{
              display: 'grid', gridTemplateColumns: '50px 40px 70px 1fr',
              gap: 8, padding: '5px 16px',
              background: i % 2 === 0 ? T.bg1 : T.bg2,
              borderLeft: `2px solid ${ACTION_COLOR[s.action] ?? T.line}`,
            }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{fmtTime(s.timestamp)}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, fontWeight: 700, color: ACTION_COLOR[s.action] ?? T.ink1 }}>{s.action}</span>
              {s.price ? <span style={{ fontFamily: T.dot, fontSize: 15, color: T.ink0 }}>₹{s.price}</span> : <span />}
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.source} · {s.reason}</span>
            </div>
          ))
        }
      </div>
    </div>
  )
}

function SignalsTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 60px' }}>
      <SessionBar status={data.status} />
      <ORBCockpit data={data} />

      {/* Left: what happened today (gate verdicts + executions — same clock).
          Right: P&L + screener context. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <GatePanel gate={data.gate} />
          <ExecutionsFeed signals={data.signals} />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <TodayPnlCard risk={data.risk} limits={data.limits} />
          <ScreenedToday kronosLive={data.kronosLive} />
        </div>
      </div>

      <ActionWatchlist
        positions={data.positions}
        paperPositions={data.paperPositions}
        tradelog={data.tradelog}
        kronosSignals={data.kronosSignals}
      />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Portfolio tab
// ─────────────────────────────────────────────────────────────────
function PnlCurve({ equity }) {
  const pts = equity?.data?.intraday ?? []
  const last = pts[pts.length - 1]
  const color = (last?.pnl ?? 0) >= 0 ? T.green : T.red
  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em',
          textTransform: 'uppercase' }}>Intraday P&L</span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {pts.length ? `${pts.length} min · 10s risk snapshots` : ''}
        </span>
      </div>
      {pts.length < 2 ? (
        <div style={{ height: 180, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          No session data yet — curve starts at 09:15 IST
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={180}>
          <LineChart data={pts} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="t" tick={{ fontFamily: T.mono, fontSize: 9, fill: T.ink3 }}
              tickLine={false} axisLine={{ stroke: T.line }} minTickGap={50} />
            <YAxis tick={{ fontFamily: T.mono, fontSize: 9, fill: T.ink3 }}
              tickLine={false} axisLine={false} width={54}
              tickFormatter={v => '₹' + Number(v).toLocaleString('en-IN')} />
            <Tooltip contentStyle={{ background: T.bg2, border: `1px solid ${T.line2}`,
              fontFamily: T.mono, fontSize: 10 }}
              labelStyle={{ color: T.ink2 }} formatter={v => ['₹' + Number(v).toLocaleString('en-IN'), 'P&L']} />
            <Line type="stepAfter" dataKey="pnl" stroke={color} strokeWidth={1.5}
              dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

function PortfolioHero({ data }) {
  const risk  = data.trader?.risk ?? {}
  const total = risk.total_pnl ?? 0
  const rpnl  = risk.realised_pnl ?? 0
  const upnl  = risk.unrealised_pnl ?? 0
  const limit = data.limits?.max_daily_loss ?? 5000
  const losspct = Math.min(Math.abs(Math.min(total, 0)) / limit * 100, 100)
  const f     = data.funds?.data?.data ?? {}
  const todays = data.tradelog?.data?.summary ?? {}

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))',
      gap: 12, marginBottom: 16 }}>
      {/* Day P&L + loss meter */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}`,
        borderTop: `2px solid ${colorPnl(total)}`, padding: '14px 16px' }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em',
          textTransform: 'uppercase', marginBottom: 8 }}>Today P&L · {data.trader?.mode ?? '—'}</div>
        <div style={{ fontFamily: T.dot, fontSize: 40, color: colorPnl(total), lineHeight: 1 }}>
          {total >= 0 ? '+' : ''}{INR0(total)}
        </div>
        <div style={{ display: 'flex', gap: 14, margin: '8px 0 10px' }}>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: colorPnl(rpnl) }}>
            R {rpnl >= 0 ? '+' : ''}{INR0(rpnl)}</span>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: colorPnl(upnl) }}>
            U {upnl >= 0 ? '+' : ''}{INR0(upnl)}</span>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
            {todays.closed_today ?? 0} closed · {todays.wins_today ?? 0} wins</span>
        </div>
        <div style={{ height: 5, background: T.bg3 }}>
          <div style={{ height: '100%', width: `${losspct}%`,
            background: `linear-gradient(90deg, ${T.green}, ${T.amber} 60%, ${T.red})`,
            transition: 'width 0.5s' }} />
        </div>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: losspct > 75 ? T.red : T.ink3, marginTop: 4 }}>
          loss limit {losspct.toFixed(0)}% of ₹{Number(limit).toLocaleString('en-IN')}
          {risk.halted && <span style={{ color: T.red, fontWeight: 700 }}>  ⛔ HALTED</span>}
        </div>
      </div>

      {/* Account */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: '14px 16px' }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em',
          textTransform: 'uppercase', marginBottom: 8 }}>Broker account</div>
        {[['Available', f.availabelBalance, T.green],
          ['SOD limit', f.sodLimit, T.ink1],
          ['Deployed', f.utilizedAmount, T.amber]].map(([l, v, c]) => (
          <div key={l} style={{ display: 'flex', justifyContent: 'space-between',
            alignItems: 'baseline', padding: '5px 0', borderBottom: `1px solid ${T.line}` }}>
            <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3,
              letterSpacing: '0.12em', textTransform: 'uppercase' }}>{l}</span>
            <span style={{ fontFamily: T.dot, fontSize: 20, color: c }}>
              ₹{Number(v ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
          </div>
        ))}
        <div style={{ fontFamily: T.mono, fontSize: 8.5, color: T.ink3, marginTop: 8 }}>
          paper equity ₹{Number(data.limits?.paper_balance ?? 0).toLocaleString('en-IN')} ·
          risk/trade {((data.limits?.max_daily_loss ?? 0) > 0 ? '1%' : '—')}
        </div>
      </div>

      {/* Open positions */}
      <OpenPositionsCard data={data} />
    </div>
  )
}

function OpenPositionsCard({ data }) {
  const positions = data.trader?.portfolio?.open_positions ?? []
  const priceBySid = {}
  ;(data.trader?.strategies ?? []).forEach(s => { priceBySid[s.security_id] = s.last_price })

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: '14px 16px' }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em',
        textTransform: 'uppercase', marginBottom: 8 }}>
        Open positions ({positions.length})
      </div>
      {positions.length === 0 ? (
        <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, padding: '14px 0' }}>
          Flat — no exposure
        </div>
      ) : positions.map((p, i) => {
        const ltp  = priceBySid[p.security_id] || 0
        const upnl = ltp ? (ltp - p.avg_price) * p.qty : null
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10,
            padding: '7px 0', borderBottom: `1px solid ${T.line}` }}>
            <span style={{ fontFamily: T.mono, fontSize: 11, fontWeight: 700, color: T.ink0,
              minWidth: 56 }}>{p.ticker ?? p.security_id}</span>
            <span style={{ fontFamily: T.mono, fontSize: 9,
              color: p.qty > 0 ? T.green : T.red }}>{p.qty > 0 ? 'LONG' : 'SHORT'}</span>
            <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>
              {Math.abs(p.qty)} @ ₹{Number(p.avg_price).toFixed(2)}</span>
            <span style={{ marginLeft: 'auto', fontFamily: T.dot, fontSize: 18,
              color: upnl == null ? T.ink3 : colorPnl(upnl) }}>
              {upnl == null ? '—' : (upnl >= 0 ? '+' : '') + INR0(upnl)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function TradeTable({ tradelog }) {
  const trades = (tradelog?.data?.trades ?? [])
    .filter(t => t.status === 'CLOSED')
    .slice(0, 30)

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em' }}>
        TRADE HISTORY
      </div>
      {trades.length === 0 ? (
        <div style={{ padding: '20px 14px', fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No closed trades yet</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {['TIME','SYMBOL','ACTION','PRICE','P&L','STRATEGY'].map(h => (
                  <th key={h} style={{ textAlign: 'left', fontFamily: T.mono, fontSize: 8, color: T.ink3, padding: '6px 10px', borderBottom: `1px solid ${T.line}`, letterSpacing: '0.15em', whiteSpace: 'nowrap' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${T.line}` }}>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{fmtTime(t.exit_ts)}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 10, color: T.ink0, fontWeight: 700 }}>{t.symbol}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 9, color: t.action === 'EXIT' ? T.amber : T.ink1 }}>{t.action}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.dot, fontSize: 15, color: T.ink0 }}>₹{t.entry_price} → ₹{t.exit_price}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.dot, fontSize: 15, color: colorPnl(t.pnl ?? 0) }}>
                    {(t.pnl ?? 0) >= 0 ? '+' : ''}{INR0(t.pnl ?? 0)}
                  </td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{t.strategy ?? 'ORB'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PortfolioMetrics({ tradelog, risk }) {
  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null)
  const wins   = exits.filter(t => (t.pnl ?? 0) > 0)
  const losses = exits.filter(t => (t.pnl ?? 0) <= 0)
  const winRate  = exits.length ? ((wins.length / exits.length) * 100).toFixed(0) : '—'
  const avgWin   = wins.length   ? wins.reduce((s, t) => s + t.pnl, 0) / wins.length : 0
  const avgLoss  = losses.length ? losses.reduce((s, t) => s + t.pnl, 0) / losses.length : 0
  const profitFactor = avgLoss !== 0 ? Math.abs(avgWin / avgLoss).toFixed(2) : '—'
  const totalPnl = exits.reduce((s, t) => s + (t.pnl ?? 0), 0)

  // Simple drawdown calc
  let peak = 0, eq = 0, maxDD = 0
  exits.forEach(t => {
    eq += t.pnl ?? 0
    if (eq > peak) peak = eq
    const dd = peak > 0 ? (peak - eq) / peak * 100 : 0
    if (dd > maxDD) maxDD = dd
  })

  const metrics = [
    ['TRADES',        exits.length || '—',           T.ink0],
    ['WIN RATE',      exits.length ? winRate + '%' : '—', wins.length >= exits.length * 0.5 ? T.green : T.amber],
    ['PROFIT FACTOR', profitFactor,                  parseFloat(profitFactor) >= 1.5 ? T.green : T.amber],
    ['MAX DRAWDOWN',  maxDD > 0 ? maxDD.toFixed(1) + '%' : '—', maxDD > 20 ? T.red : T.amber],
    ['AVG WIN',       avgWin > 0 ? '₹' + Math.round(avgWin).toLocaleString('en-IN') : '—', T.green],
    ['AVG LOSS',      avgLoss < 0 ? '₹' + Math.round(Math.abs(avgLoss)).toLocaleString('en-IN') : '—', T.red],
  ]

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 8, marginBottom: 16 }}>
      {metrics.map(([label, val, color]) => (
        <div key={label} style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: '12px 14px' }}>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 6 }}>{label}</div>
          <div style={{ fontFamily: T.dot, fontSize: 24, color, lineHeight: 1 }}>{val}</div>
        </div>
      ))}
    </div>
  )
}

function CalendarPnL({ tradelog }) {
  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null && t.exit_ts)

  // Group by date
  const byDate = {}
  exits.forEach(t => {
    const d = t.exit_ts?.slice(0, 10)
    if (d) byDate[d] = (byDate[d] ?? 0) + (t.pnl ?? 0)
  })

  const dates = Object.keys(byDate).sort()
  if (dates.length === 0) return null

  const maxAbs = Math.max(...Object.values(byDate).map(Math.abs), 1)

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16, marginBottom: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>
        DAILY P&L CALENDAR
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {dates.map(d => {
          const pnl  = byDate[d]
          const intensity = Math.abs(pnl) / maxAbs
          const bg   = pnl > 0
            ? `oklch(${0.3 + intensity * 0.3} 0.19 145)`
            : `oklch(${0.3 + intensity * 0.2} 0.22 25)`
          return (
            <div key={d} title={`${d}: ₹${Math.round(pnl)}`} style={{
              width: 28, height: 28, background: bg,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontFamily: T.mono, fontSize: 7, color: 'rgba(255,255,255,0.7)',
              cursor: 'default',
            }}>
              {new Date(d + 'T00:00').getDate()}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PortfolioTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 60px' }}>
      <PortfolioHero data={data} />

      <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr', gap: 16, marginBottom: 16 }}>
        <PnlCurve equity={data.equity} />
        <CalendarPnL tradelog={data.tradelog} />
      </div>

      <PortfolioMetrics tradelog={data.tradelog} risk={data.risk} />
      <TradeTable tradelog={data.tradelog} />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────
// System tab — full-width 3-column grid
// ─────────────────────────────────────────────────────────────────

const LEVEL_COLOR = { WARNING: T.amber, ERROR: T.red, CRITICAL: T.red, INFO: T.ink2, DEBUG: T.ink3 }

function SysLabel({ children }) {
  return (
    <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3,
      letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 10 }}>
      {children}
    </div>
  )
}

function SysPanel({ title, right, accent, children }) {
  return (
    <div style={{ background: T.bg1, border: `1px solid ${accent ?? T.line}` }}>
      <div style={{
        padding: '10px 16px', borderBottom: `1px solid ${accent ?? T.line}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: accent ? `${accent}0a` : 'transparent',
      }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: accent ?? T.ink3,
          letterSpacing: '0.18em', textTransform: 'uppercase' }}>{title}</span>
        {right && <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{right}</span>}
      </div>
      <div style={{ padding: 16 }}>{children}</div>
    </div>
  )
}

function BigStat({ label, value, unit, color, sub }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 3 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
        <span style={{ fontFamily: T.dot, fontSize: 28, color: color ?? T.ink0, lineHeight: 1 }}>{value}</span>
        {unit && <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>{unit}</span>}
      </div>
      {sub && <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginTop: 3 }}>{sub}</div>}
    </div>
  )
}

function ServiceCard({ label, ok, value, sub, warn }) {
  const color = ok == null ? T.ink3 : ok ? T.green : T.red
  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`,
      borderTop: `2px solid ${warn ? T.amber : color}`, padding: '12px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 8 }}>
        <div style={{ width: 7, height: 7, borderRadius: '50%', background: warn ? T.amber : color,
          boxShadow: ok || warn ? `0 0 7px ${warn ? T.amber : color}` : 'none' }} />
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em',
          textTransform: 'uppercase' }}>{label}</span>
      </div>
      <div style={{ fontFamily: T.dot, fontSize: 26, color: T.ink0, lineHeight: 1, marginBottom: 5 }}>{value}</div>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{sub}</div>
    </div>
  )
}

function ServicesRow({ data }) {
  const t = data.trader
  const db = data.dbStats?.data
  const bf = data.backfill?.data
  const ck = bf?.checkpoint ?? {}
  const bars = t?.bars ?? {}
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
      gap: 12, marginBottom: 16 }}>
      <ServiceCard label="Engine" ok={data.alive}
        value={data.alive ? fmtUptime(t?.uptime_seconds ?? 0) : 'DOWN'}
        sub={data.alive ? `${t?.strategies?.length ?? 0} runners · ${t?.mode}` : 'heartbeat stale'} />
      <ServiceCard label="Market data" ok={t?.feed?.connected}
        value={t?.feed?.connected ? `${t?.feed?.subscribed ?? 0} WS` : 'OFFLINE'}
        sub={`bars written ${bars.bars_written ?? 0} · pending ${bars.pending ?? 0}`} />
      <ServiceCard label="TimescaleDB" ok={db?.up}
        value={db?.up ? `${db.ping_ms}ms` : db ? 'DOWN' : '…'}
        sub={db?.up ? `${db.db_size} · schema ${db.alembic}` : (db?.error ?? 'waiting for first poll').slice(0, 48)} />
      <ServiceCard label="Backfill" ok={bf?.running} warn={bf && !bf.running && (ck.pct ?? 0) < 100}
        value={ck.pct != null ? `${ck.pct}%` : '—'}
        sub={ck.index != null ? `${ck.index}/${ck.total} NSE_EQ · ${bf?.running ? 'running' : 'stopped'}` : 'no checkpoint'} />
    </div>
  )
}

function DBPanel({ dbStats }) {
  const d = dbStats?.data
  if (!d) return (
    <SysPanel title="TimescaleDB" right="10.0.1.155:5432">
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>Waiting for first poll…</div>
    </SysPanel>
  )
  if (!d.up) return (
    <SysPanel title="TimescaleDB" right="10.0.1.155:5432" accent={T.red}>
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.red }}>UNREACHABLE — {(d.error ?? '').slice(0, 120)}</div>
    </SysPanel>
  )
  const fmtRows = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(0) + 'K' : String(n)
  return (
    <SysPanel title="TimescaleDB" right={`${d.db_size} · ping ${d.ping_ms}ms`} accent={T.cyan}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 64px 70px 70px', gap: 6,
        fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.1em', marginBottom: 6 }}>
        <span>HYPERTABLE</span><span>SIZE</span><span>~ROWS</span><span>COMPRESSED</span>
      </div>
      {(d.hypertables ?? []).filter(h => h.approx_rows > 0 || h.chunks_total > 0).map(h => (
        <div key={h.name} style={{ display: 'grid', gridTemplateColumns: '1fr 64px 70px 70px', gap: 6,
          padding: '5px 0', borderBottom: `1px solid ${T.line}`,
          fontFamily: T.mono, fontSize: 10 }}>
          <span style={{ color: T.ink0, fontWeight: 700 }}>{h.name}</span>
          <span style={{ color: T.cyan }}>{h.size}</span>
          <span style={{ color: T.ink1 }}>{fmtRows(h.approx_rows)}</span>
          <span style={{ color: h.chunks_compressed === h.chunks_total ? T.green : T.amber }}>
            {h.chunks_compressed}/{h.chunks_total}
          </span>
        </div>
      ))}
      <div style={{ marginTop: 10, fontFamily: T.mono, fontSize: 9, color: T.ink3, lineHeight: 1.8 }}>
        bars span {d.bars_span?.earliest} → {d.bars_span?.latest}<br />
        instruments {Object.values(d.instruments ?? {}).reduce((a, b) => a + b, 0).toLocaleString('en-IN')} ·
        signals {d.signals} · trades {d.trades}
      </div>
    </SysPanel>
  )
}

function BackfillPanel({ backfill }) {
  const d = backfill?.data
  const ck = d?.checkpoint ?? {}
  const pct = ck.pct ?? 0
  const tail = (d?.log_tail ?? []).slice(-8)
  return (
    <SysPanel title="Backfill — NSE_EQ raw layer"
      right={d ? (d.running ? '◉ RUNNING' : '■ STOPPED') : '…'}
      accent={d?.running ? T.green : T.amber}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 6 }}>
        <span style={{ fontFamily: T.dot, fontSize: 30, color: T.ink0 }}>{pct}%</span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          {ck.index ?? '—'}/{ck.total ?? '—'} securities · last {ck.last_id ?? '—'}
        </span>
      </div>
      <div style={{ height: 5, background: T.bg3, marginBottom: 12 }}>
        <div style={{ height: '100%', width: `${pct}%`, background: T.green, transition: 'width 1s' }} />
      </div>
      <div style={{ background: T.bg0, border: `1px solid ${T.line}`, padding: '8px 10px',
        maxHeight: 150, overflowY: 'auto' }}>
        {tail.length === 0
          ? <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>no log output</span>
          : tail.map((l, i) => (
            <div key={i} style={{ fontFamily: T.mono, fontSize: 8.5, color: T.ink2,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', lineHeight: 1.7 }}>{l}</div>
          ))}
      </div>
    </SysPanel>
  )
}

function AutomationPanel({ systemHealth }) {
  const d = systemHealth?.data
  return (
    <SysPanel title="Automation & alerts"
      right={d?.telegram_configured ? 'telegram ✓' : 'telegram NOT SET'}
      accent={d?.telegram_configured ? T.green : T.red}>
      {!d ? (
        <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>Loading…</div>
      ) : (
        <>
          <SysLabel>SCHEDULED (plain cron — ₹0/mo)</SysLabel>
          {(d.crons ?? []).map((c, i) => (
            <div key={i} style={{ display: 'flex', gap: 10, padding: '4px 0',
              borderBottom: `1px solid ${T.line}` }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.cyan, minWidth: 95 }}>{c.schedule}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink1 }}>{c.job}</span>
            </div>
          ))}
          <div style={{ marginTop: 12 }}>
            <SysLabel>TRADER ERR/CRIT TODAY</SysLabel>
            <span style={{ fontFamily: T.dot, fontSize: 24,
              color: d.trader_errors_today > 0 ? T.amber : T.green }}>{d.trader_errors_today}</span>
          </div>
          <div style={{ marginTop: 12, fontFamily: T.mono, fontSize: 8.5, color: T.ink3, lineHeight: 1.6 }}>
            {d.hermes}
          </div>
        </>
      )}
    </SysPanel>
  )
}

function FullLogPanel({ logs }) {
  const all  = logs?.data?.logs ?? []
  const rows = all.slice(-50)

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{
        padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          SYSTEM LOG
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{rows.length} lines</span>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0', minHeight: 0 }}>
        {rows.length === 0
          ? <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No log output</div>
          : rows.map((l, i) => (
            <div key={i} style={{
              display: 'grid',
              gridTemplateColumns: '58px 14px 70px 1fr',
              gap: 8,
              padding: '2px 16px',
              fontFamily: T.mono, fontSize: 10,
              background: l.level === 'ERROR' || l.level === 'CRITICAL' ? `${T.red}08` :
                          l.level === 'WARNING' ? `${T.amber}06` : 'transparent',
              borderLeft: `2px solid ${LEVEL_COLOR[l.level] ?? 'transparent'}`,
            }}>
              <span style={{ color: T.ink3, whiteSpace: 'nowrap' }}>{fmtTime(l.ts)}</span>
              <span style={{ color: LEVEL_COLOR[l.level] ?? T.ink3 }}>{l.icon}</span>
              <span style={{ color: T.ink3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.name}</span>
              <span style={{ color: LEVEL_COLOR[l.level] ?? T.ink2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.msg}</span>
            </div>
          ))
        }
      </div>
    </div>
  )
}

function SystemTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 40px' }}>
      <ServicesRow data={data} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16, marginBottom: 16 }}>
        <DBPanel       dbStats={data.dbStats} />
        <BackfillPanel backfill={data.backfill} />
        <AutomationPanel systemHealth={data.systemHealth} />
      </div>
      <div style={{ height: 420 }}>
        <FullLogPanel logs={data.logs} />
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Root
// ─────────────────────────────────────────────────────────────────
export default function App() {
  const data  = useDashboardData()
  const [tab, setTab] = useState('Signals')

  return (
    <ErrorBoundary>
      <div style={{ background: T.bg0, minHeight: '100vh', color: T.ink0 }}>
        <style>{`
          * { box-sizing: border-box; margin: 0; padding: 0; }
          body { background: ${T.bg0}; }
          @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
          ::-webkit-scrollbar { width: 4px; height: 4px; }
          ::-webkit-scrollbar-track { background: ${T.bg1}; }
          ::-webkit-scrollbar-thumb { background: ${T.line2}; }
        `}</style>

        <FloatingKillSwitch onKill={() => {}} />
        <StatusSpine data={data} />
        <OfflineBanner data={data} />
        <TabBar active={tab} onChange={setTab} />

        <ErrorBoundary>
          {tab === 'Signals'   && <SignalsTab   data={data} />}
          {tab === 'Portfolio' && <PortfolioTab data={data} />}
          {tab === 'System'    && <SystemTab    data={data} />}
        </ErrorBoundary>
      </div>
    </ErrorBoundary>
  )
}
