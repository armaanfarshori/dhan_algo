import { T } from '../../tokens'

const FNO_STEPS = [
  { n:'01', label:'FETCH',   name:'IDX_I Bulk',      desc:'All 6 underlying\nprices · one call',    lat:'~40ms' },
  { n:'02', label:'SIGNAL',  name:'RSI-14',           desc:'Cross 30 oversold\nCross 70 overbought', lat:'<1ms'  },
  { n:'03', label:'ATM',     name:'find_atm_for_idx', desc:'Instrument master\nlookup by index',     lat:'<1ms'  },
  { n:'04', label:'PREMIUM', name:'Option Quote',     desc:'Validate premium\nmin/max filter',       lat:'~40ms' },
  { n:'05', label:'RISK',    name:'check_order',      desc:'Capital gate\nposition limits',          lat:'<1ms'  },
  { n:'06', label:'FILL',    name:'place_order',      desc:'MARKET · MARGIN\nwait for fill',         lat:'~700ms'},
  { n:'07', label:'OCO',     name:'forever/orders',   desc:'Target = BEP+₹5\nStop  = entry-₹5',     lat:'~600ms'},
]

function StepCell({ step, activeStep }) {
  const isActive = step.n === activeStep
  return (
    <div style={{
      padding: '12px 12px 10px', flex: 1,
      borderRight: `1px solid ${T.line}`,
      background: isActive ? 'oklch(0.20 0.08 145 / 0.35)' : 'transparent',
      boxShadow: isActive ? `inset 0 0 0 1px ${T.green}, inset 0 0 20px oklch(0.55 0.18 145 / 0.2)` : 'none',
      transition: 'background .2s',
      position: 'relative',
    }}>
      <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.2em', textTransform: 'uppercase' }}>
        {step.n} · {step.label}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 600, color: isActive ? T.green : T.ink0, marginTop: 4 }}>
        {step.name}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, marginTop: 5, lineHeight: 1.5, whiteSpace: 'pre-line', letterSpacing: '0.04em' }}>
        {step.desc}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink1, marginTop: 6, letterSpacing: '0.1em' }}>
        ⟶ <b style={{ color: isActive ? T.green : T.cyan }}>{step.lat}</b>
      </div>
    </div>
  )
}

// Compact index RSI bar row shown below the pipeline
function IndexRow({ name, idx }) {
  if (!idx) return null
  const rsi    = idx.rsi || 0
  const inPos  = idx.in_position
  const color  = rsi > 0 && rsi <= 30 ? T.green : rsi >= 70 ? T.red : T.line2
  const pct    = Math.min(rsi, 100)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 14px', borderBottom: `1px solid ${T.line}` }}>
      <div style={{ fontFamily: T.mono, fontSize: 10, color: inPos ? T.green : T.ink2, fontWeight: inPos ? 600 : 400, width: 100, flexShrink: 0 }}>
        {inPos && <span style={{ color: T.green, marginRight: 4 }}>●</span>}
        {name}
        {inPos && <span style={{ color: T.amber, fontSize: 9, marginLeft: 6 }}>{idx.option_type}</span>}
      </div>
      <div style={{ flex: 1, height: 5, background: T.bg3, borderRadius: 2, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${pct}%`, background: color, borderRadius: 2, transition: 'width .5s, background .3s' }} />
      </div>
      <div style={{ fontFamily: T.dot, fontSize: 16, color: color, width: 40, textAlign: 'right', flexShrink: 0 }}>
        {rsi > 0 ? rsi.toFixed(1) : '—'}
      </div>
      <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.1em', width: 80, textAlign: 'right', flexShrink: 0 }}>
        {rsi > 0 && rsi <= 30 ? 'OVERSOLD' : rsi >= 70 ? 'OVERBOUGHT' : 'NEUTRAL'}
      </div>
    </div>
  )
}

export default function Pipeline({ scalper, scanner }) {
  const sc  = scanner?.data
  const isFno = sc?.mode === 'index_options'

  // Determine active pipeline step from scanner state
  let activeStep = '01'
  if (isFno && sc?.open_positions > 0) activeStep = '07'

  // Header meta
  const positions = sc?.open_positions ?? 0
  const orders    = sc?.orders_placed  ?? 0

  return (
    <div style={{ marginBottom: 14 }}>
      {/* Pipeline header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 14,
        padding: '10px 14px', borderTop: `1px solid ${T.line}`, borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em',
        background: T.bg1,
      }}>
        <span style={{ background: T.greenD, color: T.green, padding: '2px 6px', fontSize: 9 }}>LIVE</span>
        <span style={{ color: T.ink0 }}>
          {isFno ? 'INDEX OPTIONS PIPELINE · IDX_I → ATM → OCO' : 'SCALPER PIPELINE · _tick → _enter → OCO'}
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 18 }}>
          <span>POSITIONS <b style={{ color: positions > 0 ? T.amber : T.ink0 }}>{positions}</b></span>
          <span>ORDERS <b style={{ color: T.ink0 }}>{orders}</b></span>
          {isFno && sc?.active_indices && (
            <span>SCANNING <b style={{ color: T.cyan }}>{sc.active_indices.join(' · ')}</b></span>
          )}
        </div>
      </div>

      {/* Pipeline steps */}
      <div style={{ display: 'flex', background: T.bg1, borderBottom: `1px solid ${T.line}` }}>
        {(isFno ? FNO_STEPS : FNO_STEPS.slice(0, 6)).map(step => (
          <StepCell key={step.n} step={step} activeStep={activeStep} />
        ))}
      </div>

      {/* Per-index RSI strip (F&O mode only) */}
      {isFno && sc?.indices && (
        <div style={{ background: T.bg1 }}>
          <div style={{
            fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.18em',
            textTransform: 'uppercase', padding: '6px 14px 4px',
            borderBottom: `1px solid ${T.line}`, display: 'flex', justifyContent: 'space-between',
          }}>
            <span>INDEX · RSI-14 GAUGE</span>
            <span style={{ color: T.ink3 }}>BELOW 30 = SIGNAL · ABOVE 70 = SIGNAL</span>
          </div>
          {Object.entries(sc.indices).map(([name, idx]) => (
            <IndexRow key={name} name={name} idx={idx} />
          ))}
        </div>
      )}
    </div>
  )
}
