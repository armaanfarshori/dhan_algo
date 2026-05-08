import { useState } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { T, INR, INR0, colorPnl, fmtTime } from './tokens'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, CartesianGrid } from 'recharts'

// Intro removed per user request
import TopBar             from './components/cockpit/TopBar'
import HeroSection        from './components/cockpit/HeroSection'
import InfoStrip          from './components/cockpit/InfoStrip'
import Pipeline           from './components/cockpit/Pipeline'
import ActivePosition     from './components/cockpit/ActivePosition'
import PayoffChart        from './components/cockpit/PayoffChart'
import FloatingKillSwitch from './components/cockpit/FloatingKillSwitch'
import StrategySidebar    from './components/cockpit/StrategySidebar'
import BacktestTab        from './components/cockpit/BacktestTab'
import WatchlistPanel     from './components/cockpit/WatchlistPanel'
import LiveTicker        from './components/cockpit/LiveTicker'
import { FnoPanel, EqPanel } from './components/cockpit/TradingPanels'

const TABS = ['Cockpit', 'Backtest', 'Risk Console']

// ── Shared panel primitives ───────────────────────────────────────────────────
function Panel({ children, style }) {
  return <div style={{ background: T.bg1, border: `1px solid ${T.line}`, ...style }}>{children}</div>
}
function PanelH({ children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
      {children}
    </div>
  )
}
function LiveTag() {
  return <span style={{ background: T.greenD, color: T.green, padding: '2px 6px', fontSize: 9, letterSpacing: '0.2em' }}>LIVE</span>
}

// ── Account Balance ───────────────────────────────────────────────────────────
function AccountBalance({ funds }) {
  const f = funds?.data?.data
  return (
    <Panel>
      <PanelH><span style={{ color: T.ink0 }}>ACCOUNT</span></PanelH>
      <div style={{ padding: '14px 16px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {[
          ['Available', f?.availabelBalance ?? 0, T.green],
          ['SOD Limit', f?.sodLimit ?? 0, T.ink0],
          ['Utilised', f?.utilizedAmount ?? 0, T.amber],
          ['Withdrawable', f?.withdrawableBalance ?? 0, T.cyan],
        ].map(([k, v, c]) => (
          <div key={k} style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '10px 12px' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 4 }}>{k}</div>
            <div style={{ fontFamily: T.dot, fontSize: 24, color: c }}>{INR(v)}</div>
          </div>
        ))}
      </div>
    </Panel>
  )
}

// ── Risk panel ────────────────────────────────────────────────────────────────
function RiskPanel({ risk }) {
  const d = risk?.data
  const halted = d?.halted ?? false
  const r = d?.realised_pnl   ?? 0
  const u = d?.unrealised_pnl ?? 0
  const t = d?.total_pnl      ?? 0
  return (
    <Panel style={{ border: halted ? `1px solid ${T.red}` : `1px solid ${T.line}` }}>
      <PanelH>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: halted ? T.red : T.green, boxShadow: `0 0 6px ${halted ? T.red : T.green}`, animation: halted ? 'pulse 1s infinite' : 'none', flexShrink: 0 }} />
        <span style={{ color: halted ? T.red : T.green }}>{halted ? `HALTED — ${d?.halt_reason}` : 'RISK OK'}</span>
      </PanelH>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, padding: 14 }}>
        {[['REALISED', r], ['UNREALISED', u], ['TOTAL', t]].map(([k, v]) => (
          <div key={k} style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 10px' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 4 }}>{k}</div>
            <div style={{ fontFamily: T.dot, fontSize: 22, color: colorPnl(v) }}>{INR(v)}</div>
          </div>
        ))}
      </div>
      <div style={{ padding: '0 14px 14px' }}>
        <div style={{ height: 6, background: T.bg3, border: `1px solid ${T.line}`, borderRadius: 1, overflow: 'hidden' }}>
          <div style={{ height: '100%', width: `${Math.min(Math.abs(t) / 5000 * 100, 100)}%`, background: `linear-gradient(90deg,${T.green},${T.amber} 70%,${T.red})`, transition: 'width .5s' }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 4 }}>
          <span>₹0</span><span>₹2,500</span><span>₹5,000 CAP</span>
        </div>
      </div>
    </Panel>
  )
}

