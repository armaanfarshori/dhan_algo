import { useState } from 'react'
import { T, INR } from '../../tokens'
import LiveLogs from './LiveLogs'

const ALL_SEGMENTS = [
  { value: 'NSE_FNO',  label: 'NSE F&O',        desc: 'NIFTY · BANKNIFTY · FINNIFTY · NIFTYNXT50 · MIDCPNIFTY' },
  { value: 'BSE_FNO',  label: 'BSE F&O',         desc: 'SENSEX' },
  { value: 'NSE_EQ',   label: 'NSE Equity',      desc: 'Pre-market lock · NIFTY 50 + Movers' },
  { value: 'MCX_COMM', label: 'MCX Commodity',   desc: 'Top commodity futures' },
]

function WarmupBar({ label, current, required, pollSecs, color }) {
  const pct      = required > 0 ? Math.min(current / required * 100, 100) : 100
  const secsLeft = Math.max(0, (required - current)) * pollSecs
  const ready    = current >= required

  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: T.mono, fontSize: 9, color: T.ink2, marginBottom: 3 }}>
        <span style={{ color: ready ? T.green : T.ink2 }}>
          {ready ? '● ' : '◌ '}{label}
        </span>
        {ready
          ? <span style={{ color: T.green }}>READY</span>
          : <span style={{ color: T.amber }}>
              {secsLeft >= 60
                ? `${Math.ceil(secsLeft / 60)}m left`
                : `${secsLeft}s left`}
            </span>
        }
      </div>
      <div style={{ height: 4, background: T.bg3, borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%', width: `${pct}%`,
          background: ready ? T.green : color || T.blue,
          borderRadius: 2, transition: 'width .5s ease',
        }} />
      </div>
      {!ready && (
        <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 2 }}>
          {current}/{required} bars · {pollSecs}s interval
        </div>
      )}
    </div>
  )
}

export default function StrategySidebar({ config, scanner, fnoScanner, equityScanner, logs, onSwitch }) {
  const [segments, setSegments] = useState(['NSE_FNO', 'BSE_FNO', 'NSE_EQ'])
  const [msg, setMsg]           = useState(null)

  const fnoSc  = fnoScanner?.data
  const eqSc   = equityScanner?.data

  // F&O warmup per index (RSI-14 → needs 15 ticks at 10s)
  const fnoIndices = fnoSc?.indices || {}
  const fnoWarmups = Object.entries(fnoIndices).map(([name, idx]) => ({
    name,
    current:  Math.min(idx.rsi > 0 ? 15 : Math.round((idx.price > 0 ? 14 : 0)), 15),
    required: 15,
    pollSecs: 10,
    ready:    idx.rsi > 0,
  }))

  // Equity warmup per stock
  const eqWarmup  = eqSc?.warmup || {}
  const eqEntries = Object.entries(eqWarmup).slice(0, 5)

  async function toggleSegment(val) {
    const next = segments.includes(val)
      ? (segments.length > 1 ? segments.filter(s => s !== val) : segments)
      : [...segments, val]
    setSegments(next)
    try {
      const r = await fetch('/api/scanner/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segments: next }),
      })
      const d = await r.json()
      setMsg(d.ok ? null : d.error)
    } catch (e) { setMsg(String(e)) }
  }

  const label = { fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 8 }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Header */}
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em', display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ color: T.ink0 }}>CONTROLS</span>
        {msg && <span style={{ fontSize: 9, color: T.red }}>{msg}</span>}
      </div>

      {/* Dual engine status */}
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, background: T.bg2 }}>
        <div style={{ ...label, marginBottom: 8 }}>RUNNING ENGINES</div>
        {[
          { tag: 'F&O', color: T.green, pos: fnoSc?.open_positions ?? 0, orders: fnoSc?.orders_placed ?? 0, desc: 'NIFTY·BANKNIFTY·SENSEX·FINNIFTY·NIFTYNXT50·MIDCPNIFTY' },
          { tag: 'EQ',  color: T.cyan,  pos: eqSc?.open_positions  ?? 0, orders: eqSc?.orders_placed  ?? 0, desc: `${eqSc?.strategy_key?.toUpperCase() || 'MOMENTUM_BREAKOUT'} · NSE TOP 30` },
        ].map(e => (
          <div key={e.tag} style={{ background: T.bg3, border: `1px solid ${T.line}`, padding: '7px 10px', marginBottom: 6 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: e.color, letterSpacing: '0.12em' }}>● {e.tag}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>{e.pos} pos · {e.orders} orders</span>
            </div>
            <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.06em' }}>{e.desc}</div>
          </div>
        ))}
      </div>

      {/* Segment toggles */}
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}` }}>
        <div style={label}>ACTIVE SEGMENTS</div>
        {ALL_SEGMENTS.map(seg => {
          const active = segments.includes(seg.value)
          return (
            <div key={seg.value} onClick={() => toggleSegment(seg.value)} style={{
              display: 'flex', alignItems: 'flex-start', gap: 10,
              padding: '8px 10px', marginBottom: 5, cursor: 'pointer',
              background: active ? 'oklch(0.18 0.08 145 / 0.2)' : T.bg2,
              border: `1px solid ${active ? T.green : T.line}`,
              transition: 'all .15s',
            }}>
              <div style={{
                width: 13, height: 13, border: `1px solid ${active ? T.green : T.line2}`,
                background: active ? T.green : 'transparent', flexShrink: 0, marginTop: 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {active && <span style={{ color: '#000', fontSize: 9, lineHeight: 1 }}>✓</span>}
              </div>
              <div>
                <div style={{ fontFamily: T.mono, fontSize: 10, color: active ? T.ink0 : T.ink2, fontWeight: active ? 600 : 400 }}>{seg.label}</div>
                <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 1, letterSpacing: '0.06em' }}>{seg.desc}</div>
              </div>
            </div>
          )
        })}
      </div>

      {/* Warmup countdowns — F&O */}
      {fnoWarmups.length > 0 && (
        <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}` }}>
          <div style={label}>F&O RSI WARMUP</div>
          {fnoWarmups.map(w => (
            <WarmupBar key={w.name} label={w.name} current={w.current} required={w.required} pollSecs={w.pollSecs} ready={w.ready} color={T.green} />
          ))}
        </div>
      )}

      {/* Warmup countdowns — Equity */}
      {eqEntries.length > 0 && (
        <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}` }}>
          <div style={label}>EQ STRATEGY WARMUP</div>
          {eqEntries.map(([sid, w]) => (
            <WarmupBar key={sid} label={sid} current={w.current} required={w.required} pollSecs={w.secs_left / Math.max(w.required - w.current, 1) || 60} ready={w.ready} color={T.cyan} />
          ))}
          {eqEntries.length < Object.keys(eqWarmup).length && (
            <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, textAlign: 'center', marginTop: 4 }}>
              +{Object.keys(eqWarmup).length - eqEntries.length} more stocks…
            </div>
          )}
        </div>
      )}

      {/* Live logs — fills remaining space */}
      <div style={{ flex: 1, overflow: 'hidden', minHeight: 0 }}>
        <LiveLogs logs={logs} />
      </div>
    </div>
  )
}
