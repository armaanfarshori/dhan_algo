import { useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, CartesianGrid } from 'recharts'
import { T, INR, INR0 } from '../../tokens'

const STRATEGIES = [
  { value: 'sma_crossover', label: 'SMA Crossover 9/21' },
  { value: 'scalper',       label: 'RSI Scalper (simulated)' },
]
const SEGMENTS = [
  { value: 'NSE_EQ',  label: 'NSE Equity' },
  { value: 'NSE_FNO', label: 'NSE F&O' },
]
const INTERVALS = [
  { value: '1',  label: '1 min' },
  { value: '5',  label: '5 min' },
  { value: '15', label: '15 min' },
  { value: '60', label: '1 hour' },
  { value: 'D',  label: 'Daily'  },
]

function MonoInput({ value, onChange, placeholder, style }) {
  return (
    <input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
      style={{
        background: T.bg2, border: `1px solid ${T.line2}`, color: T.ink0,
        fontFamily: T.mono, fontSize: 11, padding: '8px 12px',
        outline: 'none', width: '100%', letterSpacing: '0.08em', ...style,
      }}
    />
  )
}

function MonoSelect({ value, onChange, options }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} style={{
      background: T.bg2, border: `1px solid ${T.line2}`, color: T.ink0,
      fontFamily: T.mono, fontSize: 11, padding: '8px 12px',
      outline: 'none', width: '100%', cursor: 'pointer',
    }}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

function MetricCard({ label, value, color, sub }) {
  return (
    <div style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '14px 16px' }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 8 }}>{label}</div>
      <div style={{ fontFamily: T.dot, fontSize: 42, lineHeight: 0.95, color: color || T.ink0 }}>{value}</div>
      {sub && <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2, marginTop: 6, letterSpacing: '0.12em', textTransform: 'uppercase' }}>{sub}</div>}
    </div>
  )
}

