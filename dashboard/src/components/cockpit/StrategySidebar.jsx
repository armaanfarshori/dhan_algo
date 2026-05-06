import { useState } from 'react'
import { T } from '../../tokens'

const CATEGORIES = ['All', 'Intraday', 'Options', 'Swing', 'Quant']

const STRATEGIES = [
  {
    cat: 'Options', risk: 'VERY HIGH', riskColor: '#f85149',
    name: 'RSI Options Scalper', badge: 'F&O · LIVE',
    desc: 'Buy ATM NIFTY call/put on RSI-14 crossover (30/70). Forever OCO exit at breakeven + ₹5 buffer. Auto-discovers ATM strike from Dhan master.',
    tags: ['NSE_FNO', 'NIFTY', 'Weekly', 'OCO'],
    hold: 'Minutes', capital: '₹50k+',
    strategy: 'scalper', segment: 'NSE_FNO',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM', riskColor: '#d29922',
    name: 'SMA Crossover', badge: 'EQUITY · LIVE',
    desc: 'Go long on 9/21 golden cross, short on death cross. Deque-based SMA with zero external deps. Warm-up: 21 ticks.',
    tags: ['NSE_EQ', 'Equity', 'Any stock'],
    hold: 'Hours', capital: '₹25k+',
    strategy: 'sma_crossover', segment: 'NSE_EQ',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM-HIGH', riskColor: '#d29922',
    name: 'RSI Equity Scalper', badge: 'EQUITY · LIVE',
    desc: 'Buy when RSI-14 crosses above oversold (30); sell when crosses below overbought (70). Works on any NSE equity or commodity.',
    tags: ['NSE_EQ', 'MCX', 'Any stock'],
    hold: 'Minutes–hrs', capital: '₹20k+',
    strategy: 'rsi_scalper', segment: 'NSE_EQ',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM-HIGH', riskColor: '#d29922',
    name: 'Momentum Breakout', badge: 'EQUITY · LIVE',
    desc: 'Enter long when price breaks 20-day high; enter short on 20-day low break. Exit on reverse break. Trend-following for momentum sessions.',
    tags: ['NSE_EQ', 'MCX', 'Trending'],
    hold: 'Minutes–hrs', capital: '₹25k+',
    strategy: 'momentum_breakout', segment: 'NSE_EQ',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM', riskColor: '#3fb950',
    name: 'Mean Reversion', badge: 'EQUITY · LIVE',
    desc: 'Buy extreme RSI oversold (<25); hold until RSI returns to neutral (>55). Exploits short-term overreaction in range-bound stocks.',
    tags: ['NSE_EQ', 'Range-bound', 'Reversal'],
    hold: '15–90 min', capital: '₹20k+',
    strategy: 'mean_reversion', segment: 'NSE_EQ',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM', riskColor: '#3fb950',
    name: 'Bollinger Reversion', badge: 'EQUITY · LIVE',
    desc: 'Buy lower Bollinger Band (2σ) touch; exit at upper band or mean. 20-period band. Works best in low-volatility, ranging sessions.',
    tags: ['NSE_EQ', 'Bollinger', 'Reversion'],
    hold: '30–120 min', capital: '₹20k+',
    strategy: 'bollinger', segment: 'NSE_EQ',
  },
  {
    cat: 'Intraday', risk: 'MEDIUM', riskColor: '#3fb950',
    name: 'VWAP Reversion', badge: 'EQUITY · LIVE',
    desc: 'Buy when price deviates >0.5% below VWAP; exit when price recovers to VWAP. Classic intraday institutional anchor strategy.',
    tags: ['NSE_EQ', 'VWAP', 'Institutional'],
    hold: '15–60 min', capital: '₹20k+',
    strategy: 'vwap_reversion', segment: 'NSE_EQ',
  },
  {
    cat: 'Options', risk: 'HIGH', riskColor: '#f85149',
    name: 'Short Straddle', badge: 'F&O · LIVE',
    desc: 'Sell ATM CE + PE simultaneously to collect theta. Exit both legs on 50% SL or 30% target of combined premium. Requires pre-set call/put security IDs.',
    tags: ['NSE_FNO', 'Theta', 'Two-leg'],
    hold: 'Intraday–1wk', capital: '₹3L+',
    strategy: 'short_straddle', segment: 'NSE_FNO',
  },
  {
    cat: 'Options', risk: 'MEDIUM', riskColor: '#d29922',
    name: 'Iron Condor', badge: 'COMING SOON',
    desc: 'Sell OTM call spread + OTM put spread. 4-leg strategy with defined risk on both sides. Profits when index stays range-bound.',
    tags: ['BankNifty', 'Nifty', '4-leg'],
    hold: '1–5 days', capital: '₹1.5L+',
    comingSoon: true,
  },
  {
    cat: 'Quant', risk: 'LOW-MED', riskColor: '#3fb950',
    name: 'Pairs / Stat Arb', badge: 'COMING SOON',
    desc: 'Trade the spread between co-integrated instruments. Requires co-integration test + simultaneous two-stock execution.',
    tags: ['Equity', 'Futures', '2-stock'],
    hold: '1–10 days', capital: '₹2L+',
    comingSoon: true,
  },
]

