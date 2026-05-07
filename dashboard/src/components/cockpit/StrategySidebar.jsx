import { useState } from 'react'
import { T, INR } from '../../tokens'

const ALL_SEGMENTS = [
  { value: 'NSE_FNO',  label: 'NSE F&O', desc: 'NIFTY · BANKNIFTY · FINNIFTY · NIFTYNXT50 · MIDCPNIFTY' },
  { value: 'BSE_FNO',  label: 'BSE F&O', desc: 'SENSEX' },
  { value: 'NSE_EQ',   label: 'NSE Equity', desc: 'Top 15 movers by volume' },
  { value: 'MCX_COMM', label: 'MCX Commodity', desc: 'Top commodity futures' },
]

export default function StrategySidebar({ config, scanner, onSwitch }) {
  const [segments, setSegments] = useState(['NSE_FNO', 'BSE_FNO'])
  const [capitalPct, setCapPct] = useState(70)
  const [msg, setMsg]           = useState(null)

  const sc = scanner?.data

  async function toggleSegment(val) {
    const next = segments.includes(val)
      ? (segments.length > 1 ? segments.filter(s => s !== val) : segments)
      : [...segments, val]
    setSegments(next)
    try {
      const r = await fetch('/api/scanner/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segments: next, capital_pct: capitalPct / 100 }),
      })
      const d = await r.json()
      setMsg(d.ok ? null : d.error)
    } catch (e) { setMsg(String(e)) }
  }

  async function applyCapital(pct) {
    setCapPct(pct)
    try {
      await fetch('/api/scanner/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capital_pct: pct / 100 }),
      })
    } catch {}
  }

  const label = { fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.18em', textTransform: 'uppercase' }

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ color: T.ink0 }}>SCANNER CONTROLS</span>
        {msg && <span style={{ fontSize: 9, color: T.red }}>{msg}</span>}
      </div>

      {/* Live scanner status */}
      {sc?.ok && (
        <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, background: T.bg2 }}>
          <div style={{ ...label, marginBottom: 8 }}>STATUS</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            {[
              ['MODE',     sc.mode?.replace('_',' ').toUpperCase() || '—', T.cyan],
              ['BALANCE',  sc.balance ? INR(sc.balance) : '—',             T.green],
              ['POSITIONS',`${sc.open_positions ?? 0} OPEN`,               sc.open_positions > 0 ? T.amber : T.ink2],
              ['ORDERS',   `${sc.orders_placed ?? 0} PLACED`,              T.ink0],
            ].map(([k, v, c]) => (
              <div key={k} style={{ background: T.bg3, border: `1px solid ${T.line}`, padding: '6px 8px' }}>
                <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.16em', marginBottom: 3 }}>{k}</div>
                <div style={{ fontFamily: T.mono, fontSize: 10, color: c, fontWeight: 600 }}>{v}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Segment toggles */}
      <div style={{ padding: '12px 14px', borderBottom: `1px solid ${T.line}` }}>
        <div style={{ ...label, marginBottom: 10 }}>ACTIVE SEGMENTS</div>
        {ALL_SEGMENTS.map(seg => {
          const active = segments.includes(seg.value)
          return (
            <div key={seg.value} onClick={() => toggleSegment(seg.value)} style={{
              display: 'flex', alignItems: 'flex-start', gap: 10,
              padding: '9px 10px', marginBottom: 6, cursor: 'pointer',
              background: active ? 'oklch(0.18 0.08 145 / 0.2)' : T.bg2,
              border: `1px solid ${active ? T.green : T.line}`,
              transition: 'all .15s',
            }}>
              <div style={{
                width: 14, height: 14, border: `1px solid ${active ? T.green : T.line2}`,
                background: active ? T.green : 'transparent', flexShrink: 0, marginTop: 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {active && <span style={{ color: T.bg0, fontSize: 10, lineHeight: 1 }}>✓</span>}
              </div>
              <div>
                <div style={{ fontFamily: T.mono, fontSize: 11, color: active ? T.ink0 : T.ink2, fontWeight: active ? 600 : 400 }}>
                  {seg.label}
                </div>
                <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginTop: 2, letterSpacing: '0.06em' }}>
                  {seg.desc}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {/* Capital slider */}
      <div style={{ padding: '12px 14px', borderBottom: `1px solid ${T.line}` }}>
        <div style={{ ...label, display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <span>CAPITAL PER TRADE</span>
          <span style={{ color: T.cyan }}>{capitalPct}%</span>
        </div>
        <input type="range" min={10} max={90} step={5} value={capitalPct}
          onChange={e => applyCapital(Number(e.target.value))}
          style={{ width: '100%', accentColor: T.green, cursor: 'pointer' }} />
        <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 5, letterSpacing: '0.1em' }}>
          QUANTITY AUTO-CALCULATED FROM ACCOUNT BALANCE
        </div>
      </div>

      {/* Active indices live RSI */}
      {sc?.indices && (
        <div style={{ padding: '12px 14px' }}>
          <div style={{ ...label, marginBottom: 10 }}>LIVE RSI PER INDEX</div>
          {Object.entries(sc.indices).map(([name, idx]) => {
            const rsi   = idx.rsi || 0
            const inPos = idx.in_position
            const rsiColor = rsi > 0 && rsi <= 30 ? T.green : rsi >= 70 ? T.red : T.amber
            const pct   = rsi
            return (
              <div key={name} style={{ marginBottom: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: T.mono, fontSize: 9, marginBottom: 3 }}>
                  <span style={{ color: inPos ? T.green : T.ink2, fontWeight: inPos ? 600 : 400 }}>
                    {inPos ? '● ' : ''}{name}
                    {inPos && <span style={{ color: T.amber, marginLeft: 6 }}>{idx.option_type} {idx.strike?.toLocaleString('en-IN')}</span>}
                  </span>
                  <span style={{ color: rsiColor, fontFamily: T.dot, fontSize: 14 }}>
                    {rsi > 0 ? rsi.toFixed(1) : '—'}
                  </span>
                </div>
                <div style={{ height: 4, background: T.bg3, borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${pct}%`, background: rsiColor, borderRadius: 2, transition: 'width .4s, background .3s' }} />
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
