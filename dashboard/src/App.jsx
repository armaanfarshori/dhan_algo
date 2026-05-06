import { useState } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { usePoller } from './hooks/usePoller'
import { T, INR, INR0, colorPnl, fmtTime } from './tokens'

import Intro           from './components/cockpit/Intro'
import TopBar          from './components/cockpit/TopBar'
import HeroSection     from './components/cockpit/HeroSection'
import InfoStrip       from './components/cockpit/InfoStrip'
import Pipeline        from './components/cockpit/Pipeline'
import ActivePosition  from './components/cockpit/ActivePosition'
import PayoffChart     from './components/cockpit/PayoffChart'
import ControlPanel    from './components/cockpit/ControlPanel'

import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'

const shell = { position:'relative', zIndex:1, padding:'0 22px 28px', maxWidth:1480, margin:'0 auto' }

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = ['Cockpit','Strategy','Backtest','Risk Console']

function Tabs({ active, onChange }) {
  return (
    <div style={{display:'flex', gap:0, padding:'14px 0 0', borderBottom:`1px solid ${T.line}`, marginBottom:18}}>
      {TABS.map(t => (
        <div key={t} onClick={() => onChange(t)} style={{
          fontFamily:T.mono, fontSize:11, padding:'10px 16px',
          color: active===t ? T.ink0 : T.ink2,
          textTransform:'uppercase', letterSpacing:'0.16em', cursor:'pointer',
          borderBottom: active===t ? `1px solid ${T.green}` : '1px solid transparent',
          marginBottom:-1, transition:'color .15s, border-color .15s',
        }}>{t}</div>
      ))}
    </div>
  )
}