function StrategyCard({ s, onSelect, active }) {
  return (
    <div
      onClick={() => !s.comingSoon && onSelect(s)}
      style={{
        background: active ? 'oklch(0.18 0.08 145 / 0.3)' : T.bg2,
        border: `1px solid ${active ? T.green : T.line}`,
        padding: '12px 14px', marginBottom: 8, cursor: s.comingSoon ? 'default' : 'pointer',
        opacity: s.comingSoon ? 0.55 : 1,
        transition: 'border-color .15s, background .15s',
        boxShadow: active ? `0 0 12px oklch(0.55 0.18 145 / 0.15)` : 'none',
      }}
      onMouseEnter={e => { if (!s.comingSoon && !active) e.currentTarget.style.borderColor = T.line2 }}
      onMouseLeave={e => { if (!s.comingSoon && !active) e.currentTarget.style.borderColor = T.line }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 6 }}>
        <span style={{
          fontFamily: T.mono, fontSize: 9, padding: '2px 7px',
          background: s.comingSoon ? T.bg3 : 'rgba(255,255,255,0.06)',
          color: s.comingSoon ? T.ink3 : T.ink1,
          border: `1px solid ${s.comingSoon ? T.line : T.line2}`,
          letterSpacing: '0.14em', textTransform: 'uppercase',
        }}>
          {s.comingSoon ? 'COMING SOON' : s.badge}
        </span>
        <span style={{ fontFamily: T.mono, fontSize: 9, color: s.riskColor, letterSpacing: '0.1em' }}>
          {s.risk}
        </span>
      </div>

      <div style={{ fontFamily: T.mono, fontSize: 13, fontWeight: 600, color: active ? T.green : T.ink0, marginBottom: 4 }}>
        {s.name}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2, lineHeight: 1.5, marginBottom: 8, letterSpacing: '0.04em' }}>
        {s.desc}
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
        {s.tags.map(tag => (
          <span key={tag} style={{
            fontFamily: T.mono, fontSize: 9, color: T.ink2,
            background: T.bg3, border: `1px solid ${T.line}`,
            padding: '1px 6px', letterSpacing: '0.1em',
          }}>{tag}</span>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', borderTop: `1px solid ${T.line}`, paddingTop: 8, gap: 4 }}>
        {[['RISK', s.risk, s.riskColor], ['HOLD', s.hold, T.ink0], ['CAPITAL', s.capital, T.cyan]].map(([k, v, c]) => (
          <div key={k} style={{ textAlign: 'center' }}>
            <div style={{ fontFamily: T.mono, fontSize: 10, fontWeight: 500, color: c }}>{v}</div>
            <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.14em', marginTop: 2 }}>{k}</div>
          </div>
        ))}
      </div>

      {!s.comingSoon && (
        <div style={{ fontFamily: T.mono, fontSize: 10, color: T.green, marginTop: 8, letterSpacing: '0.12em' }}>
          {active ? '● ACTIVE' : 'SELECT →'}
        </div>
      )}
    </div>
  )
}

export default function StrategySidebar({ config, onSwitch }) {
  const [cat, setCat]   = useState('All')
  const [applying, setApplying] = useState(false)
  const [msg, setMsg]   = useState(null)
  const activeStrategy  = config?.data?.strategy || 'scalper'
  const [qty, setQty]   = useState(1)

  const filtered = cat === 'All' ? STRATEGIES : STRATEGIES.filter(s => s.cat === cat)

  async function handleSelect(s) {
    setApplying(true); setMsg(null)
    try {
      const r = await fetch('/api/strategy/switch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: s.strategy, segment: s.segment, security_id: '13', quantity: s.segment === 'NSE_FNO' ? 75 : qty, num_lots: qty }),
      })
      const d = await r.json()
      setMsg(d.ok ? { ok: true, text: `Switched to ${s.name}` } : { ok: false, text: d.error })
      if (d.ok) onSwitch?.()
    } catch (e) { setMsg({ ok: false, text: String(e) }) }
    finally { setApplying(false) }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span style={{ color: T.ink0 }}>STRATEGY PANEL</span>
        {msg && <span style={{ fontSize: 9, color: msg.ok ? T.green : T.red }}>{msg.text}</span>}
      </div>

      {/* Lots slider */}
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, background: T.bg2 }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 6 }}>
          LOTS: <span style={{ color: T.cyan }}>{qty}</span>
        </div>
        <input type="range" min={1} max={10} value={qty} onChange={e => setQty(Number(e.target.value))}
          style={{ width: '100%', accentColor: T.green, cursor: 'pointer' }} />
      </div>

      {/* Category filter */}
      <div style={{ display: 'flex', borderBottom: `1px solid ${T.line}`, overflowX: 'auto' }}>
        {CATEGORIES.map(c => (
          <div key={c} onClick={() => setCat(c)} style={{
            fontFamily: T.mono, fontSize: 9, padding: '8px 12px', cursor: 'pointer',
            color: cat === c ? T.ink0 : T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase',
            borderBottom: cat === c ? `1px solid ${T.green}` : '1px solid transparent',
            marginBottom: -1, whiteSpace: 'nowrap', transition: 'color .15s',
          }}>{c}</div>
        ))}
      </div>

      {/* Strategy cards */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>
        {applying && (
          <div style={{ fontFamily: T.mono, fontSize: 10, color: T.amber, letterSpacing: '0.14em', marginBottom: 8 }}>
            SWITCHING STRATEGY…
          </div>
        )}
        {filtered.map((s, i) => (
          <StrategyCard key={i} s={s} active={s.strategy === activeStrategy && !s.comingSoon} onSelect={handleSelect} />
        ))}
      </div>
    </div>
  )
}
