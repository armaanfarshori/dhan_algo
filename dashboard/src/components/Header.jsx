import { fmtUptime } from '../utils'

const styles = {
  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '14px 24px', borderBottom: '1px solid var(--border)',
    background: 'var(--surface)',
  },
  title: { fontSize: 15, letterSpacing: 1, color: 'var(--blue)', fontWeight: 'bold' },
  right:  { display: 'flex', gap: 16, alignItems: 'center' },
  uptime: { color: 'var(--muted)', fontSize: 12 },
  dot: active => ({
    width: 7, height: 7, borderRadius: '50%', display: 'inline-block',
    background: active ? 'var(--green)' : 'var(--muted)',
    transition: 'background 0.2s',
  }),
}

function ModeBadge({ mode }) {
  const isLive = mode === 'LIVE'
  return (
    <span style={{
      padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 'bold',
      letterSpacing: 0.5,
      background: isLive ? '#2d1c1c' : '#1c2d1e',
      color: isLive ? 'var(--red)' : 'var(--green)',
      border: `1px solid ${isLive ? 'var(--red)' : 'var(--green)'}`,
    }}>
      {mode || 'PAPER'}
    </span>
  )
}

export default function Header({ status, refreshing }) {
  const d = status?.data
  return (
    <header style={styles.header}>
      <span style={styles.title}>⚡ DHANHQ ALGO PLATFORM</span>
      <div style={styles.right}>
        <ModeBadge mode={d?.mode} />
        <span style={styles.uptime}>{d ? fmtUptime(d.uptime_seconds) : '--:--:--'}</span>
        <span style={styles.dot(refreshing)} />
      </div>
    </header>
  )
}
