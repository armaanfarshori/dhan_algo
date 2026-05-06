import { useState, useEffect } from 'react'
import { T } from '../../tokens'

function Clock() {
  const [display, setDisplay] = useState({ time: '', date: '' })

  useEffect(() => {
    function tick() {
      const now = new Date()
      const time = now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
      const date = now.toLocaleDateString('en-IN', {
        timeZone: 'Asia/Kolkata', month: 'short', day: '2-digit'
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
  const mode = d?.mode || 'PAPER'
  const stratName = d?.strategy_name || '—'

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 18,
      padding: '14px 0', borderBottom: `1px solid ${T.line}`,
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
  )
}
