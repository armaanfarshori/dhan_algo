import { useState, useEffect, Component } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { T, INR, INR0, colorPnl, fmtTime } from './tokens'
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
function Header({ status, risk, funds }) {
  const mode     = status?.data?.paper_trading !== false ? 'PAPER' : 'LIVE'
  const halted   = risk?.data?.halted ?? false
  const pnl      = risk?.data?.total_pnl ?? 0
  const agentUp  = !status?.loading && !status?.error
  const balance  = funds?.data?.data?.availabelBalance ?? 0

  return (
    <header style={{
      position: 'sticky', top: 0, zIndex: 100,
      background: T.bg0,
      borderBottom: `1px solid ${T.line}`,
      display: 'flex', alignItems: 'center', gap: 20,
      padding: '0 24px', height: 44,
    }}>
      {/* Logo */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginRight: 8 }}>
        <span style={{ fontFamily: T.mono, fontSize: 13, fontWeight: 700, color: T.ink0, letterSpacing: '0.1em' }}>
          DHAN<span style={{ color: T.cyan }}>AI</span>
        </span>
      </div>

      {/* Mode badge */}
      <span style={{
        fontFamily: T.mono, fontSize: 9, letterSpacing: '0.2em',
        padding: '3px 8px',
        background: mode === 'LIVE' ? T.redD : T.bg3,
        color:      mode === 'LIVE' ? T.red   : T.ink2,
        border: `1px solid ${mode === 'LIVE' ? T.red : T.line}`,
      }}>
        {mode}
      </span>

      {/* Agent pulse */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={{
          width: 6, height: 6, borderRadius: '50%',
          background: agentUp ? T.green : T.ink3,
          boxShadow: agentUp ? `0 0 8px ${T.green}` : 'none',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
          {agentUp ? 'AGENT' : 'OFFLINE'}
        </span>
      </div>

      <div style={{ flex: 1 }} />

      {/* Balance */}
      <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
        {INR(balance)}
      </span>

      {/* Today PnL */}
      <span style={{
        fontFamily: T.dot, fontSize: 20,
        color: colorPnl(pnl),
        minWidth: 100, textAlign: 'right',
      }}>
        {pnl >= 0 ? '+' : ''}{INR0(pnl)}
      </span>

      {halted && (
        <span style={{
          fontFamily: T.mono, fontSize: 9, letterSpacing: '0.2em',
          padding: '3px 8px', background: T.redD, color: T.red,
          border: `1px solid ${T.red}`, animation: 'pulse 1s ease-in-out infinite',
        }}>⛔ HALTED</span>
      )}

      <ClockIST />
    </header>
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

function SignalCard({ sig, atr }) {
  const cfg  = SIDE_CFG[sig.side] ?? SIDE_CFG.HOLD
  const conf = Math.round((sig.confidence ?? 0) * 100)
  const ret  = (sig.features?.forecast_return ?? 0) * 100
  const atrPct = atr ? (parseFloat(atr.replace('ATR%=', '')) ?? null) : null

  return (
    <div style={{
      background: T.bg1,
      border: `1px solid ${T.line}`,
      borderLeft: `3px solid ${cfg.border}`,
      padding: 16,
      display: 'flex', flexDirection: 'column', gap: 8,
      position: 'relative', overflow: 'hidden',
    }}>
      {/* Security ID + badge */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontFamily: T.mono, fontSize: 13, fontWeight: 700, color: T.ink0 }}>
          {sig.security_id}
        </span>
        <span style={{
          fontFamily: T.mono, fontSize: 9, letterSpacing: '0.15em',
          padding: '2px 7px',
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

      {/* Subtle confidence glow behind card */}
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
function KronosBoard({ kronosSignals, screener }) {
  const raw        = kronosSignals?.data?.signals ?? []
  const candidates = screener?.data?.candidates ?? []

  // Deduplicate: keep latest per security
  const seen = new Map()
  for (const s of [...raw].sort((a,b) => b.ts?.localeCompare(a.ts ?? '') ?? 0)) {
    if (!seen.has(s.security_id)) seen.set(s.security_id, s)
  }

  // Sort: BUY > SELL > HOLD, then by confidence desc
  const ORDER = { BUY: 0, SELL: 1, HOLD: 2 }
  const sorted = [...seen.values()].sort((a, b) =>
    (ORDER[a.side] ?? 3) - (ORDER[b.side] ?? 3) || (b.confidence ?? 0) - (a.confidence ?? 0)
  )

  const lastTs = raw[0]?.ts ?? null
  const atrMap = Object.fromEntries(candidates.map(c => [c.security_id, c.reason]))

  return (
    <section>
      {/* Section header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        marginBottom: 14,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            width: 8, height: 8, borderRadius: '50%',
            background: sorted.length ? T.cyan : T.ink3,
            boxShadow: sorted.length ? `0 0 10px ${T.cyan}` : 'none',
          }} />
          <span style={{ fontFamily: T.mono, fontSize: 11, letterSpacing: '0.22em', color: T.ink0 }}>
            KRONOS AI
          </span>
        </div>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
          AAAI 2026 · OHLCV FOUNDATION MODEL
        </span>
        {lastTs && (
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginLeft: 'auto' }}>
            LAST RUN {fmtTime(lastTs)}
          </span>
        )}
      </div>

      {sorted.length === 0 ? (
        <div style={{
          border: `1px dashed ${T.line2}`,
          padding: '40px 24px',
          textAlign: 'center',
          fontFamily: T.mono, fontSize: 11, color: T.ink3,
          letterSpacing: '0.1em',
        }}>
          NO SIGNALS — RUN KRONOS FORECAST
          <div style={{ fontSize: 10, marginTop: 8, color: T.ink3 }}>
            hermes_skills/dhan/kronos_forecast/scripts/forecast.py
          </div>
        </div>
      ) : (
        <>
          {/* Signal summary strip */}
          <div style={{ display: 'flex', gap: 20, marginBottom: 14 }}>
            {[
              ['BUY',  sorted.filter(s => s.side==='BUY').length,  T.green],
              ['SELL', sorted.filter(s => s.side==='SELL').length, T.red],
              ['HOLD', sorted.filter(s => s.side==='HOLD').length, T.ink3],
            ].map(([label, count, color]) => (
              <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontFamily: T.dot, fontSize: 22, color }}>{count}</span>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{label}</span>
              </div>
            ))}
            <span style={{ marginLeft: 'auto', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
              {sorted.length} SECURITIES SCORED
            </span>
          </div>

          {/* Cards grid */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
            gap: 8,
          }}>
            {sorted.map(sig => (
              <SignalCard key={sig.security_id} sig={sig} atr={atrMap[sig.security_id]} />
            ))}
          </div>
        </>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// Live signal feed (real-time strategy signals)
// ─────────────────────────────────────────────────────────────────
function LiveFeed({ signals }) {
  // signals API returns a bare list, usePoller wraps it: {data: [...], loading, error}
  const raw  = signals?.data
  const rows = (Array.isArray(raw) ? raw : []).slice(0, 12)
  const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber, HOLD: T.ink3 }

  return (
    <section style={{ marginTop: 28 }}>
      <div style={{
        fontFamily: T.mono, fontSize: 9, letterSpacing: '0.22em',
        color: T.ink3, marginBottom: 10,
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.amber, boxShadow: `0 0 6px ${T.amber}` }} />
        LIVE SIGNAL FEED
      </div>
      {rows.length === 0 ? (
        <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, padding: '8px 0' }}>
          Waiting for first signal…
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {rows.map((s, i) => (
            <div key={i} style={{
              display: 'flex', gap: 16, alignItems: 'center',
              padding: '5px 10px',
              background: i % 2 === 0 ? T.bg1 : 'transparent',
              borderLeft: `2px solid ${ACTION_COLOR[s.action] ?? T.line}`,
            }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, minWidth: 55 }}>
                {fmtTime(s.timestamp)}
              </span>
              <span style={{ fontFamily: T.mono, fontSize: 10, fontWeight: 700, color: ACTION_COLOR[s.action] ?? T.ink1, minWidth: 36 }}>
                {s.action}
              </span>
              {s.price && (
                <span style={{ fontFamily: T.dot, fontSize: 16, color: T.ink0, minWidth: 70 }}>
                  ₹{s.price}
                </span>
              )}
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {s.reason}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// Positions strip
// ─────────────────────────────────────────────────────────────────
function PositionStrip({ positions, paperPositions, risk }) {
  // positions API: {ok, data:[...]}  → need .data.data
  // paperPositions API: {ok, count, data:{data:[...]}} → need .data.data
  const live  = (positions?.data?.data ?? []).filter(p => p.netQty !== 0)
  const paper = (paperPositions?.data?.data?.data ?? paperPositions?.data?.data ?? []).filter(p => p.in_position)
  const open  = live.length > 0 ? live : paper
  const rpnl  = risk?.data?.realised_pnl   ?? 0
  const upnl  = risk?.data?.unrealised_pnl ?? 0
  const total = risk?.data?.total_pnl      ?? 0

  return (
    <section style={{ marginTop: 28 }}>
      {/* PnL numbers */}
      <div style={{ display: 'flex', gap: 32, alignItems: 'flex-end', marginBottom: 16 }}>
        <div>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 4 }}>TODAY P&L</div>
          <div style={{ fontFamily: T.dot, fontSize: 38, color: colorPnl(total), lineHeight: 1 }}>
            {total >= 0 ? '+' : ''}{INR0(total)}
          </div>
        </div>
        <div>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 4 }}>REALISED</div>
          <div style={{ fontFamily: T.dot, fontSize: 24, color: colorPnl(rpnl) }}>
            {rpnl >= 0 ? '+' : ''}{INR0(rpnl)}
          </div>
        </div>
        <div>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 4 }}>UNREALISED</div>
          <div style={{ fontFamily: T.dot, fontSize: 24, color: colorPnl(upnl) }}>
            {upnl >= 0 ? '+' : ''}{INR0(upnl)}
          </div>
        </div>
      </div>

      {/* Open positions */}
      <div style={{ fontFamily: T.mono, fontSize: 9, letterSpacing: '0.18em', color: T.ink3, marginBottom: 8 }}>
        OPEN POSITIONS ({open.length})
      </div>
      {open.length === 0 ? (
        <div style={{
          fontFamily: T.mono, fontSize: 10, color: T.ink3,
          padding: 16,
          border: `1px solid ${T.line}`,
          borderLeft: `2px solid ${T.line2}`,
        }}>
          No open positions
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {open.map((p, i) => {
            const sid    = p.securityId ?? p.security_id ?? '—'
            const qty    = p.netQty ?? p.qty ?? 0
            const entry  = p.buyAvg ?? p.entry_price ?? 0
            const upnl   = p.unrealisedProfit ?? 0
            const side   = qty > 0 ? 'LONG' : 'SHORT'
            return (
              <div key={i} style={{
                display: 'flex', gap: 20, alignItems: 'center',
                padding: 16,
                background: T.bg1,
                border: `1px solid ${T.line}`,
                borderLeft: `3px solid ${qty > 0 ? T.green : T.red}`,
              }}>
                <span style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 700, color: T.ink0, minWidth: 60 }}>{sid}</span>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: qty > 0 ? T.green : T.red }}>{side}</span>
                <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>{Math.abs(qty)} × ₹{entry?.toFixed?.(2)}</span>
                <span style={{ marginLeft: 'auto', fontFamily: T.dot, fontSize: 20, color: colorPnl(upnl) }}>
                  {upnl >= 0 ? '+' : ''}{INR0(upnl)}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────
// Signals tab
// ─────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────
// Action watchlist — persistent (securities with live trades/positions)
// ─────────────────────────────────────────────────────────────────
function ActionWatchlist({ positions, paperPositions, tradelog, kronosSignals }) {
  const live   = (positions?.data?.data ?? []).filter(p => p.netQty !== 0)
  const paper  = (paperPositions?.data?.data?.data ?? paperPositions?.data?.data ?? []).filter(p => p.in_position)
  const recent = (tradelog?.data?.trades ?? [])
    .filter(t => t.type === 'ENTRY')
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
                <span style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 700, color: T.ink0, minWidth: 60 }}>{sid}</span>
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
function RightPanel({ risk, signals }) {
  const rpnl  = risk?.data?.realised_pnl   ?? 0
  const upnl  = risk?.data?.unrealised_pnl ?? 0
  const total = risk?.data?.total_pnl      ?? 0
  const limit = 5000
  const losspct = Math.min(Math.abs(Math.min(total, 0)) / limit * 100, 100)
  const raw   = signals?.data
  const feed  = (Array.isArray(raw) ? raw : []).slice(0, 15)
  const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber, HOLD: T.ink3 }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* PnL block */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>
          Today P&L
        </div>
        <div style={{ display: 'flex', gap: 20, alignItems: 'flex-end', marginBottom: 14 }}>
          <div>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 3 }}>TOTAL</div>
            <div style={{ fontFamily: T.dot, fontSize: 36, color: colorPnl(total), lineHeight: 1 }}>
              {total >= 0 ? '+' : ''}₹{Math.round(total).toLocaleString('en-IN')}
            </div>
          </div>
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
        {/* Loss meter */}
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 4 }}>
          DAILY LOSS LIMIT · ₹{limit.toLocaleString('en-IN')}
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

      {/* Live signal feed */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}`, flex: 1 }}>
        <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
          display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 6, height: 6, borderRadius: '50%', background: T.amber,
            boxShadow: `0 0 6px ${T.amber}` }} />
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
            Live Signal Feed
          </span>
        </div>
        <div style={{ maxHeight: 300, overflowY: 'auto' }}>
          {feed.length === 0
            ? <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>Waiting for signals…</div>
            : feed.map((s, i) => (
              <div key={i} style={{
                display: 'grid', gridTemplateColumns: '50px 40px 70px 1fr',
                gap: 8, padding: '5px 16px',
                background: i % 2 === 0 ? T.bg1 : T.bg2,
                borderLeft: `2px solid ${ACTION_COLOR[s.action] ?? T.line}`,
              }}>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{fmtTime(s.timestamp)}</span>
                <span style={{ fontFamily: T.mono, fontSize: 9, fontWeight: 700, color: ACTION_COLOR[s.action] ?? T.ink1 }}>{s.action}</span>
                {s.price && <span style={{ fontFamily: T.dot, fontSize: 15, color: T.ink0 }}>₹{s.price}</span>}
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.reason}</span>
              </div>
            ))
          }
        </div>
      </div>
    </div>
  )
}

function SignalsTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 60px' }}>
      <SessionBar status={data.status} />

      {/* Main 2-col layout: Kronos board (left) + PnL+feed (right) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 16, marginBottom: 16 }}>
        <div>
          <KronosBoard kronosSignals={data.kronosSignals} screener={data.screener} />
        </div>
        <RightPanel risk={data.risk} signals={data.signals} />
      </div>

      {/* Action watchlist — full width below */}
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
function EquityCurve({ tradelog }) {
  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.type === 'EXIT' && t.pnl != null)
    .sort((a,b) => a.ts?.localeCompare(b.ts ?? '') ?? 0)
  let eq = 0
  const pts = [{ v: 0 }]
  exits.forEach(t => { eq += t.pnl || 0; pts.push({ v: Math.round(eq) }) })
  const color = eq >= 0 ? T.green : T.red

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16, marginBottom: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 12 }}>
        EQUITY CURVE
      </div>
      {pts.length < 2 ? (
        <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, padding: '24px 0', textAlign: 'center' }}>
          Awaiting closed trades
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={pts} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={T.line} strokeDasharray="3 3" vertical={false} />
            <XAxis hide />
            <YAxis width={70} tick={{ fontFamily: T.mono, fontSize: 8, fill: T.ink3 }}
              tickFormatter={v => '₹' + v.toLocaleString('en-IN')} />
            <Tooltip
              contentStyle={{ background: T.bg2, border: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10 }}
              itemStyle={{ color: T.ink0 }}
              formatter={v => [INR(v), 'P&L']}
              labelFormatter={() => ''}
            />
            <Line type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

function TradeTable({ tradelog }) {
  const trades = (tradelog?.data?.trades ?? [])
    .filter(t => t.type === 'EXIT')
    .slice(-30)
    .reverse()

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
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{fmtTime(t.ts)}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 10, color: T.ink0, fontWeight: 700 }}>{t.symbol}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 9, color: t.action === 'EXIT' ? T.amber : T.ink1 }}>{t.action}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.dot, fontSize: 15, color: T.ink0 }}>₹{t.price}</td>
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
  const exits  = trades.filter(t => t.type === 'EXIT' && t.pnl != null)
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
  const exits  = trades.filter(t => t.type === 'EXIT' && t.pnl != null && t.ts)

  // Group by date
  const byDate = {}
  exits.forEach(t => {
    const d = t.ts?.slice(0, 10)
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
  const balance = data.funds?.data?.data?.availabelBalance ?? 0
  const sod     = data.funds?.data?.data?.sodLimit ?? 0
  const used    = data.funds?.data?.data?.utilizedAmount ?? 0
  const rpnl    = data.risk?.data?.realised_pnl ?? 0
  const upnl    = data.risk?.data?.unrealised_pnl ?? 0
  const total   = data.risk?.data?.total_pnl ?? 0
  const limit   = 5000
  const losspct = Math.min(Math.abs(Math.min(total, 0)) / limit * 100, 100)

  return (
    <div style={{ padding: '20px 24px 60px' }}>
      {/* 6-metric row */}
      <PortfolioMetrics tradelog={data.tradelog} risk={data.risk} />

      {/* Main 3-col grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 280px', gap: 16 }}>
        {/* Left: equity curve */}
        <div>
          <EquityCurve tradelog={data.tradelog} />
          <CalendarPnL tradelog={data.tradelog} />
        </div>

        {/* Mid: trade table */}
        <TradeTable tradelog={data.tradelog} />

        {/* Right: account + loss meter */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Account */}
          <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>Account</div>
            {[['Available', balance, T.green], ['SOD Limit', sod, T.ink0], ['Deployed', used, T.amber]].map(([l, v, c]) => (
              <div key={l} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: `1px solid ${T.line}` }}>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.12em', textTransform: 'uppercase' }}>{l}</span>
                <span style={{ fontFamily: T.dot, fontSize: 22, color: c }}>₹{Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
              </div>
            ))}
          </div>

          {/* Today P&L */}
          <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 12 }}>Today P&L</div>
            <div style={{ fontFamily: T.dot, fontSize: 36, color: colorPnl(total), lineHeight: 1, marginBottom: 12 }}>
              {total >= 0 ? '+' : ''}₹{Math.round(total).toLocaleString('en-IN')}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
              {[['Realised', rpnl], ['Unrealised', upnl]].map(([l, v]) => (
                <div key={l} style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 10px' }}>
                  <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 3 }}>{l}</div>
                  <div style={{ fontFamily: T.dot, fontSize: 18, color: colorPnl(v) }}>
                    {v >= 0 ? '+' : ''}₹{Math.abs(Math.round(v)).toLocaleString('en-IN')}
                  </div>
                </div>
              ))}
            </div>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 5 }}>
              Loss limit · {losspct.toFixed(1)}% of ₹{limit.toLocaleString('en-IN')}
            </div>
            <div style={{ height: 6, background: T.bg3, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${losspct}%`,
                background: `linear-gradient(90deg, ${T.green}, ${T.amber} 60%, ${T.red})`,
                transition: 'width 0.5s' }} />
            </div>
          </div>
        </div>
      </div>
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

function DBPanel({ dbStats }) {
  const d     = dbStats?.data
  const b1m   = d?.bars?.find(b => b.timeframe === '1m')
  const b1d   = d?.bars?.find(b => b.timeframe === '1d')
  const nseEq = d?.instruments?.NSE_EQ ?? 0
  const total = Object.values(d?.instruments ?? {}).reduce((a,b) => a+b, 0)

  if (!d) return (
    <SysPanel title="TimescaleDB" right="10.0.1.155:5432">
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>Connecting…</div>
    </SysPanel>
  )

  return (
    <SysPanel title="TimescaleDB" right="10.0.1.155 / dhan_trading" accent={T.cyan}>
      <BigStat label="1-min bars" value={b1m ? (b1m.rows/1000).toFixed(0)+'K' : '—'} color={T.cyan}
        sub={b1m ? `${b1m.earliest} → ${b1m.latest}` : 'No data'} />
      <BigStat label="Daily bars" value={b1d ? (b1d.rows/1000).toFixed(1)+'K' : '—'} color={T.cyan}
        sub={b1d ? `${b1d.earliest} → ${b1d.latest}` : 'No data'} />

      <div style={{ height: 1, background: T.line, margin: '12px 0' }} />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        {[
          ['NSE Equities', nseEq.toLocaleString('en-IN'), T.amber],
          ['Total Instruments', total.toLocaleString('en-IN'), T.ink1],
          ['Signals recorded', d.signals ?? 0, T.green],
          ['Trades recorded',  d.trades  ?? 0, T.green],
        ].map(([label, val, color]) => (
          <div key={label} style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 10px' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 3 }}>{label}</div>
            <div style={{ fontFamily: T.dot, fontSize: 20, color }}>{val}</div>
          </div>
        ))}
      </div>
    </SysPanel>
  )
}

function BackfillPanel({ backfill, dbStats }) {
  const d       = backfill?.data
  const running = d?.running ?? false
  const logs    = d?.log_tail ?? []

  // Extract current security from ANY log line matching the patterns:
  //   "═══ security_id=1008 ═══"  or  "  [1m] 1008  2023-08-20 ..."
  const findSec = (lines) => {
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/security_id=(\d+)/) || lines[i].match(/\[1m\]\s+(\d+)\s+\d{4}/)
      if (m) return m[1]
    }
    return null
  }
  const curSec = findSec(logs)

  // Extract current date chunk from log lines like: "[1m] 1008  2023-08-20 → 2023-11-17"
  const findChunk = (lines) => {
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/(\d{4}-\d{2}-\d{2})\s+→\s+(\d{4}-\d{2}-\d{2})/)
      if (m) return `${m[1]} → ${m[2]}`
    }
    return null
  }
  const curChunk = findChunk(logs)

  // Progress from DB: how many securities actually have bars loaded
  const loaded = (() => {
    const bars = dbStats?.data?.bars?.find(b => b.timeframe === '1m')
    // Can't get distinct security count from dbStats directly,
    // but we can estimate from the security_id in log (0-indexed position in sorted list)
    // Better: use loaded count from the DB bars endpoint if available
    return null  // will show from curSec estimate
  })()

  const TOTAL    = 22646
  // Estimate position: sort by security_id is numeric, IDs range 100–~15000
  // Use curSec as rough index — not perfectly accurate but directional
  const secNum   = parseInt(curSec ?? '0', 10)
  const pct      = curSec ? Math.min((secNum / 15000) * 100, 99) : 0
  // Each security ≈ 21 API calls (20 intraday + 1 daily); rate = 5 req/s
  const remaining= curSec ? Math.ceil((TOTAL - (TOTAL * pct / 100)) * 21 / 5 / 3600) : null

  const stripPrefix = l => l.replace(/^\d{2}:\d{2}:\d{2}\s+\w+\s+dhan\.\w+\s+[—–]\s+/, '')

  return (
    <SysPanel title="Backfill Progress" right={`${TOTAL.toLocaleString('en-IN')} NSE equities`}
      accent={running ? T.green : T.ink3}>

      {/* Status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <div style={{
          width: 10, height: 10, borderRadius: '50%',
          background: running ? T.green : T.ink3,
          boxShadow: running ? `0 0 10px ${T.green}` : 'none', flexShrink: 0,
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 11, fontWeight: 600,
          color: running ? T.green : T.ink2, letterSpacing: '0.1em' }}>
          {running ? 'RUNNING' : 'IDLE'}
        </span>
        {curSec && (
          <span style={{ fontFamily: T.dot, fontSize: 18, color: T.ink1 }}>
            #{curSec}
          </span>
        )}
      </div>

      {/* Progress bar — always shown when running */}
      {running && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between',
            fontFamily: T.mono, fontSize: 9, color: T.ink3, marginBottom: 6 }}>
            <span>PROGRESS (APPROX)</span>
            <span style={{ color: pct > 0 ? T.green : T.ink3 }}>
              {pct.toFixed(1)}%
            </span>
          </div>
          <div style={{ height: 6, background: T.bg3, position: 'relative', overflow: 'hidden' }}>
            <div style={{
              height: '100%', width: `${pct}%`,
              background: `linear-gradient(90deg, ${T.green}, ${T.cyan})`,
              transition: 'width 2s ease',
            }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between',
            fontFamily: T.mono, fontSize: 9, color: T.ink3, marginTop: 5 }}>
            {curChunk
              ? <span style={{ color: T.ink2 }}>{curChunk}</span>
              : <span>Scanning…</span>
            }
            {remaining !== null && remaining > 0 && (
              <span style={{ color: T.amber }}>~{remaining}h left</span>
            )}
          </div>
        </div>
      )}

      {/* Recent log */}
      <SysLabel>Recent activity</SysLabel>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 160, overflowY: 'auto' }}>
        {logs.length === 0
          ? <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No activity</span>
          : logs.map((l, i) => (
            <div key={i} style={{
              fontFamily: T.mono, fontSize: 9, lineHeight: 1.8,
              color: l.includes('ERROR') ? T.red : l.includes('done') || l.includes('upserted') ? T.green : T.ink2,
              borderLeft: `2px solid ${l.includes('ERROR') ? T.red : l.includes('done') || l.includes('upserted') ? T.green : T.line}`,
              paddingLeft: 8,
            }}>
              {stripPrefix(l)}
            </div>
          ))
        }
      </div>
    </SysPanel>
  )
}

