// Formatters shared across the dashboard. Colors/fonts now live in index.css
// as design tokens (the old `T` palette was retired with the shadcn redesign).

export const INR = v => '₹' + Number(v).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})
export const INR0 = v => '₹' + Number(v).toLocaleString('en-IN', {minimumFractionDigits:0,maximumFractionDigits:0})
export const colorPnl = v => Number(v) >= 0 ? 'hsl(var(--profit))' : 'hsl(var(--loss))'

export function fmtUptime(s) {
  const h = String(Math.floor(s/3600)).padStart(2,'0')
  const m = String(Math.floor((s%3600)/60)).padStart(2,'0')
  return `${h}h ${m}m`
}
export function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {hour12:false, timeZone:'Asia/Kolkata'})
}
export const istDateKey = d => new Date(d).toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
