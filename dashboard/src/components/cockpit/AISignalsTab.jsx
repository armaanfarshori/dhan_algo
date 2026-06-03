import { T, fmtTime } from '../../tokens'

const SIDE_META = {
  BUY:  { icon: '📈', color: T.green,  bg: T.greenD },
  SELL: { icon: '📉', color: T.red,    bg: T.redD   },
  HOLD: { icon: '➖', color: T.ink2,   bg: T.bg3    },
}

function ConfBar({ value }) {
  const pct = Math.round((value ?? 0) * 100)
  const color = pct >= 60 ? T.green : pct >= 40 ? T.amber : T.ink3
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ flex: 1, height: 4, background: T.bg3, position: 'relative' }}>
        <div style={{ position: 'absolute', inset: 0, width: `${pct}%`, background: color, transition: 'width 0.4s' }} />
      </div>
      <span style={{ fontFamily: T.mono, fontSize: 10, color, minWidth: 28 }}>{pct}%</span>
    </div>
  )
}

function SignalRow({ s, idx }) {
  const meta = SIDE_META[s.side] ?? SIDE_META.HOLD
  const ret  = s.features?.forecast_return ?? 0
  const retSign = ret >= 0 ? '+' : ''
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: '28px 80px 64px 1fr 80px',
      gap: 10, padding: '9px 16px', alignItems: 'center',
      background: idx % 2 === 0 ? T.bg1 : T.bg2,
      borderBottom: `1px solid ${T.line}`,
    }}>
      <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>{String(idx + 1).padStart(2, '0')}</span>
      <span style={{ fontFamily: T.mono, fontSize: 12, color: T.ink0 }}>{s.security_id}</span>
      <span style={{
        fontFamily: T.mono, fontSize: 11, fontWeight: 600,
        color: meta.color, background: meta.bg,
        padding: '2px 7px', display: 'inline-block',
      }}>
        {meta.icon} {s.side}
      </span>
      <ConfBar value={s.confidence} />
      <span style={{
        fontFamily: T.mono, fontSize: 11,
        color: ret >= 0 ? T.green : T.red, textAlign: 'right',
      }}>
        {retSign}{(ret * 100).toFixed(3)}%
      </span>
    </div>
  )
}

function ScreenerTable({ candidates }) {
  if (!candidates?.length) {
    return (
      <div style={{ padding: '32px 16px', textAlign: 'center', color: T.ink2, fontFamily: T.mono, fontSize: 11 }}>
        No screener data — run Kronos forecast first
        <div style={{ marginTop: 8, fontSize: 10, color: T.ink3 }}>
          Run: python hermes_skills/dhan/kronos_forecast/scripts/forecast.py
        </div>
      </div>
    )
  }
  return (
    <div>
      <div style={{
        display: 'grid', gridTemplateColumns: '28px 80px 64px 1fr 80px',
        gap: 10, padding: '7px 16px',
        fontFamily: T.mono, fontSize: 9, color: T.ink3,
        textTransform: 'uppercase', letterSpacing: '0.18em',
        borderBottom: `1px solid ${T.line}`,
      }}>
        <span>#</span><span>SECURITY</span><span>SIGNAL</span>
        <span>CONFIDENCE</span><span style={{ textAlign: 'right' }}>FORECAST Δ</span>
      </div>
      {candidates.map((s, i) => <SignalRow key={s.security_id} s={s} idx={i} />)}
    </div>
  )
}

function SignalHistory({ signals }) {
  if (!signals?.length) return null
  const buys  = signals.filter(s => s.side === 'BUY').length
  const sells = signals.filter(s => s.side === 'SELL').length
  const holds = signals.filter(s => s.side === 'HOLD').length
  const last  = signals[0]
  return (
    <div style={{ padding: '10px 16px', display: 'flex', gap: 20, alignItems: 'center', borderTop: `1px solid ${T.line}` }}>
      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>HISTORY</span>
      <span style={{ fontFamily: T.dot, fontSize: 18, color: T.green }}>{buys}</span>
      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>BUY</span>
      <span style={{ fontFamily: T.dot, fontSize: 18, color: T.red  }}>{sells}</span>
      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>SELL</span>
      <span style={{ fontFamily: T.dot, fontSize: 18, color: T.ink2 }}>{holds}</span>
      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>HOLD</span>
      {last && (
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginLeft: 'auto' }}>
          Last: {fmtTime(last.ts)} · {last.strategy}
        </span>
      )}
    </div>
  )
}

export default function AISignalsTab({ screener, signals }) {
  const candidates = screener?.data?.candidates ?? []
  const signalRows = signals?.data?.signals ?? []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: '20px 24px 60px' }}>

      {/* Screener + Kronos table */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
              NSE SCREENER + KRONOS AI
            </span>
            {candidates.length > 0 && (
              <span style={{ background: T.greenD, color: T.green, fontFamily: T.mono, fontSize: 9, padding: '2px 6px', letterSpacing: '0.2em' }}>
                TOP {candidates.length}
              </span>
            )}
          </div>
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>ATR-ranked · 30d lookback</span>
        </div>
        <ScreenerTable candidates={candidates} />
        <SignalHistory signals={signalRows} />
      </div>

      {/* Signal summary stats */}
      {signalRows.length > 0 && (
        <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 10 }}>
            RECENT SIGNALS (DB)
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 240, overflowY: 'auto' }}>
            {signalRows.slice(0, 30).map((s, i) => {
              const meta = SIDE_META[s.side] ?? SIDE_META.HOLD
              return (
                <div key={i} style={{ display: 'flex', gap: 12, fontFamily: T.mono, fontSize: 10, padding: '3px 0', borderBottom: `1px solid ${T.line}` }}>
                  <span style={{ color: T.ink3, minWidth: 55 }}>{fmtTime(s.ts)}</span>
                  <span style={{ color: T.ink1, minWidth: 60 }}>{s.security_id}</span>
                  <span style={{ color: meta.color, minWidth: 36 }}>{s.side}</span>
                  <span style={{ color: T.ink2 }}>conf={(s.confidence * 100).toFixed(0)}%</span>
                  <span style={{ color: T.ink3, marginLeft: 'auto' }}>{s.strategy}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