function HermesPanel({ hermes }) {
  const d       = hermes?.data
  const running = d?.running ?? false
  const CRONS = [
    ['Pre-market brief',   '08:45 IST', 'Mon–Fri', T.cyan],
    ['Drawdown check',     'Every 5min','Market hrs', T.amber],
    ['Position reconcile', 'Every 30min','Market hrs', T.green],
    ['Backfill watchdog',  'Every 15min','Always',    T.green],
    ['EOD trade review',   '15:45 IST', 'Mon–Fri',   T.cyan],
    ['Data quality scan',  '02:00 IST', 'Nightly',   T.ink2],
    ['Gap scan',           '02:30 IST', 'Nightly',   T.ink2],
    ['Strategy perf.',     '09:00 IST', 'Sunday',    T.amber],
    ['Signal calibration', '09:30 IST', 'Sunday',    T.amber],
    ['Health report',      '09:00 IST', 'Sunday',    T.cyan],
  ]

  return (
    <SysPanel title="Hermes Gateway · @farshoribot"
      accent={running ? T.cyan : T.red}>
      {/* Status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <div style={{
          width: 10, height: 10, borderRadius: '50%',
          background: running ? T.green : T.red,
          boxShadow: running ? `0 0 10px ${T.green}` : 'none', flexShrink: 0,
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 11, color: running ? T.green : T.red,
          letterSpacing: '0.1em', fontWeight: 600 }}>
          {running ? 'ONLINE' : 'OFFLINE'}
        </span>
      </div>

      {/* Model info */}
      {d?.model && (
        <div style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 12px', marginBottom: 14 }}>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.14em', marginBottom: 3 }}>MODEL</div>
          <div style={{ fontFamily: T.mono, fontSize: 10, color: T.cyan }}>{d.model}</div>
          {d.provider && <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginTop: 2 }}>via {d.provider}</div>}
        </div>
      )}

      {/* Cron schedule */}
      <SysLabel>Autonomous schedules ({CRONS.length})</SysLabel>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {CRONS.map(([name, time, freq, color]) => (
          <div key={name} style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '5px 8px',
            borderLeft: `2px solid ${color}`,
            background: T.bg2,
          }}>
            <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink1 }}>{name}</span>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <span style={{ fontFamily: T.dot, fontSize: 15, color }}>{time}</span>
              <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, minWidth: 55, textAlign: 'right' }}>{freq}</span>
            </div>
          </div>
        ))}
      </div>
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
      {/* Top row: 3 equal columns */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16, marginBottom: 16 }}>
        <DBPanel       dbStats={data.dbStats} />
        <BackfillPanel backfill={data.backfill} dbStats={data.dbStats} />
        <HermesPanel   hermes={data.hermes} />
      </div>

      {/* Bottom: full-width log, taller */}
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
        <Header status={data.status} risk={data.risk} funds={data.funds} />
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
