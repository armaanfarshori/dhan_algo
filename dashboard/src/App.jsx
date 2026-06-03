import { useState, useEffect, Component } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { T, INR, INR0, colorPnl, fmtTime } from './tokens'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import FloatingKillSwitch from './components/cockpit/FloatingKillSwitch'
import DataTab from './components/cockpit/DataTab'

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
      padding: '12px 14px',
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
          padding: '10px 14px',
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
                padding: '10px 14px',
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
function SignalsTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 60px', maxWidth: 1400 }}>
      <KronosBoard kronosSignals={data.kronosSignals} screener={data.screener} />
      <div style={{ height: 1, background: T.line, margin: '28px 0' }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 28 }}>
        <LiveFeed signals={data.signals} />
        <PositionStrip positions={data.positions} paperPositions={data.paperPositions} risk={data.risk} />
      </div>
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
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em' }}>
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

function RiskMeter({ risk }) {
  const pnl   = risk?.data?.total_pnl ?? 0
  const limit = 5000
  const pct   = Math.min(Math.abs(Math.min(pnl, 0)) / limit * 100, 100)

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 14, marginBottom: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 10 }}>
        DAILY LOSS LIMIT
      </div>
      <div style={{ height: 8, background: T.bg3, position: 'relative', overflow: 'hidden', marginBottom: 6 }}>
        <div style={{
          height: '100%', width: `${pct}%`,
          background: `linear-gradient(90deg, ${T.green}, ${T.amber} 60%, ${T.red})`,
          transition: 'width 0.5s',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
        <span>₹0</span>
        <span style={{ color: pct > 75 ? T.red : T.ink3 }}>{pct.toFixed(1)}% consumed</span>
        <span>₹{limit.toLocaleString('en-IN')} MAX</span>
      </div>
    </div>
  )
}

function PortfolioTab({ data }) {
  const balance = data.funds?.data?.data?.availabelBalance ?? 0
  const sod     = data.funds?.data?.data?.sodLimit ?? 0
  const used    = data.funds?.data?.data?.utilizedAmount ?? 0

  return (
    <div style={{ padding: '20px 24px 60px', maxWidth: 1400, display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 20 }}>
      <div>
        <EquityCurve tradelog={data.tradelog} />
        <TradeTable tradelog={data.tradelog} />
      </div>
      <div>
        <RiskMeter risk={data.risk} />
        <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 14 }}>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginBottom: 12 }}>ACCOUNT</div>
          {[
            ['AVAILABLE', balance, T.green],
            ['SOD LIMIT', sod,     T.ink0],
            ['DEPLOYED',  used,    T.amber],
          ].map(([label, val, color]) => (
            <div key={label} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: `1px solid ${T.line}` }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.12em' }}>{label}</span>
              <span style={{ fontFamily: T.dot, fontSize: 20, color }}>₹{Number(val).toLocaleString('en-IN', { minimumFractionDigits: 0 })}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// System tab — reuse DataTab + add kill switch + logs
// ─────────────────────────────────────────────────────────────────
function LogPanel({ logs }) {
  // logs API: {ok, logs:[...]}  → need .data.logs
  const rows = (logs?.data?.logs ?? []).slice(-20)
  const COLOR = { WARNING: T.amber, ERROR: T.red, CRITICAL: T.red }

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, marginTop: 16 }}>
      <div style={{ padding: '8px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.15em' }}>
        SYSTEM LOGS
      </div>
      <div style={{ maxHeight: 240, overflowY: 'auto', padding: '6px 0' }}>
        {rows.length === 0
          ? <div style={{ padding: '10px 14px', fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No log output</div>
          : rows.map((l, i) => (
            <div key={i} style={{ display: 'flex', gap: 10, padding: '2px 14px', fontFamily: T.mono, fontSize: 10 }}>
              <span style={{ color: T.ink3, minWidth: 55 }}>{fmtTime(l.ts)}</span>
              <span style={{ color: T.ink3, minWidth: 10 }}>{l.icon}</span>
              <span style={{ color: T.ink3, minWidth: 60 }}>{l.name}</span>
              <span style={{ color: COLOR[l.level] ?? T.ink2 }}>{l.msg}</span>
            </div>
          ))
        }
      </div>
    </div>
  )
}

function SystemTab({ data }) {
  return (
    <div style={{ padding: '20px 24px 60px', maxWidth: 900 }}>
      <DataTab dbStats={data.dbStats} backfill={data.backfill} hermes={data.hermes} />
      <LogPanel logs={data.logs} />
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
