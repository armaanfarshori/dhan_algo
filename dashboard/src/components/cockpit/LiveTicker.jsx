import { T } from '../../tokens'

export default function LiveTicker({ fnoScanner, equityScanner }) {
  const sc  = fnoScanner?.data
  const eq  = equityScanner?.data
  const idx = sc?.indices || {}

  const indices = Object.entries(idx).filter(([, v]) => v.price > 0)
  const eqSignals = eq?.latest_signals || []

  if (!indices.length && !eqSignals.length) return null

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 0,
      borderBottom: `1px solid ${T.line}`, marginBottom: 18,
      background: T.bg1, overflowX: 'auto',
    }}>
      {/* Index prices */}
      {indices.map(([name, v]) => {
        const isPos = v.change_pct >= 0
        const inPos = v.in_position
        return (
          <div key={name} style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 16px', borderRight: `1px solid ${T.line}`,
            flexShrink: 0,
            background: inPos ? 'oklch(0.18 0.08 145 / 0.15)' : 'transparent',
          }}>
            <div>
              <div style={{ fontFamily: 'JetBrains Mono', fontSize: 9, color: inPos ? T.green : T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase' }}>
                {inPos && '● '}{name}
              </div>
              <div style={{ fontFamily: 'VT323', fontSize: 20, color: T.ink0, lineHeight: 1 }}>
                {v.price > 0 ? v.price.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}
              </div>
            </div>
            {v.change_pct !== 0 && (
              <div style={{ fontFamily: 'JetBrains Mono', fontSize: 9, color: isPos ? T.green : T.red, fontWeight: 600 }}>
                {isPos ? '▲' : '▼'} {Math.abs(v.change_pct).toFixed(2)}%
              </div>
            )}
            {/* RSI badge */}
            {v.rsi > 0 && (
              <div style={{
                fontFamily: 'JetBrains Mono', fontSize: 8, padding: '1px 5px',
                background: v.rsi <= 30 ? 'oklch(0.45 0.16 145 / 0.2)' : v.rsi >= 70 ? 'oklch(0.42 0.18 25 / 0.2)' : T.bg3,
                color: v.rsi <= 30 ? T.green : v.rsi >= 70 ? T.red : T.ink3,
                border: `1px solid ${v.rsi <= 30 ? T.green : v.rsi >= 70 ? T.red : T.line}`,
                letterSpacing: '0.1em',
              }}>
                RSI {v.rsi.toFixed(0)}
              </div>
            )}
          </div>
        )
      })}

      {/* Equity top movers divider */}
      {eqSignals.length > 0 && (
        <>
          <div style={{ padding: '8px 12px', borderRight: `1px solid ${T.line}`, fontFamily: 'JetBrains Mono', fontSize: 8, color: T.ink3, letterSpacing: '0.2em', textTransform: 'uppercase', flexShrink: 0 }}>
            EQ SIGNALS
          </div>
          {eqSignals.slice(0, 5).map((s, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRight: `1px solid ${T.line}`, flexShrink: 0 }}>
              <span style={{ fontFamily: 'JetBrains Mono', fontSize: 9, color: 'oklch(0.82 0.12 200)', letterSpacing: '0.1em' }}>{s.symbol}</span>
              <span style={{ fontFamily: 'JetBrains Mono', fontSize: 9, color: s.action === 'BUY' ? T.green : T.red, fontWeight: 600 }}>{s.action}</span>
              <span style={{ fontFamily: 'VT323', fontSize: 16, color: T.ink0 }}>₹{s.price?.toLocaleString('en-IN')}</span>
            </div>
          ))}
        </>
      )}

      <div style={{ flex: 1 }} />
      <div style={{ padding: '8px 14px', fontFamily: 'JetBrains Mono', fontSize: 8, color: T.ink3, letterSpacing: '0.14em', flexShrink: 0 }}>
        {sc?.mode === 'index_options' ? '● F&O SCANNING' : '○'} &nbsp;
        {eq ? '● EQ SCANNING' : '○'}
      </div>
    </div>
  )
}
