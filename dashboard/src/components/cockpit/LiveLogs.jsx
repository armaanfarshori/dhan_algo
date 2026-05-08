import { useEffect, useRef } from 'react'
import { T } from '../../tokens'

const LEVEL_COLOR = {
  INFO:     T.ink2,
  WARNING:  T.amber,
  ERROR:    T.red,
  CRITICAL: T.red,
  DEBUG:    T.ink3,
}
const NAME_COLOR = {
  'index_options': T.green,
  'scanner':       T.cyan,
  'trade_log':     T.amber,
  'risk':          T.red,
  'auth':          T.violet,
  'watchlist':     T.ink1,
  'live_feed':     'oklch(0.82 0.12 200)',
  'instruments':   T.ink2,
}

export default function LiveLogs({ logs }) {
  const bottomRef = useRef(null)
  const entries   = logs?.data?.logs ?? []

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [entries.length])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{
        padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase',
        letterSpacing: '0.16em', display: 'flex', justifyContent: 'space-between',
      }}>
        <span style={{ color: T.ink0 }}>LIVE ACTIVITY</span>
        <span style={{ fontSize: 9, color: T.ink3 }}>{entries.length} entries</span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 0', fontFamily: T.mono, fontSize: 10 }}>
        {entries.length === 0 ? (
          <div style={{ padding: '20px 14px', color: T.ink3, letterSpacing: '0.12em' }}>WAITING FOR EVENTS…</div>
        ) : (
          entries.map((e, i) => {
            const nameColor = NAME_COLOR[e.name] || T.ink2
            const lvlColor  = LEVEL_COLOR[e.level] || T.ink2
            const ts        = new Date(e.ts).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })
            return (
              <div key={i} style={{
                display: 'flex', gap: 6, padding: '3px 12px',
                borderBottom: `1px solid ${T.line}`,
                background: e.level === 'ERROR' || e.level === 'CRITICAL' ? 'oklch(0.18 0.08 25 / 0.1)' :
                            e.level === 'WARNING' ? 'oklch(0.18 0.08 75 / 0.06)' : 'transparent',
              }}>
                <span style={{ color: T.ink3, flexShrink: 0, fontSize: 9 }}>{ts}</span>
                <span style={{ color: nameColor, flexShrink: 0, width: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.name}</span>
                <span style={{ color: lvlColor, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 9 }}>{e.msg}</span>
              </div>
            )
          })
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
