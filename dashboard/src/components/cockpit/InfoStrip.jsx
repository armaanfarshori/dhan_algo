import { T, INR, fmtUptime } from '../../tokens'

function Seg({ label, children, color }) {
  return (
    <span style={{padding:'0 16px', borderRight:`1px solid ${T.line}`, fontSize:10, fontFamily:T.mono, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.14em'}}>
      {label} <b style={{color: color||T.ink0, fontWeight:500}}>{children}</b>
    </span>
  )
}

export default function InfoStrip({ status, risk, funds, scalper }) {
  const d = status?.data
  const r = risk?.data
  const f = funds?.data?.data
  const sc = scalper?.data

  return (
    <div style={{
      display:'flex', flexWrap:'wrap',
      fontFamily:T.mono, fontSize:10, color:T.ink2,
      padding:'8px 0 18px',
      textTransform:'uppercase', letterSpacing:'0.14em',
      borderBottom:`1px solid ${T.line}`, marginBottom:18,
    }}>
      <Seg label="CLIENT">{d?.client_id || '—'}</Seg>
      <Seg label="EXPIRY">{sc?.active_expiry || '—'}</Seg>
      <Seg label="FUNDS">{f ? INR(f.availabelBalance) : '—'}</Seg>
      <Seg label="MAX LOSS">{r ? INR(r.realised_pnl + r.unrealised_pnl) : '—'}</Seg>
      <Seg label="RSI-14" color={T.cyan}>{sc?.last_rsi ? sc.last_rsi.toFixed(1) : '—'}</Seg>
      <Seg label="UPTIME">{d?.uptime_seconds ? fmtUptime(d.uptime_seconds) : '—'}</Seg>
      <Seg label="ORDERS">{d?.orders_placed ?? '—'}</Seg>
      <Seg label="OCO STATE" color={sc?.oco_state === 'IN_POSITION' ? T.amber : T.green}>{sc?.oco_state || '—'}</Seg>
    </div>
  )
}
