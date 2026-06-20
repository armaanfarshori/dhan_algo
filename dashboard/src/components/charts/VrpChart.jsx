import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'

const COLOR_VIX      = 'hsl(var(--sky))'
const COLOR_REALIZED = 'hsl(var(--amber))'

/** Format a vol fraction (0.13 → "13.2%") */
const fmtPct = (v) => (v == null || !Number.isFinite(Number(v))) ? '—' : `${(Number(v) * 100).toFixed(1)}%`

/** Compact date label: "Jun '26" or "MM-DD" for intra-year density */
const fmtDate = (dateStr) => {
  if (!dateStr) return ''
  const d = new Date(dateStr + 'T00:00:00')
  if (Number.isNaN(d.getTime())) return dateStr
  const mon = d.toLocaleString('en-US', { month: 'short' })
  const yr  = String(d.getFullYear()).slice(2)
  return `${mon} '${yr}`
}

/** Dual-line VRP chart: India VIX vs NIFTY realized vol.
 *
 *  Props
 *  -----
 *  series  : { date: "YYYY-MM-DD", realized: number|null, vix: number }[]
 *            Values are fractions (0.13 = 13 %).
 *  height  : number  (default 240)
 */
export default function VrpChart({ series, height = 240 }) {
  if (!series || series.length < 2) {
    return (
      <div
        className="flex items-center justify-center text-[11px] text-muted-foreground mono"
        style={{ height }}
      >
        No data yet
      </div>
    )
  }

  return (
    <div>
      {/* Inline legend */}
      <div
        style={{
          display: 'flex',
          gap: '14px',
          paddingBottom: '6px',
          fontFamily: 'var(--font-mono,monospace)',
          fontSize: 10,
          color: 'hsl(var(--faint))',
        }}
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: COLOR_VIX,
              flexShrink: 0,
            }}
          />
          India VIX
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              borderRadius: '50%',
              background: COLOR_REALIZED,
              flexShrink: 0,
            }}
          />
          Realized 20d
        </span>
      </div>

      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={series} margin={{ top: 8, right: 10, bottom: 0, left: 6 }}>
          <CartesianGrid
            stroke="hsl(var(--border))"
            strokeDasharray="2 4"
            vertical={false}
          />
          <XAxis
            dataKey="date"
            tickFormatter={fmtDate}
            tick={{ fontFamily: 'var(--font-mono,monospace)', fontSize: 9.5, fill: 'hsl(var(--faint))' }}
            tickLine={false}
            axisLine={{ stroke: 'hsl(var(--border))' }}
            minTickGap={56}
            interval="preserveStartEnd"
          />
          <YAxis
            tickFormatter={fmtPct}
            tick={{ fontFamily: 'var(--font-mono,monospace)', fontSize: 9.5, fill: 'hsl(var(--faint))' }}
            tickLine={false}
            axisLine={false}
            width={48}
          />
          <Tooltip
            contentStyle={{
              background: 'hsl(var(--card))',
              border: '1px solid hsl(var(--border2))',
              fontFamily: 'var(--font-mono,monospace)',
              fontSize: 10,
              borderRadius: 6,
            }}
            labelStyle={{ color: 'hsl(var(--muted-fg))' }}
            formatter={(value, name) => [
              fmtPct(value),
              name === 'vix' ? 'India VIX' : 'Realized 20d',
            ]}
          />
          <Line
            type="monotone"
            dataKey="vix"
            stroke={COLOR_VIX}
            strokeWidth={1.6}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
          <Line
            type="monotone"
            dataKey="realized"
            stroke={COLOR_REALIZED}
            strokeWidth={1.6}
            dot={false}
            isAnimationActive={false}
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
