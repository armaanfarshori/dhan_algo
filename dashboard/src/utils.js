export const INR = v =>
  '₹' + Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const colorVar = v =>
  Number(v) > 0 ? 'var(--green)' : Number(v) < 0 ? 'var(--red)' : 'var(--muted)'

export function fmtUptime(secs) {
  const h = String(Math.floor(secs / 3600)).padStart(2, '0')
  const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0')
  const s = String(secs % 60).padStart(2, '0')
  return `↑ ${h}:${m}:${s}`
}

export function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', { hour12: false })
}
