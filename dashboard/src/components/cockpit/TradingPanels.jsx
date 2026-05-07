/**
 * Separate F&O and Equity trading panels — positions, signals, live P&L.
 */
import { T, INR, INR0, colorPnl, fmtTime } from '../../tokens'

const ACTION_COLOR = { BUY: T.green, SELL: T.red, EXIT: T.amber }

function PanelH({ children }) {
  return (
    <div style={{ display:'flex', alignItems:'center', gap:10, padding:'10px 14px', borderBottom:`1px solid ${T.line}`, fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em' }}>
      {children}
    </div>
  )
}

function Stat({ label, value, color }) {
  return (
    <div style={{ background:T.bg3, border:`1px solid ${T.line}`, padding:'8px 10px', flex:1 }}>
      <div style={{ fontFamily:T.mono, fontSize:8, color:T.ink3, letterSpacing:'0.16em', marginBottom:3 }}>{label}</div>
      <div style={{ fontFamily:'VT323', fontSize:20, color:color||T.ink0 }}>{value}</div>
    </div>
  )
}

// ── F&O Panel ────────────────────────────────────────────────────────────────
export function FnoPanel({ fnoScanner, signals }) {
  const sc  = fnoScanner?.data
  const idx = sc?.indices || {}

  const openPositions = Object.entries(idx).filter(([,v]) => v.in_position)
  const totalUnrealized = openPositions.reduce((sum,[,v]) => sum + (v.unrealized_pnl||0), 0)
  const fnoSignals = (signals?.data||[]).filter(s => s.source === 'F&O').slice(0,20)

  return (
    <div style={{ background:T.bg1, border:`1px solid ${T.line}` }}>
      {/* Header */}
      <PanelH>
        <span style={{ background:T.greenD, color:T.green, padding:'2px 6px', fontSize:9 }}>F&O</span>
        <span style={{ color:T.ink0 }}>INDEX OPTIONS</span>
        <div style={{ display:'flex', gap:8, marginLeft:'auto' }}>
          <Stat label="POSITIONS" value={openPositions.length} color={openPositions.length>0?T.amber:T.ink3}/>
          <Stat label="ORDERS"    value={sc?.orders_placed??0} color={T.ink0}/>
          <Stat label="UNREALISED" value={INR0(totalUnrealized)} color={colorPnl(totalUnrealized)}/>
        </div>
      </PanelH>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:0 }}>
        {/* Open positions */}
        <div style={{ borderRight:`1px solid ${T.line}`, padding:14 }}>
          <div style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.18em', textTransform:'uppercase', marginBottom:10 }}>OPEN POSITIONS</div>
          {openPositions.length === 0 ? (
            <div style={{ fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.12em' }}>NO OPEN F&O POSITIONS</div>
          ) : openPositions.map(([name, v]) => {
            const upnl = v.unrealized_pnl || 0
            const pct  = v.current_premium && v.entry ? ((v.current_premium - v.entry) / v.entry * 100).toFixed(1) : null
            return (
              <div key={name} style={{ background:T.bg2, border:`1px solid ${T.green}`, padding:'10px 12px', marginBottom:8 }}>
                <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
                  <div>
                    <span style={{ fontFamily:T.mono, fontSize:11, color:T.green, fontWeight:600 }}>{name}</span>
                    <span style={{ fontFamily:T.mono, fontSize:9, color:T.amber, marginLeft:8 }}>{v.option_type} {v.strike?.toLocaleString('en-IN')}</span>
                  </div>
                  <span style={{ fontFamily:'VT323', fontSize:18, color:colorPnl(upnl) }}>{upnl>=0?'+':''}{INR0(upnl)}</span>
                </div>
                <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:6 }}>
                  {[
                    ['ENTRY',   v.entry ? `₹${v.entry}` : '—',              T.ink0],
                    ['NOW',     v.current_premium ? `₹${v.current_premium?.toFixed(2)}` : '—', pct ? colorPnl(parseFloat(pct)) : T.ink0],
                    ['BEP',     v.breakeven ? `₹${v.breakeven}` : '—',      T.amber],
                    ['TARGET',  v.target ? `₹${v.target}` : '—',            T.green],
                  ].map(([k,val,c]) => (
                    <div key={k}>
                      <div style={{ fontFamily:T.mono, fontSize:7, color:T.ink3, letterSpacing:'0.14em' }}>{k}</div>
                      <div style={{ fontFamily:'VT323', fontSize:16, color:c }}>{val}</div>
                    </div>
                  ))}
                </div>
                <div style={{ fontFamily:T.mono, fontSize:8, color:T.ink3, marginTop:6 }}>
                  LOT {v.lot_size} · {v.expiry} · STOP ₹{v.stop}
                </div>
              </div>
            )
          })}
        </div>

        {/* F&O signals */}
        <div style={{ padding:14 }}>
          <div style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.18em', textTransform:'uppercase', marginBottom:10 }}>SIGNALS</div>
          <div style={{ overflowY:'auto', maxHeight:300 }}>
            {fnoSignals.length === 0
              ? <div style={{ fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.12em' }}>WAITING FOR SIGNAL…</div>
              : fnoSignals.map((s,i) => (
                <div key={i} style={{ display:'flex', gap:10, alignItems:'center', padding:'6px 0', borderBottom:`1px solid ${T.line}` }}>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:T.ink3, width:56, flexShrink:0 }}>{fmtTime(s.timestamp)}</span>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:ACTION_COLOR[s.action]||T.ink0, fontWeight:600, width:32 }}>{s.action}</span>
                  <span style={{ fontFamily:'VT323', fontSize:16, color:T.ink0, width:60 }}>{s.price?`₹${s.price}`:'—'}</span>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, flex:1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{s.reason}</span>
                </div>
              ))
            }
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Equity Panel ──────────────────────────────────────────────────────────────
export function EqPanel({ paperPositions, signals }) {
  const pp = paperPositions?.data
  const eqPos = (pp?.data||[]).filter(p => p.engine === 'EQ')
  const eqSignals = (signals?.data||[]).filter(s => s.source === 'EQ').slice(0,20)
  const totalUnrealized = eqPos.reduce((sum,p) => sum + (p.unrealized_pnl||0), 0)

  return (
    <div style={{ background:T.bg1, border:`1px solid ${T.line}` }}>
      <PanelH>
        <span style={{ background:'oklch(0.30 0.10 200)', color:T.cyan, padding:'2px 6px', fontSize:9 }}>EQ</span>
        <span style={{ color:T.ink0 }}>EQUITY TOP MOVERS</span>
        <div style={{ display:'flex', gap:8, marginLeft:'auto' }}>
          <Stat label="POSITIONS"  value={eqPos.length}           color={eqPos.length>0?T.amber:T.ink3}/>
          <Stat label="SIGNALS"    value={eqSignals.length}       color={T.ink0}/>
          <Stat label="UNREALISED" value={INR0(totalUnrealized)}  color={colorPnl(totalUnrealized)}/>
        </div>
      </PanelH>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:0 }}>
        {/* Open positions */}
        <div style={{ borderRight:`1px solid ${T.line}`, padding:14 }}>
          <div style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.18em', textTransform:'uppercase', marginBottom:10 }}>OPEN POSITIONS</div>
          {eqPos.length === 0 ? (
            <div style={{ fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.12em' }}>NO OPEN EQUITY POSITIONS</div>
          ) : eqPos.map((p,i) => {
            const upnl = p.unrealized_pnl || 0
            return (
              <div key={i} style={{ background:T.bg2, border:`1px solid ${T.cyan}`, padding:'10px 12px', marginBottom:8 }}>
                <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
                  <div>
                    <span style={{ fontFamily:T.mono, fontSize:11, color:T.cyan, fontWeight:600 }}>{p.symbol}</span>
                    <span style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, marginLeft:8 }}>QTY {p.qty}</span>
                  </div>
                  <span style={{ fontFamily:'VT323', fontSize:18, color:colorPnl(upnl) }}>{upnl>=0?'+':''}{INR0(upnl)}</span>
                </div>
                <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:6 }}>
                  {[
                    ['ENTRY',   p.entry_price  ? INR(p.entry_price)   : '—', T.ink0],
                    ['NOW',     p.current_price ? INR(p.current_price) : '—', colorPnl(upnl)],
                    ['CHG%',    p.change_pct != null ? `${p.change_pct>=0?'+':''}${p.change_pct}%` : '—', colorPnl(upnl)],
                  ].map(([k,val,c]) => (
                    <div key={k}>
                      <div style={{ fontFamily:T.mono, fontSize:7, color:T.ink3, letterSpacing:'0.14em' }}>{k}</div>
                      <div style={{ fontFamily:'VT323', fontSize:16, color:c }}>{val}</div>
                    </div>
                  ))}
                </div>
                <div style={{ fontFamily:T.mono, fontSize:8, color:T.ink3, marginTop:4 }}>{p.name} · {p.segment}</div>
              </div>
            )
          })}
        </div>

        {/* Equity signals */}
        <div style={{ padding:14 }}>
          <div style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.18em', textTransform:'uppercase', marginBottom:10 }}>SIGNALS</div>
          <div style={{ overflowY:'auto', maxHeight:300 }}>
            {eqSignals.length === 0
              ? <div style={{ fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.12em' }}>WAITING FOR SIGNAL…</div>
              : eqSignals.map((s,i) => (
                <div key={i} style={{ display:'flex', gap:10, alignItems:'center', padding:'6px 0', borderBottom:`1px solid ${T.line}` }}>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:T.ink3, width:56, flexShrink:0 }}>{fmtTime(s.timestamp)}</span>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:ACTION_COLOR[s.action]||T.ink0, fontWeight:600, width:32 }}>{s.action}</span>
                  <span style={{ fontFamily:'VT323', fontSize:16, color:T.ink0, width:60 }}>{s.price?`₹${s.price}`:'—'}</span>
                  <span style={{ fontFamily:T.mono, fontSize:9, color:T.ink2, flex:1, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{s.reason}</span>
                </div>
              ))
            }
          </div>
        </div>
      </div>
    </div>
  )
}
