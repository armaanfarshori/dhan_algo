import { useState } from 'react'
import { T, INR } from '../../tokens'

const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber }

export default function WatchlistPanel({ watchlist, scanner }) {
  const [refreshing, setRefreshing] = useState(false)
  const [msg, setMsg] = useState(null)

  const wl = watchlist?.data
  const sc = scanner?.data
  const stocks = wl?.stocks ?? []

  async function refresh() {
    setRefreshing(true); setMsg(null)
    try {
      const r = await fetch('/api/watchlist/refresh', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } })
      const d = await r.json()
      setMsg(d.ok ? `Refreshed — ${d.count} stocks` : d.error)
    } catch (e) { setMsg(String(e)) }
    finally { setRefreshing(false) }
  }

  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, marginBottom: 14 }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em',
      }}>
        <span style={{ background: T.greenD, color: T.green, padding: '2px 6px', fontSize: 9 }}>NSE</span>
        <span style={{ color: T.ink0 }}>TOP MOVERS · AUTO WATCHLIST</span>
        <span style={{ color: T.ink2, fontSize: 9 }}>{stocks.length} stocks</span>
        {wl?.last_refresh && (
          <span style={{ color: T.ink3, fontSize: 9 }}>
            · refreshed {new Date(wl.last_refresh).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {msg && <span style={{ color: T.amber, fontSize: 9 }}>{msg}</span>}
        <button onClick={refresh} disabled={refreshing} style={{
          background: 'transparent', border: `1px solid ${T.line2}`, color: T.ink2,
          fontFamily: T.mono, fontSize: 9, padding: '3px 10px', cursor: 'pointer',
          letterSpacing: '0.14em', textTransform: 'uppercase',
        }}>
          {refreshing ? 'REFRESHING…' : '⟳ REFRESH'}
        </button>
        {sc?.open_positions > 0 && (
          <span style={{ fontFamily: T.mono, fontSize: 9, color: T.cyan }}>
            {sc.open_positions} POSITIONS OPEN
          </span>
        )}
      </div>

      {/* Scanner config strip */}
      {sc?.ok && (
        <div style={{
          display: 'flex', gap: 20, padding: '8px 14px', borderBottom: `1px solid ${T.line}`,
          fontFamily: T.mono, fontSize: 9, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.14em',
          background: T.bg2, flexWrap: 'wrap',
        }}>
          <span>STRATEGY <b style={{ color: T.cyan }}>{sc.strategy_key?.toUpperCase()}</b></span>
          <span>SEGMENTS <b style={{ color: T.ink0 }}>{(sc.segments || []).join(' + ')}</b></span>
          <span>CAPITAL <b style={{ color: T.green }}>{Math.round((sc.capital_pct || 0.7) * 100)}%</b></span>
          <span>MAX POS <b style={{ color: T.ink0 }}>{sc.open_positions ?? 0} / {sc.max_positions ?? 5}</b></span>
          <span>HEDGE <b style={{ color: sc.hedge_fno ? T.green : T.ink3 }}>{sc.hedge_fno ? 'ON' : 'OFF'}</b></span>
          {sc.available_balance > 0 && (
            <span>BALANCE <b style={{ color: T.green }}>{INR(sc.available_balance)}</b></span>
          )}
        </div>
      )}

      {/* Stocks table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['SYMBOL', 'NAME', 'LTP', 'CHG %', 'VOLUME', 'SOURCE', 'SIGNAL'].map(h => (
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
              return (
                <tr key={i} style={{
                  borderBottom: `1px solid ${T.line}`,
                  background: sig === 'BUY' ? 'oklch(0.18 0.08 145 / 0.1)' :
                              sig === 'SELL' ? 'oklch(0.18 0.08 25 / 0.1)' : 'transparent',
                }}>
                  <td style={{ padding: '8px 12px', fontFamily: T.mono, fontSize: 11, color: T.cyan, fontWeight: 600 }}>{s.symbol}</td>
                  <td style={{ padding: '8px 12px', fontFamily: T.mono, fontSize: 10, color: T.ink2, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</td>
                  <td style={{ padding: '8px 12px', fontFamily: T.dot, fontSize: 18, color: T.ink0 }}>{s.ltp ? `₹${s.ltp.toLocaleString('en-IN')}` : '—'}</td>
                  <td style={{ padding: '8px 12px', fontFamily: T.mono, fontSize: 11, color: isPos ? T.green : T.red, fontWeight: 600 }}>
                    {isPos ? '+' : ''}{s.change_pct.toFixed(2)}%
                  </td>
                  <td style={{ padding: '8px 12px', fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
                    {s.volume > 0 ? (s.volume / 1e6).toFixed(2) + 'M' : '—'}
                  </td>
                  <td style={{ padding: '8px 12px', fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.12em', textTransform: 'uppercase' }}>
                    {s.source.replace('_', ' ')}
                  </td>
                  <td style={{ padding: '8px 12px' }}>
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