export default function BacktestTab() {
  const [form, setForm]     = useState({
    strategy: 'sma_crossover', segment: 'NSE_EQ',
    security_id: '2885', symbol: 'RELIANCE',
    from_date: '2026-01-01', to_date: '2026-05-01',
    quantity: 1, fast_period: 9, slow_period: 21,
    interval: 'D',
  })
  const [running, setRunning]   = useState(false)
  const [result, setResult]     = useState(null)
  const [error, setError]       = useState(null)
  const [searchQ, setSearchQ]   = useState('')
  const [searchRes, setSearchRes] = useState([])

  function set(k, v) { setForm(f => ({ ...f, [k]: v })) }

  async function searchStock(q) {
    setSearchQ(q)
    if (q.length < 2) { setSearchRes([]); return }
    try {
      const r = await fetch(`/api/instruments/search?q=${encodeURIComponent(q)}&segment=${form.segment}`)
      const d = await r.json()
      if (d.ok) setSearchRes(d.results.slice(0, 6))
    } catch {}
  }

  async function runBacktest() {
    setRunning(true); setResult(null); setError(null)
    try {
      const r = await fetch('/api/backtest/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      })
      const d = await r.json()
      if (d.ok) setResult(d)
      else setError(d.error || 'Backtest failed')
    } catch (e) { setError(String(e)) }
    finally { setRunning(false) }
  }

  const label = { fontFamily: T.mono, fontSize: 9, color: T.ink2, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 6 }

  return (
    <div style={{ padding: '20px 0' }}>
      {/* Config */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}`, marginBottom: 14 }}>
        <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink0, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
          BACKTEST CONFIGURATION
        </div>
        <div style={{ padding: 16, display: 'grid', gridTemplateColumns: '1fr 1fr 2fr 1fr 1fr', gap: 14 }}>
          <div>
            <div style={label}>STRATEGY</div>
            <MonoSelect value={form.strategy} onChange={v => set('strategy', v)} options={STRATEGIES} />
          </div>
          <div>
            <div style={label}>SEGMENT</div>
            <MonoSelect value={form.segment} onChange={v => { set('segment', v); setSearchRes([]) }} options={SEGMENTS} />
          </div>
          <div style={{ position: 'relative' }}>
            <div style={label}>SYMBOL · SECURITY ID: <span style={{ color: T.cyan }}>{form.security_id}</span></div>
            <MonoInput value={searchQ || form.symbol} onChange={searchStock} placeholder="SEARCH SYMBOL…" />
            {searchRes.length > 0 && (
              <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 10, background: T.bg2, border: `1px solid ${T.line2}`, maxHeight: 160, overflowY: 'auto' }}>
                {searchRes.map((s, i) => (
                  <div key={i} onMouseDown={() => { set('security_id', s.security_id); set('symbol', s.symbol); setSearchQ(''); setSearchRes([]) }}
                    style={{ padding: '8px 12px', cursor: 'pointer', fontFamily: T.mono, fontSize: 11, color: T.ink0, borderBottom: `1px solid ${T.line}` }}
                    onMouseEnter={e => e.currentTarget.style.background = T.bg3}
                    onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                    <span style={{ color: T.cyan }}>{s.symbol}</span>
                    <span style={{ color: T.ink2, marginLeft: 10, fontSize: 9 }}>{s.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>
            <div style={label}>FROM DATE</div>
            <MonoInput value={form.from_date} onChange={v => set('from_date', v)} placeholder="YYYY-MM-DD" />
          </div>
          <div>
            <div style={label}>TO DATE</div>
            <MonoInput value={form.to_date} onChange={v => set('to_date', v)} placeholder="YYYY-MM-DD" />
          </div>
        </div>

        {form.strategy === 'sma_crossover' && (
          <div style={{ padding: '0 16px 16px', display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr 1fr', gap: 14 }}>
            <div>
              <div style={label}>FAST PERIOD</div>
              <MonoInput value={form.fast_period} onChange={v => set('fast_period', Number(v))} />
            </div>
            <div>
              <div style={label}>SLOW PERIOD</div>
              <MonoInput value={form.slow_period} onChange={v => set('slow_period', Number(v))} />
            </div>
            <div>
              <div style={label}>QUANTITY</div>
              <MonoInput value={form.quantity} onChange={v => set('quantity', Number(v))} />
            </div>
            <div>
              <div style={label}>INTERVAL</div>
              <MonoSelect value={form.interval} onChange={v => set('interval', v)} options={INTERVALS} />
            </div>
            <div style={{ display: 'flex', alignItems: 'flex-end' }}>
              <button onClick={runBacktest} disabled={running} style={{
                width: '100%', padding: '8px 0', background: 'transparent',
                border: `1px solid ${T.green}`, color: T.green,
                fontFamily: T.mono, fontSize: 11, fontWeight: 600,
                letterSpacing: '0.16em', textTransform: 'uppercase', cursor: 'pointer',
                opacity: running ? 0.6 : 1,
              }}>
                {running ? 'RUNNING…' : 'RUN ⟶'}
              </button>
            </div>
          </div>
        )}

        {form.strategy !== 'sma_crossover' && (
          <div style={{ padding: '0 16px 16px', display: 'flex', justifyContent: 'flex-end' }}>
            <button onClick={runBacktest} disabled={running} style={{
              padding: '8px 24px', background: 'transparent',
              border: `1px solid ${T.green}`, color: T.green,
              fontFamily: T.mono, fontSize: 11, fontWeight: 600,
              letterSpacing: '0.16em', textTransform: 'uppercase', cursor: 'pointer',
              opacity: running ? 0.6 : 1,
            }}>
              {running ? 'RUNNING…' : 'RUN BACKTEST ⟶'}
            </button>
          </div>
        )}
      </div>

      {error && (
        <div style={{ background: T.bg1, border: `1px solid ${T.red}`, padding: '14px 16px', marginBottom: 14, fontFamily: T.mono, fontSize: 11, color: T.red, letterSpacing: '0.1em' }}>
          ⚠ ERROR: {error}
        </div>
      )}

      {running && (
        <div style={{ background: T.bg1, border: `1px solid ${T.line}`, padding: '40px', textAlign: 'center', fontFamily: T.mono, fontSize: 11, color: T.cyan, letterSpacing: '0.2em', textTransform: 'uppercase', marginBottom: 14 }}>
          <span style={{ animation: 'pulse 1s infinite' }}>● FETCHING HISTORICAL DATA + RUNNING SIMULATION…</span>
        </div>
      )}

      {result && (
        <>
          {/* Metrics */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 14, marginBottom: 14 }}>
            <MetricCard label="Total Trades"  value={result.summary.total_trades} />
            <MetricCard label="Win Rate"      value={`${result.summary.win_rate_pct}%`} color={result.summary.win_rate_pct >= 50 ? T.green : T.red} />
            <MetricCard label="Total P&L"     value={INR0(result.summary.total_pnl_inr)} color={result.summary.total_pnl_inr >= 0 ? T.green : T.red} sub={`${result.bars} bars`} />
            <MetricCard label="Max Drawdown"  value={`${result.summary.max_drawdown_pct}%`} color={T.red} />
            <MetricCard label="Sharpe Ratio"  value={result.summary.sharpe_ratio} color={result.summary.sharpe_ratio >= 1 ? T.green : T.amber} />
            <MetricCard label="Final Capital" value={INR0(result.summary.final_capital)} color={T.cyan} />
          </div>

          {/* Equity curve */}
          <div style={{ background: T.bg1, border: `1px solid ${T.line}`, marginBottom: 14 }}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink0, textTransform: 'uppercase', letterSpacing: '0.16em', display: 'flex', gap: 18 }}>
              EQUITY CURVE · {form.symbol} · {form.strategy}
              <span style={{ marginLeft: 'auto', color: T.ink2 }}>
                {form.from_date} → {form.to_date}
              </span>
            </div>
            <div style={{ padding: '12px 16px 16px' }}>
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={result.equity_curve.map((v, i) => ({ i, equity: Math.round(v) }))} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke={T.line} strokeDasharray="2 4" vertical={false} />
                  <XAxis dataKey="i" hide />
                  <YAxis tickFormatter={v => `₹${(v/1000).toFixed(0)}k`} width={60}
                    tick={{ fill: T.ink3, fontSize: 9, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                  <Tooltip formatter={v => [INR(v), 'Equity']} contentStyle={{ background: T.bg3, border: `1px solid ${T.line2}`, fontFamily: 'JetBrains Mono', fontSize: 11 }} />
                  <ReferenceLine y={100000} stroke={T.line2} strokeDasharray="4 2" label={{ value: 'START', fill: T.ink3, fontSize: 8 }} />
                  <Line type="monotone" dataKey="equity"
                    stroke={result.summary.total_pnl_inr >= 0 ? T.green : T.red}
                    dot={false} strokeWidth={1.5} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Trades table */}
          {result.trades?.length > 0 && (
            <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
              <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.line}`, fontFamily: T.mono, fontSize: 10, color: T.ink0, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
                TRADE LOG · LAST {Math.min(result.trades.length, 20)} TRADES
              </div>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>{['ENTRY DATE', 'EXIT DATE', 'DIRECTION', 'ENTRY', 'EXIT', 'P&L', 'REASON'].map(h => (
                      <th key={h} style={{ textAlign: 'left', fontFamily: T.mono, fontSize: 9, color: T.ink2, padding: '8px 12px', borderBottom: `1px solid ${T.line}`, letterSpacing: '0.14em', textTransform: 'uppercase', background: T.bg1 }}>{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {result.trades.slice(-20).reverse().map((t, i) => (
                      <tr key={i} style={{ borderBottom: `1px solid ${T.line}` }}>
                        <td style={{ fontFamily: T.mono, fontSize: 10, padding: '8px 12px', color: T.ink2 }}>{String(t.entry_date).slice(0, 10)}</td>
                        <td style={{ fontFamily: T.mono, fontSize: 10, padding: '8px 12px', color: T.ink2 }}>{String(t.exit_date).slice(0, 10)}</td>
                        <td style={{ fontFamily: T.mono, fontSize: 10, padding: '8px 12px', color: t.direction === 'LONG' ? T.green : T.red }}>{t.direction}</td>
                        <td style={{ fontFamily: T.dot, fontSize: 16, padding: '8px 12px', color: T.ink0 }}>{INR(t.entry_price || 0)}</td>
                        <td style={{ fontFamily: T.dot, fontSize: 16, padding: '8px 12px', color: T.ink0 }}>{INR(t.exit_price || 0)}</td>
                        <td style={{ fontFamily: T.dot, fontSize: 16, padding: '8px 12px', color: (t.pnl || 0) >= 0 ? T.green : T.red }}>{(t.pnl || 0) >= 0 ? '+' : ''}{INR(t.pnl || 0)}</td>
                        <td style={{ fontFamily: T.mono, fontSize: 9, padding: '8px 12px', color: T.ink2, maxWidth: 200 }}>{t.exit_reason || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
