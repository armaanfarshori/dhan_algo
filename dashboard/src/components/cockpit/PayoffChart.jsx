import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, CartesianGrid } from 'recharts'
import { T, INR } from '../../tokens'

function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const v = payload[0].value
  return (
    <div style={{background:T.bg3, border:`1px solid ${T.line2}`, padding:'6px 12px', fontFamily:T.mono, fontSize:11}}>
      <div style={{color:T.ink2, fontSize:9, letterSpacing:'0.14em'}}>P&L</div>
      <div style={{color: v >= 0 ? T.green : T.red, fontSize:16, fontFamily:T.dot}}>{INR(v)}</div>
    </div>
  )
}

export default function PayoffChart({ payoff }) {
  const d = payoff?.data
  const hasData = d?.ok && d?.points?.length > 1

  return (
    <div style={{background:T.bg1, border:`1px solid ${T.line}`, marginBottom:14}}>
      <div style={{
        display:'flex', alignItems:'center', gap:10,
        padding:'10px 14px', borderBottom:`1px solid ${T.line}`,
        fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em',
      }}>
        <span style={{color:T.ink0}}>PAYOFF DIAGRAM</span>
        {d?.mode && (
          <span style={{
            marginLeft:'auto', padding:'2px 6px', fontSize:9,
            background: d.mode === 'live' ? T.greenD : 'oklch(0.30 0.10 75)',
            color: d.mode === 'live' ? T.green : T.amber,
          }}>
            {d.mode === 'live' ? '● LIVE POSITION' : '◌ WHAT-IF'}
          </span>
        )}
        {hasData && (
          <div style={{display:'flex', gap:18, marginLeft: d?.mode ? 0 : 'auto'}}>
            {d.entry    && <span>ENTRY <b style={{color:T.ink0}}>{INR(d.entry)}</b></span>}
            {d.breakeven && <span>BEP <b style={{color:T.amber}}>{INR(d.breakeven)}</b></span>}
            {d.target   && <span>TARGET <b style={{color:T.green}}>{INR(d.target)}</b></span>}
            {d.stop     && <span>STOP <b style={{color:T.red}}>{INR(d.stop)}</b></span>}
          </div>
        )}
      </div>
      <div style={{padding:'12px 16px 16px'}}>
        {hasData ? (
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={d.points} margin={{top:8,right:8,left:0,bottom:0}}>
              <defs>
                <linearGradient id="payoffGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={T.green} stopOpacity={0.25}/>
                  <stop offset="95%" stopColor={T.green} stopOpacity={0.02}/>
                </linearGradient>
                <linearGradient id="payoffGradRed" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={T.red} stopOpacity={0.25}/>
                  <stop offset="95%" stopColor={T.red} stopOpacity={0.02}/>
                </linearGradient>
              </defs>
              <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false}/>
              <XAxis dataKey="premium" tickFormatter={v=>`₹${v}`}
                tick={{fill:T.ink3, fontSize:9, fontFamily:'JetBrains Mono'}} axisLine={false} tickLine={false}/>
              <YAxis tickFormatter={v=>`₹${v}`} width={64}
                tick={{fill:T.ink3, fontSize:9, fontFamily:'JetBrains Mono'}} axisLine={false} tickLine={false}/>
              <Tooltip content={<CustomTooltip/>}/>
              <ReferenceLine y={0} stroke={T.line2} strokeDasharray="4 2"/>
              {d.entry     && <ReferenceLine x={d.entry}     stroke={T.cyan}  strokeDasharray="3 3" label={{value:'ENTRY',  fill:T.cyan,  fontSize:8, fontFamily:'JetBrains Mono'}}/>}
              {d.breakeven && <ReferenceLine x={d.breakeven} stroke={T.amber} strokeDasharray="3 3" label={{value:'BEP',    fill:T.amber, fontSize:8, fontFamily:'JetBrains Mono'}}/>}
              {d.target    && <ReferenceLine x={d.target}    stroke={T.green} strokeDasharray="3 3" label={{value:'TARGET', fill:T.green, fontSize:8, fontFamily:'JetBrains Mono'}}/>}
              {d.stop      && <ReferenceLine x={d.stop}      stroke={T.red}   strokeDasharray="3 3" label={{value:'STOP',   fill:T.red,   fontSize:8, fontFamily:'JetBrains Mono'}}/>}
              <Area type="monotone" dataKey="pnl"
                stroke={T.green} fill="url(#payoffGrad)" strokeWidth={1.5} dot={false}/>
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div style={{height:100, display:'flex', alignItems:'center', justifyContent:'center',
            fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.14em', textTransform:'uppercase'}}>
            NO POSITION DATA · PAYOFF UNAVAILABLE
          </div>
        )}
      </div>
    </div>
  )
}
