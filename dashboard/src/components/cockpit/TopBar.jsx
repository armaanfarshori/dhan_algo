import { useState, useEffect } from 'react'
import { T } from '../../tokens'

function Clock() {
  const [display, setDisplay] = useState({ time: '', date: '' })
  useEffect(() => {
    function tick() {
      const now  = new Date()
      const time = now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
      const date = now.toLocaleDateString('en-IN', {
        timeZone: 'Asia/Kolkata', month: 'short', day: '2-digit',
      }).toUpperCase().replace(/\//g, ' ')
      setDisplay({ time, date })
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])
  return (
    <div style={{ fontFamily: T.mono, fontSize: 14, fontWeight: 500, letterSpacing: '0.08em', color: T.ink0 }}>
      {display.time}
      <span style={{ fontSize: 9, color: T.ink2, marginLeft: 8, letterSpacing: '0.2em' }}>
        IST · {display.date}
      </span>
    </div>
  )
}

function MarketBadge({ label, status }) {
  const isOpen  = status === 'OPEN'
  const isPre   = status === 'PRE'
  const color   = isOpen ? T.green : isPre ? T.amber : T.ink3
  const bg      = isOpen ? 'oklch(0.45 0.16 145 / 0.15)' : isPre ? 'oklch(0.45 0.12 75 / 0.15)' : T.bg2
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      fontFamily: T.mono, fontSize: 9, padding: '4px 8px',
      border: `1px solid ${isOpen ? T.greenD : isPre ? 'oklch(0.45 0.12 75)' : T.line}`,
      background: bg, letterSpacing: '0.14em', textTransform: 'uppercase', color,
    }}>
      <span style={{
        width: 5, height: 5, borderRadius: '50%', background: color, flexShrink: 0,
        animation: isOpen ? 'pulse 2s ease-in-out infinite' : 'none',
      }} />
      {label} <span style={{ opacity: 0.7 }}>{status || '—'}</span>
    </div>
  )
}

function Pill({ children, color }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      fontFamily: T.mono, fontSize: 10, padding: '5px 9px',
      border: `1px solid ${T.line2}`, color: T.ink1,
      textTransform: 'uppercase', letterSpacing: '0.12em', background: T.bg2,
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: color || T.green, flexShrink: 0 }} />
      {children}
    </div>
  )
}

export default function TopBar({ status, halted }) {
  const d = status?.data
  const mode      = d?.mode || 'PAPER'
  const stratName = d?.strategy_name || '—'

  const [market, setMarket] = useState(null)
  useEffect(() => {
    async function fetchMarket() {
      try {
        const r = await fetch('/api/market')
        if (r.ok) setMarket(await r.json())
      } catch {}
    }
    fetchMarket()
    const id = setInterval(fetchMarket, 30000)
    return () => clearInterval(id)
  }, [])

  return (
    <div>
      {/* Market status bar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '6px 0', borderBottom: `1px solid ${T.line}`,
        fontSize: 9, fontFamily: T.mono,
      }}>
        <span style={{ color: T.ink3, letterSpacing: '0.16em', textTransform: 'uppercase', marginRight: 4 }}>MARKETS</span>
        <MarketBadge label="NSE EQ"  status={market?.nse_equity} />
        <MarketBadge label="NSE F&O" status={market?.nse_fno} />
        <MarketBadge label="MCX"     status={market?.mcx} />
        {market?.is_weekend && (
          <span style={{ color: T.ink3, fontFamily: T.mono, fontSize: 9, letterSpacing: '0.14em', marginLeft: 4 }}>
            WEEKEND · ALL MARKETS CLOSED
          </span>
        )}
        <div style={{ flex: 1 }} />
        <span style={{ color: T.ink3 }}>
          {market?.weekday?.toUpperCase()} · {market?.ist_time} IST
        </span>
      </div>

      {/* Main top bar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 18,
        padding: '12px 0', borderBottom: `1px solid ${T.line}`,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontWeight: 600, fontSize: 13, letterSpacing: '0.04em' }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: halted ? T.red : T.green,
            boxShadow: `0 0 8px ${halted ? T.red : T.green}`,
            animation: 'pulse 1.6s ease-in-out infinite', flexShrink: 0,
          }} />
          DHAN · ALGO
          <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.2em' }}>
            v1.0 · cockpit
          </span>
        </div>

        <div style={{ fontFamily: T.mono, fontSize: 11, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.18em', display: 'flex', gap: 14 }}>
          <span style={{ color: T.ink0 }}>{mode} MODE</span>
          <span style={{ color: T.ink3 }}>·</span>
          <span>{stratName}</span>
          <span style={{ color: T.ink3 }}>·</span>
          <span>DHAN v2</span>
        </div>

        <div style={{ flex: 1 }} />
        <Pill color={halted ? T.red : T.green}>{halted ? 'HALTED' : 'SESSION OPEN'}</Pill>
        <Pill color={T.amber}>RSI SCALPER</Pill>
        <Clock />
      </div>
    </div>
  )
}
