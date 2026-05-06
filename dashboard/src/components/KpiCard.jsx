const styles = {
  card: {
    background: 'var(--surface)', border: '1px solid var(--border)',
    borderRadius: 8, padding: 16,
  },
  label: {
    fontSize: 10, letterSpacing: 1, textTransform: 'uppercase',
    color: 'var(--muted)', marginBottom: 8,
  },
  value: { fontSize: 22, fontWeight: 'bold' },
  sub:   { fontSize: 11, color: 'var(--muted)', marginTop: 4 },
}

export default function KpiCard({ label, value, sub, valueColor }) {
  return (
    <div style={styles.card}>
      <div style={styles.label}>{label}</div>
      <div style={{ ...styles.value, color: valueColor || 'var(--text)' }}>{value ?? '—'}</div>
      {sub && <div style={styles.sub}>{sub}</div>}
    </div>
  )
}
