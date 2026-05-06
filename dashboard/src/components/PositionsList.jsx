import { INR, colorVar } from '../utils'

const s = {
  card:  { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: 16 },
  label: { fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 8 },
  item:  { background: 'var(--bg)', borderRadius: 6, padding: 10, marginTop: 8 },
  sym:   { fontWeight: 'bold', color: 'var(--blue)' },
  qty:   { color: 'var(--muted)', fontSize: 11, marginTop: 2 },
  upnl:  { fontSize: 13, marginTop: 4 },
  empty: { color: 'var(--muted)', fontSize: 12, marginTop: 8 },
  err:   { color: 'var(--red)', fontSize: 11, marginTop: 8 },
}

export default function PositionsList({ positions }) {
  const pos = positions
  if (!pos || pos.loading) return <div style={s.card}><div style={s.label}>Live Positions</div><div style={s.empty}>Loading…</div></div>
  if (pos.error || !pos.data?.ok) return <div style={s.card}><div style={s.label}>Live Positions</div><div style={s.err}>Could not load positions</div></div>

  const open = (pos.data.data ?? []).filter(p => p.netQty !== 0)
  return (
    <div style={s.card}>
      <div style={s.label}>Live Positions</div>
      {open.length === 0
        ? <div style={s.empty}>No open positions</div>
        : open.map((p, i) => {
            const upnl = p.unrealisedProfit ?? 0
            return (
              <div key={i} style={s.item}>
                <div style={s.sym}>{p.tradingSymbol ?? p.securityId}</div>
                <div style={s.qty}>Qty: {p.netQty} · {p.productType ?? ''}</div>
                <div style={{ ...s.upnl, color: colorVar(upnl) }}>uPnL: {INR(upnl)}</div>
              </div>
            )
          })
      }
    </div>
  )
}