// ── Panel wrapper ─────────────────────────────────────────────────────────────
function Panel({ children, style }) {
  return <div style={{background:T.bg1, border:`1px solid ${T.line}`, ...style}}>{children}</div>
}
function PanelH({ children }) {
  return (
    <div style={{display:'flex', alignItems:'center', gap:10, padding:'10px 14px', borderBottom:`1px solid ${T.line}`, fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em'}}>
      {children}
    </div>
  )
}
function LiveTag() {
  return <span style={{background:T.greenD, color:T.green, padding:'2px 6px', fontSize:9, letterSpacing:'0.2em'}}>LIVE</span>
}

// ── Equity curve ──────────────────────────────────────────────────────────────
function EquityPanel({ signals }) {
  const raw = signals?.data ?? []
  if (raw.length < 2) return (
    <Panel>
      <PanelH><span style={{color:T.ink0}}>P&amp;L CURVE</span></PanelH>
      <div style={{height:120, display:'flex', alignItems:'center', justifyContent:'center', fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.14em'}}>NO TRADE HISTORY YET</div>
    </Panel>
  )

  let equity = 0
  const pts = []
  raw.slice().reverse().forEach((s, i) => {
    if (s.action === 'BUY') equity -= (s.price || 0) * 75
    if (s.action === 'EXIT') equity += (s.price || 0) * 75
    pts.push({ i, pnl: Math.round(equity) })
  })
  const isUp = pts[pts.length-1]?.pnl >= 0

  return (
    <Panel>
      <PanelH>
        <LiveTag/><span style={{color:T.ink0}}>SESSION P&amp;L CURVE</span>
        <span style={{marginLeft:'auto', color: isUp ? T.green : T.red, fontFamily:T.dot, fontSize:20}}>
          {pts[pts.length-1]?.pnl >= 0 ? '+' : ''}{INR0(pts[pts.length-1]?.pnl || 0)}
        </span>
      </PanelH>
      <div style={{padding:'12px 16px 16px'}}>
        <ResponsiveContainer width="100%" height={130}>
          <LineChart data={pts} margin={{top:4,right:4,left:0,bottom:0}}>
            <XAxis dataKey="i" hide />
            <YAxis tickFormatter={v=>`₹${v}`} width={60} tick={{fill:T.ink3, fontSize:9, fontFamily:'JetBrains Mono'}} axisLine={false} tickLine={false}/>
            <Tooltip formatter={v=>[INR0(v),'P&L']} contentStyle={{background:T.bg3, border:`1px solid ${T.line2}`, fontFamily:'JetBrains Mono', fontSize:11}}/>
            <ReferenceLine y={0} stroke={T.line2} strokeDasharray="4 2"/>
            <Line type="monotone" dataKey="pnl" stroke={isUp?T.green:T.red} dot={false} strokeWidth={1.5}/>
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  )
}

// ── Signal feed ───────────────────────────────────────────────────────────────
function SignalFeed({ signals }) {
  const rows = signals?.data ?? []
  const ACTION_COLOR = { BUY:T.green, SELL:T.red, EXIT:T.amber }
  return (
    <Panel>
      <PanelH>
        <span style={{color:T.ink0}}>SIGNAL FEED</span>
        <span style={{marginLeft:'auto', fontSize:9}}>{rows.length} signals</span>
      </PanelH>
      <div style={{overflowY:'auto', maxHeight:220}}>
        <table style={{width:'100%', borderCollapse:'collapse'}}>
          <thead>
            <tr>{['TIME','ACTION','PRICE','REASON'].map(h=>(
              <th key={h} style={{textAlign:'left', fontFamily:T.mono, fontSize:9, letterSpacing:'0.14em', color:T.ink2, padding:'6px 8px', borderBottom:`1px solid ${T.line}`, textTransform:'uppercase', position:'sticky', top:0, background:T.bg1}}>{h}</th>
            ))}</tr>
          </thead>
          <tbody>
            {!rows.length
              ? <tr><td colSpan={4} style={{padding:'16px 8px', fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.14em'}}>WAITING FOR FIRST SIGNAL…</td></tr>
              : rows.map((s,i)=>(
                <tr key={i} style={{borderBottom:`1px solid ${T.line}`}}>
                  <td style={{padding:'7px 8px', fontFamily:T.mono, fontSize:11, color:T.ink2}}>{fmtTime(s.timestamp)}</td>
                  <td style={{padding:'7px 8px', fontFamily:T.mono, fontSize:11, color:ACTION_COLOR[s.action]||T.ink0, fontWeight:600}}>{s.action}</td>
                  <td style={{padding:'7px 8px', fontFamily:T.dot, fontSize:16, color:T.ink0}}>{s.price ? `₹${s.price}` : '—'}</td>
                  <td style={{padding:'7px 8px', fontFamily:T.mono, fontSize:10, color:T.ink2}}>{s.reason}</td>
                </tr>
              ))
            }
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

// ── Positions ─────────────────────────────────────────────────────────────────
function Positions({ positions }) {
  const pos = (positions?.data?.data ?? []).filter(p => p.netQty !== 0)
  return (
    <Panel>
      <PanelH><span style={{color:T.ink0}}>LIVE POSITIONS</span></PanelH>
      <div style={{padding:14}}>
        {!pos.length
          ? <div style={{fontFamily:T.mono, fontSize:10, color:T.ink3, letterSpacing:'0.14em', textTransform:'uppercase'}}>NO OPEN POSITIONS</div>
          : pos.map((p,i)=>{
            const upnl = p.unrealisedProfit || 0
            return (
              <div key={i} style={{background:T.bg2, border:`1px solid ${T.line}`, padding:'10px 12px', marginBottom:8}}>
                <div style={{fontFamily:T.mono, fontSize:12, color:T.cyan, fontWeight:600}}>{p.tradingSymbol||p.securityId}</div>
                <div style={{fontFamily:T.mono, fontSize:10, color:T.ink2, marginTop:2}}>QTY {p.netQty} · {p.productType||''}</div>
                <div style={{fontFamily:T.dot, fontSize:20, color:colorPnl(upnl), marginTop:4}}>{INR(upnl)}</div>
              </div>
            )
          })
        }
      </div>
    </Panel>
  )
}

// ── Risk panel ────────────────────────────────────────────────────────────────
function RiskPanel({ risk }) {
  const d = risk?.data
  const halted = d?.halted ?? false
  const r = d?.realised_pnl   ?? 0
  const u = d?.unrealised_pnl ?? 0
  const t = d?.total_pnl      ?? 0

  return (
    <Panel style={{border: halted ? `1px solid ${T.red}` : `1px solid ${T.line}`}}>
      <PanelH>
        <span style={{width:8,height:8,borderRadius:'50%',background:halted?T.red:T.green,boxShadow:`0 0 6px ${halted?T.red:T.green}`,animation:halted?'pulse 1s infinite':'none',flexShrink:0}}/>
        <span style={{color: halted?T.red:T.green}}>{halted ? `HALTED — ${d?.halt_reason}` : 'RISK OK'}</span>
      </PanelH>
      <div style={{display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:8, padding:14}}>
        {[['REALISED',r],['UNREALISED',u],['TOTAL',t]].map(([k,v])=>(
          <div key={k} style={{background:T.bg2, border:`1px solid ${T.line}`, padding:'8px 10px'}}>
            <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.2em', textTransform:'uppercase', marginBottom:4}}>{k}</div>
            <div style={{fontFamily:T.dot, fontSize:22, color:colorPnl(v)}}>{INR(v)}</div>
          </div>
        ))}
      </div>
      <div style={{padding:'0 14px 14px'}}>
        <div style={{height:6, background:T.bg3, border:`1px solid ${T.line}`, borderRadius:1, overflow:'hidden'}}>
          <div style={{height:'100%', width:`${Math.min(Math.abs(t)/5000*100,100)}%`, background:`linear-gradient(90deg,${T.green},${T.amber} 70%,${T.red})`, transition:'width .5s'}}/>
        </div>
        <div style={{display:'flex', justifyContent:'space-between', fontFamily:T.mono, fontSize:8, color:T.ink3, marginTop:4}}>
          <span>₹0</span><span>₹2,500</span><span>₹5,000 CAP</span>
        </div>
      </div>
    </Panel>
  )
}

// ── Velocity ──────────────────────────────────────────────────────────────────
function VelocityPanel({ status, risk }) {
  const d = status?.data
  const orders = d?.orders_placed ?? 0
  const uptime = d?.uptime_seconds ?? 1
  const rate = (orders / (uptime/3600)).toFixed(1)
  return (
    <Panel>
      <PanelH><span style={{color:T.ink0}}>VELOCITY · DAILY</span></PanelH>
      <div style={{padding:14}}>
        <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.2em', textTransform:'uppercase', marginBottom:8}}>TRADES PER HOUR</div>
        <div style={{fontFamily:T.dot, fontSize:56, color:T.amber, textShadow:`0 0 14px oklch(0.82 0.16 75 / 0.25)`, lineHeight:0.95}}>
          {rate}<span style={{fontSize:22, color:T.ink2, marginLeft:6}}>/hr</span>
        </div>
        <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, marginTop:6, letterSpacing:'0.14em', textTransform:'uppercase'}}>
          {orders} orders · {Math.floor(uptime/3600)}h {Math.floor((uptime%3600)/60)}m uptime
        </div>
      </div>
    </Panel>
  )
}

// ── Footer ────────────────────────────────────────────────────────────────────
function FootBar({ status, risk }) {
  const d = status?.data
  const r = risk?.data
  return (
    <div style={{display:'flex', alignItems:'center', gap:18, padding:'12px 0', marginTop:18, borderTop:`1px solid ${T.line}`, fontFamily:T.mono, fontSize:9, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.18em', flexWrap:'wrap'}}>
      <span>ORDERS <b style={{color:T.ink0}}>{d?.orders_placed??'—'}</b></span>
      <span>POSITION <b style={{color: (d?.position??0)!==0?T.green:T.ink3}}>{d?.position??0}</b></span>
      <span>HALTED <b style={{color:r?.halted?T.red:T.green}}>{r?.halted?'YES':'NO'}</b></span>
      <div style={{flex:1}}/>
      <span>FEED <b style={{color:T.green}}>OK</b></span>
      <span>AUTH <b style={{color:T.green}}>OK</b></span>
      <span>BUILD <b>1.0.{d?.orders_placed||0}</b></span>
    </div>
  )
}

// ── Cockpit tab ───────────────────────────────────────────────────────────────
function CockpitTab({ data, onSwitch, onKill }) {
  const { status, risk, signals, funds, positions, scalper, payoff, config } = data
  const halted = risk?.data?.halted ?? false

  return (
    <>
      <HeroSection risk={risk} status={status} />
      <InfoStrip status={status} risk={risk} funds={funds} scalper={scalper} />

      {/* Row: Risk + Velocity + Active Position */}
      <div style={{display:'grid', gridTemplateColumns:'1fr 1fr 1.4fr', gap:14, marginBottom:14}}>
        <RiskPanel risk={risk} />
        <VelocityPanel status={status} risk={risk} />
        <ActivePosition scalper={scalper} status={status} />
      </div>

      {/* Pipeline */}
      <Pipeline scalper={scalper} />

      {/* Control panel with 6 features */}
      <ControlPanel config={config} onSwitch={onSwitch} onKill={onKill} />

      {/* Payoff chart */}
      <PayoffChart payoff={payoff} />

      {/* Equity curve + signals + positions */}
      <div style={{display:'grid', gridTemplateColumns:'1fr', gap:14, marginBottom:14}}>
        <EquityPanel signals={signals} />
      </div>
      <div style={{display:'grid', gridTemplateColumns:'1.8fr 1fr', gap:14, marginBottom:14}}>
        <SignalFeed signals={signals} />
        <Positions positions={positions} />
      </div>

      <FootBar status={status} risk={risk} />
    </>
  )
}

// ── Strategy tab ──────────────────────────────────────────────────────────────
function StrategyTab({ data, onSwitch, onKill }) {
  return (
    <div style={{padding:'20px 0'}}>
      <ControlPanel config={data.config} onSwitch={onSwitch} onKill={onKill} />
      <PayoffChart payoff={data.payoff} />
      <SignalFeed signals={data.signals} />
    </div>
  )
}

// ── Root ──────────────────────────────────────────────────────────────────────
export default function App() {
  const data = useDashboardData()
  const [tab, setTab] = useState('Cockpit')
  const halted = data.risk?.data?.halted ?? false

  return (
    <>
      <Intro />
      <div style={shell}>
        <TopBar status={data.status} halted={halted} />
        <Tabs active={tab} onChange={setTab} />
        {tab === 'Cockpit'       && <CockpitTab  data={data} onSwitch={() => {}} onKill={() => {}} />}
        {tab === 'Strategy'      && <StrategyTab data={data} onSwitch={() => {}} onKill={() => {}} />}
        {tab === 'Backtest'      && <div style={{padding:'40px 0', fontFamily:T.mono, fontSize:12, color:T.ink2, letterSpacing:'0.14em', textTransform:'uppercase'}}>BACKTEST MODULE — COMING SOON</div>}
        {tab === 'Risk Console'  && <div style={{padding:'20px 0'}}><RiskPanel risk={data.risk}/></div>}
      </div>
    </>
  )
}
