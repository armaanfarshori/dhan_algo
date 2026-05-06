import { INR, colorVar } from '../utils'

const s = {
  card:    { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: 16 },
  label:   { fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 8 },
  row:     { display: 'flex', alignItems: 'center', gap: 12, marginTop: 8 },
  grid:    { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 12 },
  cell:    { background: 'var(--bg)', borderRadius: 6, padding: '8px 10px' },
  clabel:  { fontSize: 10, color: 'var(--muted)', marginBottom: 2 },
  cval:    { fontSize: 14, fontWeight: 'bold' },
}

function Dot({ halted }) {
  return (
    <span style={{
      width: 10, height: 10, borderRadius: '50%', flexShrink: 0, display: 'inline-block',
      background: halted ? 'var(--red)' : 'var(--green)',
      boxShadow: halted ? '0 0 6px var(--red)' : '0 0 6px var(--green)',
      animation: halted ? 'pulse 1s infinite' : 'none',
    }} />
  )
}

export default function RiskPanel({ risk }) {
  const d = risk?.data
  const halted = d?.halted ?? false
  const r = d?.realised_pnl   ?? 0
  const u = d?.unrealised_pnl ?? 0

  return (
    <div style={{ ...s.card, border: halted ? '1px solid var(--red)' : '1px solid var(--border)' }}>
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }`}</style>
      <div style={s.label}>Risk Manager</div>
      <div style={s.row}>
        <Dot halted={halted} />
        <span style={{ color: halted ? 'var(--red)' : 'var(--green)' }}>
          {halted ? `HALTED — ${d?.halt_reason}` : 'OK — All limits within range'}
        </span>
      </div>
      <div style={s.grid}>
        <div style={s.cell}>
          <div style={s.clabel}>Realised</div>
          <div style={{ ...s.cval, color: colorVar(r) }}>{INR(r)}</div>
        </div>
        <div style={s.cell}>
          <div style={s.clabel}>Unrealised</div>
          <div style={{ ...s.cval, color: colorVar(u) }}>{INR(u)}</div>
        </div>
        <div style={s.cell}>
          <div style={s.clabel}>Daily Limit</div>
          <div style={s.cval}>₹5,000</div>
        </div>
      </div>
    </div>
  )
}
