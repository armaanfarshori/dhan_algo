export const T = {
  bg0:'#07080a', bg1:'#0c0e12', bg2:'#11141a', bg3:'#161a22',
  line:'#1f242e', line2:'#2a3140',
  ink0:'#e7ebf2', ink1:'#a9b2c2', ink2:'#6b7589', ink3:'#424b5c',
  green:'oklch(0.78 0.19 145)', greenD:'oklch(0.45 0.16 145)',
  red:'oklch(0.68 0.22 25)',     redD:'oklch(0.42 0.18 25)',
  amber:'oklch(0.82 0.16 75)',
  cyan:'oklch(0.82 0.12 200)',
  mono:"'JetBrains Mono', monospace",
  dot:"'VT323', monospace",
  ui:"'Inter', system-ui, sans-serif",
}

export const INR = v => '₹' + Number(v).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2})
export const INR0 = v => '₹' + Number(v).toLocaleString('en-IN', {minimumFractionDigits:0,maximumFractionDigits:0})
export const colorPnl = v => Number(v) >= 0 ? T.green : T.red

export function fmtUptime(s) {
  const h = String(Math.floor(s/3600)).padStart(2,'0')
  const m = String(Math.floor((s%3600)/60)).padStart(2,'0')
  return `${h}h ${m}m`
}
export function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-IN', {hour12:false})
}
