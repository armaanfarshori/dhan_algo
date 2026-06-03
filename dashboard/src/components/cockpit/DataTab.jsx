import { T, fmtTime } from '../../tokens'

function Stat({ label, value, sub, color }) {
  return (
    <div style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: '10px 14px' }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.16em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontFamily: T.dot, fontSize: 26, color: color ?? T.ink0 }}>{value}</div>
      {sub && <div style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3, marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function PanelHeader({ children, right }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      padding: '10px 14px', borderBottom: `1px solid ${T.line}`,
      fontFamily: T.mono, fontSize: 10, color: T.ink2,
      textTransform: 'uppercase', letterSpacing: '0.16em',
    }}>
      <span>{children}</span>
      {right && <span style={{ color: T.ink3, textTransform: 'none', letterSpacing: 0 }}>{right}</span>}
    </div>
  )
}

function DBStats({ dbStats }) {
  const data = dbStats?.data
  if (!data) return (
    <div style={{ padding: 16, color: T.ink3, fontFamily: T.mono, fontSize: 11 }}>
      DB not connected — start main.py to enable live stats
    </div>
  )
  const bars1m = data.bars?.find(b => b.timeframe === '1m')
  const bars1d = data.bars?.find(b => b.timeframe === '1d')
  const nseEq  = data.instruments?.NSE_EQ ?? 0
  const total  = Object.values(data.instruments ?? {}).reduce((a, b) => a + b, 0)
  return (
    <div style={{ padding: 14, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10 }}>
      <Stat
        label="1m Bars"
        value={bars1m ? (bars1m.rows / 1000).toFixed(0) + 'K' : '—'}
        sub={bars1m ? `${bars1m.earliest} → ${bars1m.latest}` : 'No data'}
        color={T.cyan}
      />
      <Stat
        label="1d Bars"
        value={bars1d ? (bars1d.rows / 1000).toFixed(1) + 'K' : '—'}
        sub={bars1d ? `${bars1d.earliest} → ${bars1d.latest}` : 'No data'}
        color={T.cyan}
      />
      <Stat
        label="NSE Equities"
        value={nseEq ? nseEq.toLocaleString('en-IN') : '—'}
        sub={`${total.toLocaleString('en-IN')} total instruments`}
        color={T.amber}
      />
      <Stat
        label="DB Signals"
        value={data.signals ?? '—'}
        sub={`${data.trades ?? 0} trades recorded`}
        color={T.green}
      />
    </div>
  )
}

function BackfillStatus({ backfill }) {
  const data    = backfill?.data
  const running = data?.running ?? false
  const logs    = data?.log_tail ?? []

  const lastLog    = logs[logs.length - 1] ?? ''
  const secMatch   = lastLog.match(/security_id=(\d+)/)
  const currentSec = secMatch?.[1] ?? '—'

  return (
    <div>
      <div style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 10, borderBottom: `1px solid ${T.line}` }}>
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: running ? T.green : T.ink3,
          boxShadow: running ? `0 0 6px ${T.green}` : 'none',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 11, color: running ? T.green : T.ink2 }}>
          {running ? 'RUNNING' : 'IDLE'}
        </span>
        {running && (
          <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
            · processing security {currentSec}
          </span>
        )}
      </div>
      <div style={{ padding: '10px 14px', maxHeight: 140, overflowY: 'auto' }}>
        {logs.length === 0
          ? <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No log activity</span>
          : logs.map((l, i) => (
            <div key={i} style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2, lineHeight: 1.7,
              color: l.includes('ERROR') ? T.red : l.includes('done') ? T.green : T.ink2 }}>
              {l.replace(/^\d{2}:\d{2}:\d{2}\s+INFO\s+dhan\.\w+\s+—\s+/, '')}
            </div>
          ))
        }
      </div>
    </div>
  )
}

function HermesStatus({ hermes }) {
  const data    = hermes?.data
  const running = data?.running ?? false
  return (
    <div style={{ padding: '10px 14px', display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: running ? T.green : T.red,
          boxShadow: running ? `0 0 6px ${T.green}` : 'none',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 11, color: running ? T.green : T.red }}>
          {running ? 'GATEWAY ONLINE' : 'GATEWAY OFFLINE'}
        </span>
      </div>
      {data?.model && (
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink2 }}>
          {data.model}
        </span>
      )}
      {data?.provider && (
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.cyan }}>
          via {data.provider}
        </span>
      )}
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 14 }}>
        {[
          ['Pre-market', '08:45 IST', 'daily'],
          ['EOD review', '15:45 IST', 'daily'],
          ['Health rpt', '09:00 IST', 'weekly'],
        ].map(([name, time, freq]) => (
          <div key={name} style={{ textAlign: 'right' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{name}</div>
            <div style={{ fontFamily: T.mono, fontSize: 10, color: T.amber }}>{time} · {freq}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function DataTab({ dbStats, backfill, hermes }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 16 }}>

      {/* TimescaleDB */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader right="10.0.1.155:5432 / dhan_trading">TIMESCALEDB</PanelHeader>
        <DBStats dbStats={dbStats} />
      </div>

      {/* Backfill */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader right="22,646 NSE equities · ~5 days total">BACKFILL PROGRESS</PanelHeader>
        <BackfillStatus backfill={backfill} />
      </div>

      {/* Hermes */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader>HERMES GATEWAY · @farshoribot</PanelHeader>
        <HermesStatus hermes={hermes} />
      </div>

    </div>
  )
}
