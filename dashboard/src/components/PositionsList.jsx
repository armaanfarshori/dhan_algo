import { T, INR, colorPnl } from '../tokens'

export default function PositionsList({ positions }) {
  const pos = positions
  if (!pos || pos.loading) return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.18em', marginBottom: 8 }}>Live Positions</div>
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, marginTop: 8 }}>Loading…</div>
    </div>
  )
  if (pos.error || !pos.data?.ok) return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.18em', marginBottom: 8 }}>Live Positions</div>
      <div style={{ fontFamily: T.mono, fontSize: 10, color: T.red, marginTop: 8 }}>Could not load positions</div>
    </div>
  )

  const open = (pos.data.data ?? []).filter(p => p.netQty !== 0)
  return (
    <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.18em', marginBottom: 8 }}>
        Live Positions
      </div>
      {open.length === 0
        ? <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, marginTop: 8 }}>No open positions</div>
        : open.map((p, i) => {
            const upnl = p.unrealisedProfit ?? 0
            const qty  = p.netQty
            return (
              <div key={i} style={{
                background: T.bg2,
                border: `1px solid ${T.line}`,
                borderLeft: `3px solid ${qty > 0 ? T.green : T.red}`,
                padding: 16,
                marginTop: 8,
              }}>
                <div style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 700, color: T.cyan }}>
                  {p.tradingSymbol ?? p.securityId}
                </div>
                <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', marginTop: 4 }}>
                  QTY: {qty} · {p.productType ?? ''}
                </div>
                <div style={{ fontFamily: T.dot, fontSize: 20, color: colorPnl(upnl), marginTop: 4 }}>
                  {upnl >= 0 ? '+' : ''}{INR(upnl)}
                </div>
              </div>
            )
          })
      }
    </div>
  )
}
