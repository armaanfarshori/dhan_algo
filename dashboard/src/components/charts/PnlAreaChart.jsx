import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { INR0 } from '@/tokens'

/** Shared P&L area chart — used by Signals "Intraday P&L" and Portfolio
 *  "Equity Curve" so they share one design. data: [{ t, v }].
 *
 *  emptyTicks / emptyNote (optional) describe the EMPTY state: pass the axis
 *  labels the chart *would* span (e.g. the trading session) and a note, and the
 *  placeholder renders that axis instead of a bare "No data yet". Callers that
 *  omit them keep the previous behaviour exactly. */
export function PnlAreaChart({
  data, height = 210, xKey = 't', dataKey = 'v',
  emptyTicks = null, emptyNote = 'No data yet',
}) {
  const pts  = data ?? []
  const last = pts.length ? (pts[pts.length - 1][dataKey] ?? 0) : 0
  const up   = last >= 0
  const color = up ? 'hsl(var(--profit))' : 'hsl(var(--loss))'
  const gid   = `pnlgrad-${dataKey}`

  if (pts.length < 2) {
    // Placeholder axis: shows the window the chart covers WITHOUT plotting a
    // single point, so an empty session can never be mistaken for a flat line.
    if (emptyTicks?.length) {
      return (
        <div className="flex flex-col" style={{ height }}>
          <div className="relative min-h-0 flex-1">
            <div
              className="absolute inset-x-0 top-1/2 border-t border-dashed border-border"
              aria-hidden="true"
            />
            <div className="absolute inset-0 grid place-items-center">
              <span className="mono text-[11px] text-muted-foreground">{emptyNote}</span>
            </div>
          </div>
          <div className="flex justify-between border-t border-border px-1 pt-1.5">
            {emptyTicks.map((t, i) => (
              <span key={i} className="mono text-[9.5px] text-faint">{t}</span>
            ))}
          </div>
        </div>
      )
    }
    return (
      <div className="flex items-center justify-center text-[11px] text-muted-foreground mono" style={{ height }}>
        {emptyNote}
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={pts} margin={{ top: 8, right: 10, bottom: 0, left: 6 }}>
        <defs>
          <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.20} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey={xKey}
          tick={{ fontFamily: 'var(--font-mono,monospace)', fontSize: 9.5, fill: 'hsl(var(--faint))' }}
          tickLine={false}
          axisLine={{ stroke: 'hsl(var(--border))' }}
          minTickGap={56}
          interval="preserveStartEnd"
        />
        <YAxis
          tick={{ fontFamily: 'var(--font-mono,monospace)', fontSize: 9.5, fill: 'hsl(var(--faint))' }}
          tickLine={false}
          axisLine={false}
          width={44}
          tickFormatter={(v) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : v)}
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
          formatter={(v) => [INR0(v), 'P&L']}
        />
        <Area
          type="monotone"
          dataKey={dataKey}
          stroke={color}
          strokeWidth={1.6}
          fill={`url(#${gid})`}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