// ── Velocity ──────────────────────────────────────────────────────────────────
function VelocityPanel({ status }) {
  const d = status?.data
  const orders = d?.orders_placed ?? 0
  const uptime = d?.uptime_seconds ?? 1
  const rate = (orders / (uptime / 3600)).toFixed(1)
  return (
    <Panel>
      <PanelH><span style={{ color: T.ink0 }}>VELOCITY</span></PanelH>
      <div style={{ padding: 14 }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 8 }}>TRADES / HOUR</div>
        <div style={{ fontFamily: T.dot, fontSize: 52, color: T.amber, lineHeight: 0.95, textShadow: `0 0 14px oklch(0.82 0.16 75 / 0.25)` }}>
          {rate}<span style={{ fontSize: 22, color: T.ink2, marginLeft: 6 }}>/hr</span>
        </div>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, marginTop: 8, letterSpacing: '0.14em', textTransform: 'uppercase' }}>
          {orders} orders · {Math.floor(uptime / 3600)}h {Math.floor((uptime % 3600) / 60)}m uptime
        </div>
      </div>
    </Panel>
  )
}

// ── Signal feed ───────────────────────────────────────────────────────────────
function SignalFeed({ signals }) {
  const rows = signals?.data ?? []
  const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber }
  return (
    <Panel>
      <PanelH>
        <span style={{ color: T.ink0 }}>SIGNAL FEED</span>
        <span style={{ marginLeft: 'auto', fontSize: 9 }}>{rows.length} signals</span>
      </PanelH>
      <div style={{ overflowY: 'auto', maxHeight: 260 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>{['TIME', 'ACTION', 'PRICE', 'REASON'].map(h => (
              <th key={h} style={{ textAlign: 'left', fontFamily: T.mono, fontSize: 9, color: T.ink2, padding: '6px 10px', borderBottom: `1px solid ${T.line}`, textTransform: 'uppercase', letterSpacing: '0.12em', position: 'sticky', top: 0, background: T.bg1 }}>{h}</th>
            ))}</tr>
          </thead>
          <tbody>
            {!rows.length
              ? <tr><td colSpan={4} style={{ padding: '16px 10px', fontFamily: T.mono, fontSize: 10, color: T.ink3, letterSpacing: '0.14em' }}>WAITING FOR FIRST SIGNAL…</td></tr>
              : rows.map((s, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${T.line}` }}>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>{fmtTime(s.timestamp)}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 10, color: ACTION_COLOR[s.action] || T.ink0, fontWeight: 600 }}>{s.action}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.dot, fontSize: 16, color: T.ink0 }}>{s.price ? `₹${s.price}` : '—'}</td>
                  <td style={{ padding: '7px 10px', fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>{s.reason}</td>
                </tr>
              ))
            }
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

// ── Realised P&L curve (from trade log) ──────────────────────────────────────
function EquityPanel({ tradelog }) {
  const trades = tradelog?.data?.trades ?? []

  // Build cumulative P&L from EXIT records in the trade log (accurate source)
  const exits = trades
    .filter(t => t.type === 'EXIT' && t.pnl != null)
    .sort((a, b) => a.ts.localeCompare(b.ts))

  let equity = 0
  const pts = [{ pnl: 0 }]
  exits.forEach(t => {
    equity += t.pnl || 0
    pts.push({ pnl: Math.round(equity) })
  })

  const isUp   = equity >= 0
  const hasData = exits.length > 0
  const summary = tradelog?.data?.summary

  return (
    <Panel>
      <PanelH>
        <LiveTag /><span style={{ color: T.ink0 }}>REALISED P&amp;L CURVE</span>
        <span style={{ marginLeft: 'auto', fontFamily: T.dot, fontSize: 20, color: isUp ? T.green : T.red }}>
          {hasData
          ? (isUp ? '+' : '') + INR0(pts[pts.length - 1]?.pnl || 0)
          : summary ? `${summary.total_entries} entries · ${summary.total_exits} exits` : 'AWAITING EXIT'}
        </span>
      </PanelH>
      <div style={{ padding: '12px 16px 16px' }}>
        {hasData ? (
          <ResponsiveContainer width="100%" height={130}>
            <LineChart data={pts} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
              <XAxis hide />
              <YAxis tickFormatter={v => `₹${v}`} width={60} tick={{ fill: T.ink3, fontSize: 9, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
              <Tooltip formatter={v => [INR0(v), 'Realised P&L']} contentStyle={{ background: T.bg3, border: `1px solid ${T.line2}`, fontFamily: 'JetBrains Mono', fontSize: 11 }} />
              <ReferenceLine y={0} stroke={T.line2} strokeDasharray="4 2" />
              <Line type="monotone" dataKey="pnl" stroke={isUp ? T.green : T.red} dot={false} strokeWidth={1.5} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div style={{ height: 80, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: T.mono, fontSize: 10, color: T.ink3, letterSpacing: '0.14em', flexDirection: 'column', gap: 8 }}>
            <span>{summary ? `${summary.total_entries} TRADES · ${summary.open_trades} OPEN` : 'NO TRADES YET'}</span>
            <span style={{ fontSize: 8 }}>P&L UPDATES ON EVERY EXIT — SOURCE: .logs/trades.jsonl</span>
          </div>
        )}
      </div>
    </Panel>
  )
}

// ── Paper Positions (scanner simulated) ───────────────────────────────────────
function PaperPositions({ paperPositions }) {
  const pp = paperPositions?.data
  const items = pp?.data ?? []
  return (
    <Panel>
      <PanelH>
        <LiveTag />
        <span style={{ color: T.ink0 }}>PAPER POSITIONS</span>
        <span style={{ marginLeft: 'auto', fontSize: 9, color: items.length > 0 ? T.amber : T.ink3 }}>
          {items.length} OPEN
        </span>
      </PanelH>
      <div style={{ padding: 14 }}>
        {!items.length
          ? <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase' }}>NO PAPER POSITIONS</div>
          : items.map((p, i) => (
            <div key={i} style={{ background: T.bg2, border: `1px solid ${p.engine === 'F&O' ? T.green : T.cyan}`, padding: '10px 12px', marginBottom: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontFamily: T.mono, fontSize: 11, color: p.engine === 'F&O' ? T.green : T.cyan, fontWeight: 600 }}>{p.symbol}</span>
                <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.14em', background: T.bg3, padding: '1px 5px' }}>{p.engine}</span>
              </div>
              {p.engine === 'F&O' && (
                <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
                  Entry ₹{p.entry_premium} · Lot {p.lot_size} · BEP ₹{p.bep?.toFixed(2)} · {p.expiry}
                </div>
              )}
              {p.engine === 'EQ' && (
                <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
                  Entry ₹{p.entry_price?.toFixed(2)} · Qty {p.qty} · {p.segment}
                </div>
              )}
            </div>
          ))
        }
      </div>
    </Panel>
  )
}

// ── Footer ────────────────────────────────────────────────────────────────────
function FootBar({ status, risk }) {
  const d = status?.data
  const r = risk?.data
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 18, padding: '12px 0', marginTop: 18, borderTop: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 9, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.18em', flexWrap: 'wrap' }}>
      <span>ORDERS <b style={{ color: T.ink0 }}>{d?.orders_placed ?? '—'}</b></span>
      <span>POSITION <b style={{ color: (d?.position ?? 0) !== 0 ? T.green : T.ink3 }}>{d?.position ?? 0}</b></span>
      <span>HALTED <b style={{ color: r?.halted ? T.red : T.green }}>{r?.halted ? 'YES' : 'NO'}</b></span>
      <div style={{ flex: 1 }} />
      <span>FEED <b style={{ color: T.green }}>OK</b></span>
      <span>AUTH <b style={{ color: T.green }}>OK</b></span>
      <span>PAPER <b style={{ color: T.amber }}>SAFE</b></span>
    </div>
  )
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function Tabs({ active, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 0, padding: '14px 0 0', borderBottom: `1px solid ${T.line}`, marginBottom: 18 }}>
      {TABS.map(t => (
        <div key={t} onClick={() => onChange(t)} style={{
          fontFamily: T.mono, fontSize: 11, padding: '10px 16px',
          color: active === t ? T.ink0 : T.ink2,
          textTransform: 'uppercase', letterSpacing: '0.16em', cursor: 'pointer',
          borderBottom: active === t ? `1px solid ${T.green}` : '1px solid transparent',
          marginBottom: -1, transition: 'color .15s',
        }}>{t}</div>
      ))}
    </div>
  )
}

// ── Cockpit layout ────────────────────────────────────────────────────────────
function CockpitTab({ data }) {
  const { status, risk, signals, funds, positions, paperPositions, scalper, payoff, config, watchlist, scanner, fnoScanner, equityScanner, tradelog } = data

  return (
    <>
      {/* Full-width: hero + ticker + info strip above the two-column split */}
      <HeroSection risk={risk} status={status} funds={funds} paperPositions={paperPositions} />
      <LiveTicker fnoScanner={fnoScanner} equityScanner={equityScanner} />
      <InfoStrip status={status} risk={risk} funds={funds} scalper={scalper} />

      {/* Two-column grid starts here — sidebar aligns with account card */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 18, alignItems: 'start' }}>

        {/* ── LEFT ── */}
        <div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14, marginBottom: 14 }}>
            <AccountBalance funds={funds} />
            <RiskPanel risk={risk} />
            <VelocityPanel status={status} />
          </div>

          <ActivePosition scalper={scalper} status={status} />
          <div style={{ marginBottom: 14 }} />

          {/* Trading panels immediately after Active Position */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
            <FnoPanel fnoScanner={fnoScanner} signals={signals} />
            <EqPanel  paperPositions={paperPositions} signals={signals} />
          </div>

          <PayoffChart payoff={payoff} />
          <EquityPanel tradelog={tradelog} />
          <div style={{ marginBottom: 14 }} />

          {/* Pipelines */}
          <Pipeline scalper={scalper} scanner={fnoScanner} label="F&O INDEX OPTIONS" />
          <Pipeline scalper={null} scanner={equityScanner} label="EQUITY TOP MOVERS" />

          {/* Watchlist with SMA gauge — no refresh button */}
          <WatchlistPanel watchlist={watchlist} scanner={scanner} equityScanner={equityScanner} />

          <FootBar status={status} risk={risk} />
        </div>

        {/* ── RIGHT: sticky sidebar, no internal scrollbar ── */}
        <div style={{ position: 'sticky', top: 16 }}>
          <Panel>
            <StrategySidebar config={config} scanner={fnoScanner} equityScanner={equityScanner} onSwitch={() => {}} />

          </Panel>
        </div>

      </div>
    </>
  )
}

// ── Risk console tab ──────────────────────────────────────────────────────────
function RiskConsoleTab({ data }) {
  return (
    <div style={{ padding: '20px 0', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
      <RiskPanel risk={data.risk} />
      <AccountBalance funds={data.funds} />
      <VelocityPanel status={data.status} />
      <Positions positions={data.positions} />
    </div>
  )
}

// ── Root ──────────────────────────────────────────────────────────────────────
export default function App() {
  const data   = useDashboardData()
  const [tab, setTab] = useState('Cockpit')
  const halted = data.risk?.data?.halted ?? false

  return (
    <>
      <FloatingKillSwitch onKill={() => {}} />
      <div style={{ position: 'relative', zIndex: 1, padding: '0 22px 60px', maxWidth: 1520, margin: '0 auto' }}>
        <TopBar status={data.status} halted={halted} />
        <Tabs active={tab} onChange={setTab} />
        {tab === 'Cockpit'      && <CockpitTab     data={data} />}
        {tab === 'Backtest'     && <BacktestTab />}
        {tab === 'Risk Console' && <RiskConsoleTab data={data} />}
      </div>
    </>
  )
}
