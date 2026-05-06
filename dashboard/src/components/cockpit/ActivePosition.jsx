import { T, INR } from '../../tokens'

export default function ActivePosition({ scalper, status }) {
  const sc = scalper?.data
  const d  = status?.data
  const inPos = sc?.state === 'IN_POSITION' || (d?.position ?? 0) !== 0

  const panelStyle = {
    background:T.bg1, border:`1px solid ${inPos ? T.green : T.line}`,
    boxShadow: inPos ? `0 0 20px oklch(0.55 0.18 145 / 0.1)` : 'none',
  }

  return (
    <div style={panelStyle}>
      <div style={{display:'flex', alignItems:'center', gap:10, padding:'10px 14px', borderBottom:`1px solid ${T.line}`, fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em'}}>
        {inPos && <span style={{background:T.greenD, color:T.green, padding:'2px 6px', fontSize:9}}>LIVE</span>}
        <span style={{color:T.ink0}}>ACTIVE POSITION</span>
        {sc?.current_atm && (
          <span style={{marginLeft:'auto', color:T.cyan}}>{sc.current_atm.strike} · {sc.active_expiry}</span>
        )}
      </div>

      {inPos ? (
        <div style={{padding:'14px 16px', display:'grid', gridTemplateColumns:'auto 1fr', gap:16, alignItems:'start'}}>
          <div style={{
            width:38, height:38, border:`1px solid ${T.green}`, color:T.green,
            display:'flex', alignItems:'center', justifyContent:'center',
            fontFamily:T.mono, fontWeight:600, fontSize:14,
          }}>
            {d?.position > 0 ? 'CE' : 'PE'}
          </div>
          <div>
            <div style={{fontFamily:T.mono, fontSize:11, color:T.ink2, letterSpacing:'0.14em', textTransform:'uppercase'}}>IN POSITION</div>
            <div style={{fontFamily:T.mono, fontSize:13, color:T.ink0, marginTop:2}}>
              {sc?.current_atm ? `NIFTY ${sc.current_atm.strike} · ${sc.active_expiry}` : 'NIFTY OPTIONS'}
            </div>
            <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap:'10px 18px', marginTop:10}}>
              {[
                ['ENTRY',    d?.entry_price ? INR(d.entry_price) : '—', T.ink0],
                ['BEP',      sc?.breakeven_premium ? INR(sc.breakeven_premium) : '—', T.amber],
                ['TARGET',   sc?.breakeven_premium ? INR(sc.breakeven_premium + 5) : '—', T.green],
                ['STOP',     d?.entry_price ? INR(d.entry_price - 5) : '—', T.red],
              ].map(([k, v, c]) => (
                <div key={k} style={{fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.14em'}}>
                  {k}
                  <div style={{fontFamily:T.dot, fontSize:22, color:c, display:'block', lineHeight:1, marginTop:2}}>{v}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div style={{padding:'32px 16px', textAlign:'center', fontFamily:T.mono, fontSize:11, color:T.ink3, letterSpacing:'0.14em', textTransform:'uppercase'}}>
          NO ACTIVE POSITION · FLAT
        </div>
      )}
    </div>
  )
}
