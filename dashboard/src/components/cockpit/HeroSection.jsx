import { T, INR0, colorPnl } from '../../tokens'

function StreakOrb({ count }) {
  return (
    <div style={{
      width:150, height:150, borderRadius:'50%', position:'relative',
      background:'radial-gradient(circle at 35% 30%, oklch(0.30 0.10 200), oklch(0.12 0.04 220))',
      border:`1px solid ${T.line2}`,
      display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center',
      boxShadow:`inset 0 0 30px rgba(0,0,0,0.6), 0 0 28px oklch(0.62 0.10 200 / 0.15)`,
      flexShrink: 0,
    }}>
      <div style={{
        position:'absolute', inset:-4, borderRadius:'50%',
        border:`1px dashed ${T.line2}`, opacity:0.5,
        animation:'spin 28s linear infinite',
      }} />
      <div style={{fontFamily:T.dot, fontSize:56, color:T.cyan, textShadow:`0 0 18px oklch(0.82 0.12 200 / 0.5)`, lineHeight:1}}>
        {count >= 0 ? `+${count}` : count}
      </div>
      <div style={{fontFamily:T.mono, fontSize:8, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.2em', marginTop:6}}>WIN STREAK</div>
    </div>
  )
}

export default function HeroSection({ risk, status }) {
  const r = risk?.data
  const d = status?.data
  const totalPnl = r?.total_pnl ?? 0
  const unrealised = r?.unrealised_pnl ?? 0
  const pos = d?.position ?? 0
  const orders = d?.orders_placed ?? 0

  return (
    <div style={{
      display:'grid', gridTemplateColumns:'auto auto 1fr auto', gap:36,
      alignItems:'end', padding:'14px 0 22px',
      borderBottom:`1px solid ${T.line}`, marginBottom:18,
    }}>
      <div>
        <div style={{fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.2em', marginBottom:8, display:'flex', gap:10, alignItems:'center'}}>
          <span style={{background:T.greenD, color:T.green, padding:'2px 6px', fontSize:9, letterSpacing:'0.2em'}}>LIVE</span>
          SESSION P&amp;L
        </div>
        <div style={{
          fontFamily:T.dot, fontSize:110, lineHeight:0.85,
          color: totalPnl >= 0 ? T.green : T.red,
          textShadow: totalPnl >= 0
            ? '0 0 28px oklch(0.78 0.19 145 / 0.25), 0 0 1px oklch(0.78 0.19 145 / 0.6)'
            : '0 0 28px oklch(0.68 0.22 25 / 0.25)',
          animation: 'breathe 6s ease-in-out infinite',
        }}>
          <span style={{fontSize:60, verticalAlign:'top', marginRight:4, opacity:0.7}}>₹</span>
          {Math.abs(Math.round(totalPnl)).toLocaleString('en-IN')}
        </div>
        <div style={{fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em', marginTop:14, display:'flex', gap:14, flexWrap:'wrap'}}>
          <span><b style={{color:T.ink0, fontWeight:500}}>{orders} orders</b></span>
          <span style={{color: totalPnl>=0 ? T.green : T.red}}>{totalPnl>=0?'▲':'▼'} PAPER</span>
        </div>
      </div>

      <div>
        <div style={{fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.2em', marginBottom:8}}>UNREALISED · OPEN</div>
        <div style={{fontFamily:T.dot, fontSize:110, lineHeight:0.85, color: unrealised !== 0 ? colorPnl(unrealised) : T.ink3}}>
          {unrealised >= 0 ? '+' : ''}{Math.round(unrealised).toLocaleString('en-IN')}
        </div>
        <div style={{fontFamily:T.mono, fontSize:10, color:T.ink2, marginTop:14}}>
          {pos !== 0 ? <span style={{color:T.green}}>{pos > 0 ? `+${pos}` : pos} qty open</span> : 'FLAT'}
        </div>
      </div>

      <div />
      <StreakOrb count={orders > 0 ? Math.floor(orders * 0.65) : 0} />
    </div>
  )
}
