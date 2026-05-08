import { T } from '../../tokens'

const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber }

function SmaGauge({ sid, stockSignals }) {
  const s = stockSignals?.[sid]
  if (!s) return <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3 }}>—</span>

  if (!s.warmed_up) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <div style={{ flex: 1, height: 3, background: T.line, borderRadius: 2, overflow: 'hidden' }}>
          <div style={{ height: '100%', width: '30%', background: T.ink3, borderRadius: 2, animation: 'pulse 1.5s ease-in-out infinite' }} />
        </div>
        <span style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, whiteSpace: 'nowrap' }}>WARM</span>
      </div>
    )
  }

  const gap     = s.gap_pct || 0
  const isBull  = gap > 0
  const color   = s.in_position ? T.cyan : isBull ? T.green : T.red
  // Scale: ±1% = full bar
  const barPct  = Math.min(Math.abs(gap) / 0.5 * 100, 100)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      {/* Bearish side */}
      <div style={{ width: 36, display: 'flex', justifyContent: 'flex-end' }}>
        {!isBull && (
          <div style={{ height: 4, width: `${barPct}%`, background: T.red, borderRadius: '2px 0 0 2px', transition: 'width .4s' }} />
        )}
      </div>
      {/* Centre line */}
      <div style={{ width: 1, height: 10, background: T.line2, flexShrink: 0 }} />
      {/* Bullish side */}
      <div style={{ width: 36 }}>
        {isBull && (
          <div style={{ height: 4, width: `${barPct}%`, background: T.green, borderRadius: '0 2px 2px 0', transition: 'width .4s' }} />
        )}
      </div>
      {s.signal && (
        <span style={{ fontFamily: T.mono, fontSize: 8, color, fontWeight: 600, letterSpacing: '0.1em', width: 28 }}>
          {s.signal}
        </span>
      )}
    </div>
  )
}

export default function WatchlistPanel({ watchlist, scanner, equityScanner }) {
  const wl          = watchlist?.data
  const stocks      = wl?.stocks ?? []
  const stockSignals = equityScanner?.data?.stock_signals || {}

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, marginBottom: 14 }}>
      {/* Header — no refresh button */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em',
      }}>
        <span style={{ background: T.greenD, color: T.green, padding: '2px 6px', fontSize: 9 }}>NSE</span>
        <span style={{ color: T.ink0 }}>PRE-MARKET LOCK · SESSION LIST</span>
        <span style={{ color: T.ink2, fontSize: 9 }}>{stocks.length} stocks</span>
        {wl?.last_refresh && (
          <span style={{ color: T.ink3, fontSize: 9 }}>
            · locked {new Date(wl.last_refresh).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>REFRESHES 08:30 IST DAILY</span>
      </div>

      {/* Stocks table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['SYMBOL', 'NAME', 'LTP', 'CHG %', 'VOLUME', 'SMA SIGNAL', 'ACTION'].map(h => (
                <th key={h} style={{
                  textAlign: 'left', fontFamily: T.mono, fontSize: 9, color: T.ink2,
                  padding: '6px 12px', borderBottom: `1px solid ${T.line}`,
                  textTransform: 'uppercase', letterSpacing: '0.12em',
                  background: T.bg1, position: 'sticky', top: 0,
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {stocks.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: '20px 12px', fontFamily: T.mono, fontSize: 10, color: T.ink3, letterSpacing: '0.14em' }}>
                LOADING TOP MOVERS FROM NSE…
              </td></tr>
            ) : stocks.map((s, i) => {
              const isPos = s.change_pct > 0
              const sig   = s.signal
              const ss    = stockSignals[s.security_id]
              return (
                <tr key={i} style={{
                  borderBottom: `1px solid ${T.line}`,
                  background: ss?.in_position ? 'oklch(0.18 0.08 200 / 0.1)' :
                              sig === 'BUY'   ? 'oklch(0.18 0.08 145 / 0.06)' :
                              sig === 'SELL'  ? 'oklch(0.18 0.08 25  / 0.06)' : 'transparent',
                }}>
                  <td style={{ padding: '7px 12px', fontFamily: T.mono, fontSize: 11, color: ss?.in_position ? T.cyan : T.cyan, fontWeight: 600 }}>
                    {ss?.in_position && <span style={{ color: T.green, marginRight: 4 }}>●</span>}
                    {s.symbol}
                  </td>
                  <td style={{ padding: '7px 12px', fontFamily: T.mono, fontSize: 10, color: T.ink2, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</td>
                  <td style={{ padding: '7px 12px', fontFamily: 'VT323', fontSize: 18, color: T.ink0 }}>{s.ltp ? `₹${s.ltp.toLocaleString('en-IN')}` : '—'}</td>
                  <td style={{ padding: '7px 12px', fontFamily: T.mono, fontSize: 11, color: isPos ? T.green : T.red, fontWeight: 600 }}>
                    {isPos ? '+' : ''}{s.change_pct.toFixed(2)}%
                  </td>
                  <td style={{ padding: '7px 12px', fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
                    {s.volume > 0 ? (s.volume / 1e6).toFixed(2) + 'M' : '—'}
                  </td>
                  <td style={{ padding: '7px 12px', minWidth: 140 }}>
                    <SmaGauge sid={s.security_id} stockSignals={stockSignals} />
                  </td>
                  <td style={{ padding: '7px 12px' }}>
                    {sig ? (
                      <span style={{
                        fontFamily: T.mono, fontSize: 9, fontWeight: 600,
                        color: ACTION_COLOR[sig] || T.ink2,
                        background: sig === 'BUY' ? 'oklch(0.45 0.16 145 / 0.15)' :
                                   sig === 'SELL' ? 'oklch(0.42 0.18 25 / 0.15)' : 'transparent',
                        padding: '2px 6px', letterSpacing: '0.12em',
                        border: `1px solid ${ACTION_COLOR[sig] || T.line}`,
                      }}>
                        {sig}
                      </span>
                    ) : (
                      <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
