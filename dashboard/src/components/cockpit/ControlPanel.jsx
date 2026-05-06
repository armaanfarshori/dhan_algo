import { useState, useRef, useEffect, useCallback } from 'react'
import { T, INR } from '../../tokens'

const STRATEGIES = [
  { value:'scalper',      label:'Options Scalper · RSI-14 NIFTY' },
  { value:'sma_crossover',label:'SMA Crossover · 9/21 Equity' },
]
const SEGMENTS = [
  { value:'NSE_FNO', label:'NSE F&O · Options/Futures' },
  { value:'NSE_EQ',  label:'NSE Equity · Cash' },
  { value:'MCX',     label:'MCX · Commodity' },
]

function MonoSelect({ value, onChange, options }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} style={{
      background:T.bg2, border:`1px solid ${T.line2}`, color:T.ink0,
      fontFamily:T.mono, fontSize:11, padding:'8px 12px',
      textTransform:'uppercase', letterSpacing:'0.1em', cursor:'pointer',
      outline:'none', width:'100%',
    }}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

function StockSearch({ segment, onSelect }) {
  const [q, setQ]         = useState('')
  const [results, setRes] = useState([])
  const [open, setOpen]   = useState(false)
  const debounceRef       = useRef(null)

  const search = useCallback((val) => {
    clearTimeout(debounceRef.current)
    if (val.length < 2) { setRes([]); return }
    debounceRef.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/instruments/search?q=${encodeURIComponent(val)}&segment=${segment}`)
        const d = await r.json()
        if (d.ok) setRes(d.results.slice(0, 8))
      } catch {}
    }, 300)
  }, [segment])

  return (
    <div style={{position:'relative'}}>
      <input
        value={q} placeholder="SEARCH SYMBOL…"
        onChange={e => { setQ(e.target.value); search(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        style={{
          width:'100%', background:T.bg2, border:`1px solid ${T.line2}`, color:T.ink0,
          fontFamily:T.mono, fontSize:11, padding:'8px 12px',
          textTransform:'uppercase', letterSpacing:'0.1em', outline:'none',
        }}
      />
      {open && results.length > 0 && (
        <div style={{
          position:'absolute', top:'100%', left:0, right:0, zIndex:20,
          background:T.bg2, border:`1px solid ${T.line2}`, maxHeight:200, overflowY:'auto',
        }}>
          {results.map((r, i) => (
            <div key={i} onMouseDown={() => { onSelect(r); setQ(r.symbol); setOpen(false) }}
              style={{
                padding:'8px 12px', cursor:'pointer', fontFamily:T.mono, fontSize:11,
                color:T.ink0, letterSpacing:'0.1em', borderBottom:`1px solid ${T.line}`,
              }}
              onMouseEnter={e => e.currentTarget.style.background = T.bg3}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              <span style={{color:T.cyan}}>{r.symbol}</span>
              <span style={{color:T.ink2, marginLeft:10, fontSize:9}}>{r.name} · LOT {r.lot_size}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function KillSwitch({ onKill }) {
  const [armed, setArmed]   = useState(false)
  const [fired, setFired]   = useState(false)
  const [busy, setBusy]     = useState(false)

  async function handleClick() {
    if (fired) return
    if (!armed) { setArmed(true); return }
    setBusy(true)
    try {
      const r = await fetch('/api/killswitch', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' })
      const d = await r.json()
      if (d.ok) { setFired(true); onKill?.() }
    } finally { setBusy(false) }
  }

  // Auto-disarm after 4s if not confirmed
  useEffect(() => {
    if (!armed || fired) return
    const t = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(t)
  }, [armed, fired])

  return (
    <button onClick={handleClick} disabled={busy || fired} style={{
      width:'100%', padding:'12px 0', cursor: fired ? 'default' : 'pointer',
      background: fired ? '#1a0a0a' : armed ? '#2d0f0f' : '#1a0808',
      border: `2px solid ${fired ? T.ink3 : T.red}`,
      color: fired ? T.ink3 : T.red,
      fontFamily:T.mono, fontSize:12, fontWeight:600,
      letterSpacing:'0.2em', textTransform:'uppercase',
      boxShadow: armed && !fired ? `0 0 20px oklch(0.68 0.22 25 / 0.3)` : 'none',
      transition:'all .2s',
    }}>
      {busy ? 'HALTING…' : fired ? '⛔ HALTED' : armed ? '⚠ CONFIRM KILL?' : '🔴 KILL SWITCH'}
    </button>
  )
}

export default function ControlPanel({ config, onSwitch, onKill }) {
  const cfg = config?.data
  const [form, setForm]     = useState({ strategy:'scalper', segment:'NSE_FNO', security_id:'13', symbol:'NIFTY', quantity:1, lot_size:75 })
  const [price, setPrice]   = useState(0)
  const [applying, setApplying] = useState(false)
  const [msg, setMsg]       = useState(null)

  // Seed form from live config
  useEffect(() => {
    if (cfg) setForm(f => ({ ...f, strategy:cfg.strategy||f.strategy, segment:cfg.segment||f.segment, security_id:cfg.security_id||f.security_id, symbol:cfg.symbol||f.symbol, quantity:cfg.quantity||f.quantity }))
  }, [cfg])

  // Fetch price when security changes
  useEffect(() => {
    if (!form.security_id || form.segment === 'NSE_FNO') return
    fetch(`/api/instruments/price?security_id=${form.security_id}&segment=${form.segment}`)
      .then(r => r.json()).then(d => { if (d.ok) setPrice(d.price) }).catch(()=>{})
  }, [form.security_id, form.segment])

  function set(k, v) { setForm(f => ({ ...f, [k]: v })) }

  const capital = (() => {
    const qty = form.quantity * form.lot_size
    if (form.segment === 'NSE_EQ')  return qty * price
    if (form.segment === 'MCX')     return qty * price * 0.15
    return qty * price  // F&O: premium × qty
  })()

  async function apply() {
    setApplying(true); setMsg(null)
    try {
      const r = await fetch('/api/strategy/switch', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ strategy:form.strategy, segment:form.segment, security_id:form.security_id, quantity:form.lot_size, num_lots:form.quantity }),
      })
      const d = await r.json()
      setMsg(d.ok ? { ok:true, text:`Switched to ${form.strategy}` } : { ok:false, text:d.error })
      if (d.ok) onSwitch?.()
    } catch (e) { setMsg({ ok:false, text:String(e) }) }
    finally { setApplying(false) }
  }

  const label = { fontFamily:T.mono, fontSize:9, color:T.ink2, letterSpacing:'0.2em', textTransform:'uppercase', marginBottom:6 }

  return (
    <div style={{background:T.bg1, border:`1px solid ${T.line}`, marginBottom:14}}>
      <div style={{
        display:'flex', alignItems:'center', gap:10,
        padding:'10px 14px', borderBottom:`1px solid ${T.line}`,
        fontFamily:T.mono, fontSize:10, color:T.ink2, textTransform:'uppercase', letterSpacing:'0.16em',
      }}>
        <span style={{color:T.ink0}}>STRATEGY CONTROL PANEL</span>
        {msg && (
          <span style={{marginLeft:'auto', color: msg.ok ? T.green : T.red, fontSize:10}}>{msg.text}</span>
        )}
      </div>

      <div style={{padding:'16px', display:'grid', gridTemplateColumns:'1fr 1fr 1.5fr 1fr auto', gap:14, alignItems:'end'}}>

        {/* Strategy */}
        <div>
          <div style={label}>STRATEGY</div>
          <MonoSelect value={form.strategy} onChange={v => set('strategy', v)} options={STRATEGIES}/>
        </div>

        {/* Segment */}
        <div>
          <div style={label}>SEGMENT</div>
          <MonoSelect value={form.segment} onChange={v => { set('segment', v); if (v==='NSE_FNO'){set('security_id','13');set('symbol','NIFTY');set('lot_size',75)} }} options={SEGMENTS}/>
        </div>

        {/* Stock search (equity/commodity) or static for FNO */}
        <div>
          <div style={label}>{form.segment === 'NSE_FNO' ? 'INSTRUMENT' : 'STOCK / SYMBOL'}</div>
          {form.segment === 'NSE_FNO' ? (
            <div style={{background:T.bg2, border:`1px solid ${T.line2}`, color:T.cyan, fontFamily:T.mono, fontSize:11, padding:'8px 12px', letterSpacing:'0.1em'}}>
              NIFTY · IDX_I · AUTO-ATM
            </div>
          ) : (
            <StockSearch segment={form.segment} onSelect={s => { set('security_id', s.security_id); set('symbol', s.symbol); set('lot_size', s.lot_size) }}/>
          )}
        </div>

        {/* Quantity slider */}
        <div>
          <div style={label}>
            LOTS / QTY
            <span style={{color:T.cyan, marginLeft:8}}>{form.quantity} × {form.lot_size} = {form.quantity * form.lot_size} units</span>
          </div>
          <input type="range" min={1} max={10} value={form.quantity} onChange={e => set('quantity', Number(e.target.value))}
            style={{width:'100%', accentColor:T.green, cursor:'pointer'}}/>
          <div style={{fontFamily:T.mono, fontSize:9, color:T.ink2, marginTop:4, letterSpacing:'0.1em'}}>
            EST. CAPITAL: <b style={{color: capital > 50000 ? T.amber : T.green}}>
              {capital > 0 ? `₹${Math.round(capital).toLocaleString('en-IN')}` : '—'}
            </b>
          </div>
        </div>

        {/* Apply */}
        <button onClick={apply} disabled={applying} style={{
          padding:'8px 20px', background:'transparent',
          border:`1px solid ${T.green}`, color:T.green,
          fontFamily:T.mono, fontSize:11, fontWeight:600,
          letterSpacing:'0.16em', textTransform:'uppercase', cursor:'pointer',
          opacity: applying ? 0.6 : 1, whiteSpace:'nowrap',
        }}>
          {applying ? 'SWITCHING…' : 'APPLY ⟶'}
        </button>
      </div>

      {/* Kill switch */}
      <div style={{padding:'0 16px 16px'}}>
        <KillSwitch onKill={onKill}/>
      </div>
    </div>
  )
}
