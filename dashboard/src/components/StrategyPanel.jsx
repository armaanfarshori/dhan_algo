import { INR } from '../utils'

const s = {
  card:  { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: 16 },
  label: { fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 8 },
  row:   { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' },
  name:  { color: 'var(--blue)', fontSize: 14, fontWeight: 'bold' },
  track: { height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden', marginTop: 4 },
  fill:  pct => ({ height: '100%', background: 'var(--blue)', borderRadius: 3, width: `${pct}%`, transition: 'width .4s ease' }),
  wlabel:{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--muted)', marginBottom: 4, marginTop: 10 },
  ready: { display: 'inline-block', background: '#1c2d1e', color: 'var(--green)', border: '1px solid var(--green)', borderRadius: 12, padding: '2px 10px', fontSize: 11 },
  grid:  { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginTop: 12 },
  cell:  { background: 'var(--bg)', borderRadius: 6, padding: '8px 10px' },
  cl:    { fontSize: 10, color: 'var(--muted)' },
  cv:    { fontSize: 14, fontWeight: 'bold', marginTop: 2 },
}

export default function StrategyPanel({ status, scalper }) {
  const d  = status?.data
  const sc = scalper?.data
  const w  = d?.warmup ?? {}
  const pct = w.slow_required ? Math.round((w.slow_current / w.slow_required) * 100) : 0
  const pos = d?.position ?? 0

  const posColor = pos > 0 ? 'var(--green)' : pos < 0 ? 'var(--red)' : 'var(--muted)'
  const posLabel = pos > 0 ? `+${pos} LONG` : pos < 0 ? `${pos} SHORT` : 'FLAT'

  return (
    <div style={s.card}>
      <div style={s.label}>Strategy</div>
      <div style={s.row}>
        <span style={s.name}>{d?.strategy_name ?? '—'}</span>
        {w.ready
          ? <span style={s.ready}>● READY</span>
          : null}
      </div>

      {!w.ready && (
        <div>
          <div style={s.wlabel}>
            <span>Warming up (slow SMA)</span>
            <span>{w.slow_current ?? 0} / {w.slow_required ?? 21}</span>
          </div>
          <div style={s.track}><div style={s.fill(pct)} /></div>
        </div>
      )}

      <div style={s.grid}>
        <div style={s.cell}>
          <div style={s.cl}>Position</div>
          <div style={{ ...s.cv, color: posColor }}>{posLabel}</div>
        </div>
        <div style={s.cell}>
          <div style={s.cl}>Entry Price</div>
          <div style={s.cv}>{d?.entry_price ? INR(d.entry_price) : '—'}</div>
        </div>
        {sc && (
          <>
            <div style={s.cell}>
              <div style={s.cl}>Breakeven</div>
              <div style={s.cv}>{sc.breakeven_premium ? INR(sc.breakeven_premium) : '—'}</div>
            </div>
            <div style={s.cell}>
              <div style={s.cl}>OCO State</div>
              <div style={{ ...s.cv, color: sc.oco_state === 'IN_POSITION' ? 'var(--yellow)' : 'var(--muted)' }}>
                {sc.oco_state ?? '—'}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
