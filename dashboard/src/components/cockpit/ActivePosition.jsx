import { T, INR } from '../../tokens'

function StatCell({ label, value, color }) {
  return (
    <div style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '8px 10px' }}>
      <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, letterSpacing: '0.16em', textTransform: 'uppercase', marginBottom: 4 }}>{label}</div>
      <div style={{ fontFamily: 'VT323', fontSize: 20, color: color || T.ink0, lineHeight: 1 }}>{value}</div>
    </div>
  )
}

function FnoPosition({ name, idx }) {
  const upnl   = idx.unrealized_pnl || 0
  const pctChg = idx.entry && idx.current_premium
    ? ((idx.current_premium - idx.entry) / idx.entry * 100).toFixed(1)
    : null

  return (
    <div style={{
      border: `1px solid ${upnl >= 0 ? T.green : T.red}`,
      background: upnl >= 0 ? 'oklch(0.18 0.08 145 / 0.12)' : 'oklch(0.18 0.08 25 / 0.12)',
      padding: '12px 14px', marginBottom: 10,
      boxShadow: `0 0 16px ${upnl >= 0 ? 'oklch(0.55 0.18 145 / 0.08)' : 'oklch(0.55 0.18 25 / 0.08)'}`,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            width: 34, height: 34, border: `1px solid ${T.green}`, color: T.green,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontFamily: T.mono, fontWeight: 700, fontSize: 11,
          }}>
            {idx.option_type}
          </div>
          <div>
            <div style={{ fontFamily: T.mono, fontSize: 12, fontWeight: 600, color: T.green }}>
              {name} {parseInt(idx.strike).toLocaleString('en-IN')} {idx.option_type}
            </div>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, marginTop: 2 }}>
              {idx.expiry} · IN POSITION
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontFamily: 'VT323', fontSize: 28, color: upnl >= 0 ? T.green : T.red, lineHeight: 1 }}>
            {upnl >= 0 ? '+' : ''}{INR(upnl)}
          </div>
          <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>UNREALISED</div>
        </div>
      </div>

      {/* Stats grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 6 }}>
        <StatCell label="ENTRY"   value={idx.entry          ? `₹${idx.entry}` : '—'}                               color={T.ink0} />
        <StatCell label="NOW"     value={idx.current_premium ? `₹${idx.current_premium?.toFixed(2)}` : '—'}
          color={pctChg ? (parseFloat(pctChg) >= 0 ? T.green : T.red) : T.ink0} />
        <StatCell label="BEP"     value={idx.breakeven      ? `₹${idx.breakeven?.toFixed(2)}` : '—'}               color={T.amber} />
        <StatCell label="TARGET"  value={idx.target         ? `₹${idx.target}` : '—'}                              color={T.green} />
        <StatCell label="STOP"    value={idx.stop           ? `₹${idx.stop}` : '—'}                                color={T.red}   />
      </div>
    </div>
  )
}

export default function ActivePosition({ fnoScanner, paperPositions }) {
  // Collect all open F&O positions from scanner indices
  const indices  = fnoScanner?.data?.indices || {}
  const fnoOpen  = Object.entries(indices).filter(([, v]) => v.in_position)

  // Equity open positions from paper positions
  const pp       = paperPositions?.data?.data ?? []
  const eqOpen   = pp.filter(p => p.engine === 'EQ')

  const anyOpen  = fnoOpen.length > 0 || eqOpen.length > 0

  return (
    <div style={{
      background: T.bg1,
      border: `1px solid ${anyOpen ? T.green : T.line}`,
      marginBottom: 14,
      boxShadow: anyOpen ? `0 0 24px oklch(0.55 0.18 145 / 0.08)` : 'none',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
        fontFamily: T.mono, fontSize: 10, color: T.ink2, textTransform: 'uppercase', letterSpacing: '0.16em',
      }}>
        {anyOpen && <span style={{ background: T.greenD, color: T.green, padding: '2px 6px', fontSize: 9 }}>LIVE</span>}
        <span style={{ color: T.ink0 }}>ACTIVE POSITIONS</span>
        <span style={{ color: anyOpen ? T.amber : T.ink3, marginLeft: 'auto', fontSize: 9 }}>
          {fnoOpen.length} F&O · {eqOpen.length} EQ
        </span>
      </div>

      <div style={{ padding: fnoOpen.length || eqOpen.length ? 14 : 0 }}>
        {!anyOpen && (
          <div style={{ padding: '28px 14px', textAlign: 'center', fontFamily: T.mono, fontSize: 11, color: T.ink3, letterSpacing: '0.14em', textTransform: 'uppercase' }}>
            NO ACTIVE POSITIONS · FLAT
          </div>
        )}

        {/* F&O positions */}
        {fnoOpen.map(([name, idx]) => (
          <FnoPosition key={name} name={name} idx={idx} />
        ))}

        {/* Equity positions */}
        {eqOpen.map((p, i) => {
          const upnl = p.unrealized_pnl || 0
          const chg  = p.change_pct || 0
          return (
            <div key={i} style={{
              border: `1px solid ${upnl >= 0 ? T.cyan : T.red}`,
              background: T.bg2, padding: '10px 12px', marginBottom: 8,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                <div>
                  <div style={{ fontFamily: T.mono, fontSize: 11, fontWeight: 600, color: T.cyan }}>{p.symbol}</div>
                  <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>{p.segment} · QTY {p.qty}</div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div style={{ fontFamily: 'VT323', fontSize: 24, color: upnl >= 0 ? T.green : T.red }}>{upnl >= 0 ? '+' : ''}{INR(upnl)}</div>
                  <div style={{ fontFamily: T.mono, fontSize: 9, color: chg >= 0 ? T.green : T.red }}>{chg >= 0 ? '+' : ''}{chg}%</div>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 6 }}>
                <StatCell label="ENTRY" value={p.entry_price ? `₹${p.entry_price?.toFixed(2)}` : '—'} color={T.ink0} />
                <StatCell label="NOW"   value={p.current_price ? `₹${p.current_price?.toFixed(2)}` : '—'} color={upnl >= 0 ? T.green : T.red} />
                <StatCell label="CHG%"  value={`${chg >= 0 ? '+' : ''}${chg}%`} color={chg >= 0 ? T.green : T.red} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
