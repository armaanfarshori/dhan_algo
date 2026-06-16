import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { INR0 } from '@/tokens'

/** Shared P&L area chart — used by Signals "Intraday P&L" and Portfolio
 *  "Equity Curve" so they share one design. data: [{ t, v }]. */
export function PnlAreaChart({ data, height = 210, xKey = 't', dataKey = 'v' }) {
  const pts  = data ?? []
  const last = pts.length ? (pts[pts.length - 1][dataKey] ?? 0) : 0
  const up   = last >= 0
  const color = up ? 'hsl(var(--profit))' : 'hsl(var(--loss))'
  const gid   = `pnlgrad-${dataKey}`

  if (pts.length < 2) {
    return (
      <div className="flex items-center justify-center text-[11px] text-muted-foreground mono" style={{ height }}>
        No data yet
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
          labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
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
