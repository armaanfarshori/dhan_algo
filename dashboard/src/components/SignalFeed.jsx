import { INR, fmtTime } from '../utils'

const s = {
  card:   { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: 16, display: 'flex', flexDirection: 'column' },
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 },
  label:  { fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)' },
  count:  { color: 'var(--muted)', fontSize: 11 },
  scroll: { overflowY: 'auto', maxHeight: 260 },
  th:     { textAlign: 'left', fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)', padding: '6px 8px', borderBottom: '1px solid var(--border)', position: 'sticky', top: 0, background: 'var(--surface)' },
  td:     { padding: '7px 8px', fontSize: 12 },
  empty:  { color: 'var(--muted)', padding: '16px 8px', fontSize: 12 },
}

const ACTION_COLOR = { BUY: 'var(--green)', SELL: 'var(--red)', EXIT: 'var(--yellow)' }

export default function SignalFeed({ signals }) {
  const rows = signals?.data ?? []
  return (
    <div style={s.card}>
      <div style={s.header}>
        <span style={s.label}>Signal Feed (last 50)</span>
        <span style={s.count}>{rows.length} signal{rows.length !== 1 ? 's' : ''}</span>
      </div>
      <div style={s.scroll}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['Time', 'Action', 'Price', 'Reason'].map(h => (
                <th key={h} style={s.th}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0
              ? <tr><td colSpan={4} style={s.empty}>Waiting for first signal…</td></tr>
              : rows.map((sig, i) => (
                  <tr key={i} style={{ borderBottom: '1px solid #21262d' }}>
                    <td style={s.td}>{fmtTime(sig.timestamp)}</td>
                    <td style={{ ...s.td, color: ACTION_COLOR[sig.action] ?? 'var(--text)', fontWeight: 'bold' }}>{sig.action}</td>
                    <td style={s.td}>{sig.price ? INR(sig.price) : '—'}</td>
                    <td style={{ ...s.td, color: 'var(--muted)' }}>{sig.reason}</td>
                  </tr>
                ))
            }
          </tbody>
        </table>
      </div>
    </div>
  )
}
