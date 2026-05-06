import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { INR } from '../utils'

const s = {
  card:  { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8, padding: 16 },
  label: { fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)', marginBottom: 12 },
  empty: { color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: '32px 0' },
}

function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const v = payload[0].value
  return (
    <div style={{ background: 'var(--surface)', border: '1px solid var(--border)', padding: '6px 10px', borderRadius: 6 }}>
      <span style={{ color: v >= 0 ? 'var(--green)' : 'var(--red)', fontWeight: 'bold' }}>{INR(v)}</span>
    </div>
  )
}

export default function EquityCurve({ equity }) {
  const raw = equity?.data ?? []
  if (raw.length < 2) {
    return (
      <div style={s.card}>
        <div style={s.label}>Equity Curve</div>
        <div style={s.empty}>No trade history yet</div>
      </div>
    )
  }

  const baseline = raw[0]
  const chartData = raw.map((v, i) => ({ i, pnl: +(v - baseline).toFixed(2) }))
  const isUp = chartData[chartData.length - 1].pnl >= 0

  return (
    <div style={s.card}>
      <div style={s.label}>Equity Curve</div>
      <ResponsiveContainer width="100%" height={140}>
        <LineChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
          <XAxis dataKey="i" hide />
          <YAxis
            tickFormatter={v => `₹${v}`}
            width={60}
            tick={{ fill: 'var(--muted)', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip content={<CustomTooltip />} />
          <ReferenceLine y={0} stroke="var(--border)" strokeDasharray="3 3" />
          <Line
            type="monotone"
            dataKey="pnl"
            stroke={isUp ? 'var(--green)' : 'var(--red)'}
            dot={false}
            strokeWidth={1.5}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
