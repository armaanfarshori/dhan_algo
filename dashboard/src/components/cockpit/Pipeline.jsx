import { T } from '../../tokens'

const STEPS = [
  { n:'01', label:'SCAN',   name:'Poll OHLC',     desc:'Underlying tick\nevery 10s · IDX_I', lat:'38ms' },
  { n:'02', label:'SIGNAL', name:'RSI-14',         desc:'Cross 30 / 70\nWilder smoothed',    lat:'0.4ms' },
  { n:'03', label:'ATM',    name:'find_atm',       desc:'Master.find_atm(\nprice, expiry)',   lat:'1.2ms' },
  { n:'04', label:'RISK',   name:'check_order',    desc:'Cap, positions,\nkill switch',       lat:'0.1ms' },
  { n:'05', label:'FILL',   name:'place_order',    desc:'MARKET · MARGIN\npoll ORDER_ID',    lat:'720ms' },
  { n:'06', label:'OCO',    name:'forever/orders', desc:'Target = BEP+5\nStop  = entry-5',   lat:'610ms' },
]

export default function Pipeline({ scalper }) {
  const sc = scalper?.data
  const state = sc?.state || 'FLAT'
  const activeStep = state === 'IN_POSITION' ? 4 : state === 'FLAT' ? 0 : 2

  return (
    <div style={{marginBottom:14}}>
      <div style={{
        display:'flex', alignItems:'center', gap:14,
        padding:'10px 14px', borderTop:`1px solid ${T.line}`, borderBottom:`1px solid ${T.line}`,
        fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em',
        background:T.bg1,
      }}>
        <span style={{background:T.greenD, color:T.green, padding:'2px 6px', fontSize:9, letterSpacing:'0.2em'}}>LIVE</span>
        <span style={{color:T.ink0}}>SCALPER EXECUTION PIPELINE · _tick → _enter → OCO</span>
        <div style={{marginLeft:'auto', display:'flex', gap:18}}>
          <span>STATE <b style={{color:T.ink0}}>{state}</b></span>
          <span>EXPIRY <b style={{color:T.ink0}}>{sc?.active_expiry||'—'}</b></span>
          <span>OCO <b style={{color:sc?.oco_order_id ? T.green : T.ink3}}>{sc?.oco_order_id ? 'ACTIVE' : 'NONE'}</b></span>
        </div>
      </div>
      <div style={{display:'grid', gridTemplateColumns:'repeat(6,1fr)', background:T.bg1, borderBottom:`1px solid ${T.line}`}}>
        {STEPS.map((s, i) => {
          const isActive = i === activeStep
          return (
            <div key={i} style={{
              padding:'14px 14px 12px',
              borderRight: i < 5 ? `1px solid ${T.line}` : 'none',
              position:'relative',
              background: isActive ? 'oklch(0.20 0.08 145 / 0.35)' : 'transparent',
              boxShadow: isActive ? `inset 0 0 0 1px ${T.green}, inset 0 0 24px oklch(0.55 0.18 145 / 0.25)` : 'none',
              transition:'background .2s',
            }}>
              <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.2em', textTransform:'uppercase'}}>{s.n} · {s.label}</div>
              <div style={{fontFamily:T.mono, fontSize:14, fontWeight:600, color: isActive ? T.green : T.ink0, marginTop:4}}>{s.name}</div>
              <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, marginTop:6, lineHeight:1.5, letterSpacing:'0.04em', whiteSpace:'pre-line'}}>{s.desc}</div>
              <div style={{fontFamily:T.mono, fontSize:9, color:T.ink1, marginTop:8, letterSpacing:'0.1em'}}>
                ⟶ <b style={{color: isActive ? T.green : T.cyan}}>{s.lat}</b>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
