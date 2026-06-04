import { T, fmtTime } from '../../tokens'

function Stat({ label, value, sub, color }) {
  return (
    <div style={{ background: T.bg2, border: `1px solid ${T.line}`, padding: 16 }}>
      <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.18em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontFamily: T.dot, fontSize: 26, color: color ?? T.ink0 }}>{value}</div>
      {sub && <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

function PanelHeader({ children, right }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      padding: '10px 16px', borderBottom: `1px solid ${T.line}`,
      fontFamily: T.mono, fontSize: 9, color: T.ink3,
      textTransform: 'uppercase', letterSpacing: '0.18em',
    }}>
      <span>{children}</span>
      {right && <span style={{ color: T.ink3, textTransform: 'none', letterSpacing: 0, fontSize: 9 }}>{right}</span>}
    </div>
  )
}

// Segment colour map
const SEG_COLOR = {
  NSE_EQ:   T.cyan,
  NSE_FNO:  T.amber,
  NSE_CDS:  '#7dd3fc',   // sky blue — currency
  BSE_EQ:   '#a78bfa',   // violet
  BSE_FNO:  '#c084fc',
  MCX_COMM: '#fb923c',   // orange — commodities
  NSE_IDX:  T.ink2,
  BSE_IDX:  T.ink3,
}

function SegmentTable({ segments, instruments }) {
  if (!segments?.length) return (
    <div style={{ padding: 16, fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>
      No segment data yet — bars loading...
    </div>
  )

  // Total bars across all segments for progress bar
  const totalBars = segments.reduce((s, r) => s + r.bars, 0)

  return (
    <div style={{ padding: '12px 16px' }}>
      {segments.map((seg, i) => {
        const color = SEG_COLOR[seg.segment] ?? T.ink2
        const instrCount = instruments?.[seg.segment] ?? 0
        const pct = instrCount > 0 ? Math.min(seg.securities / instrCount * 100, 100) : 0
        return (
          <div key={seg.segment} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '8px 0',
            borderBottom: i < segments.length - 1 ? `1px solid ${T.line}` : 'none',
          }}>
            <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
            <span style={{ fontFamily: T.mono, fontSize: 10, color, minWidth: 90, fontWeight: 600 }}>{seg.segment}</span>
            <div style={{ flex: 1 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink2 }}>
                  {seg.securities.toLocaleString('en-IN')} / {instrCount.toLocaleString('en-IN')} securities
                </span>
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>
                  {(seg.bars / 1e6).toFixed(2)}M bars
                </span>
              </div>
              <div style={{ height: 3, background: T.bg3, overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${pct}%`, background: color, transition: 'width 1s' }} />
              </div>
              <div style={{ fontFamily: T.mono, fontSize: 8, color: T.ink3, marginTop: 2 }}>
                {pct.toFixed(1)}% loaded · {seg.earliest} → {seg.latest}
              </div>
            </div>
          </div>
        )
      })}
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
  const totalInsts = Object.values(data.instruments ?? {}).reduce((a, b) => a + b, 0)
  const totalSegs  = (data.segments ?? []).length

  return (
    <>
      <div style={{ padding: 16, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
        <Stat
          label="1m Bars"
          value={bars1m ? (bars1m.rows / 1e6).toFixed(1) + 'M' : '—'}
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
          label="Segments loaded"
          value={totalSegs || '—'}
          sub={`${totalInsts.toLocaleString('en-IN')} instruments`}
          color={T.amber}
        />
        <Stat
          label="Signals / Trades"
          value={data.signals ?? '—'}
          sub={`${data.trades ?? 0} trades recorded`}
          color={T.green}
        />
      </div>
      {(data.segments?.length > 0) && (
        <div style={{ borderTop: `1px solid ${T.line}` }}>
          <div style={{ padding: '8px 16px', fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
            Per-segment coverage
          </div>
          <SegmentTable segments={data.segments} instruments={data.instruments} />
        </div>
      )}
    </>
  )
}

// Queue status — shows which segment is next
const QUEUE_ORDER = ['NSE_EQ', 'NSE_CDS', 'BSE_EQ', 'MCX_COMM']
const QUEUE_LABELS = {
  NSE_EQ:   'NSE Equity (22,646 securities)',
  NSE_CDS:  'NSE Currency derivatives (8,990)',
  BSE_EQ:   'BSE Equity (13,185)',
  MCX_COMM: 'MCX Commodities (15,030)',
}

function BackfillStatus({ backfill, dbStats }) {
  const d       = backfill?.data
  const running = d?.running ?? false
  const logs    = d?.log_tail ?? []
  const allLogs = [...logs]

  // Find current security from any log line
  const findSec = (lines) => {
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/security_id=(\d+)/) || lines[i].match(/\[1m\]\s+(\d+)\s+\d{4}/)
      if (m) return m[1]
    }
    return null
  }
  const findChunk = (lines) => {
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/(\d{4}-\d{2}-\d{2})\s+→\s+(\d{4}-\d{2}-\d{2})/)
      if (m) return `${m[1]} → ${m[2]}`
    }
    return null
  }

  const curSec   = findSec(allLogs)
  const curChunk = findChunk(allLogs)

  // Determine which segment is currently loading from DB stats
  const segs       = dbStats?.data?.segments ?? []
  const instruments= dbStats?.data?.instruments ?? {}
  const activeSegs = QUEUE_ORDER.filter(seg => {
    const loaded = segs.find(s => s.segment === seg)?.securities ?? 0
    const total  = instruments[seg] ?? 0
    return loaded < total || loaded === 0
  })
  const currentSeg = activeSegs[0] ?? null
  const doneSeg    = QUEUE_ORDER.filter(s => !activeSegs.includes(s))

  const stripPrefix = l => l.replace(/^\d{2}:\d{2}:\d{2}\s+\w+\s+dhan\.\w+\s+[—–]\s+/, '')

  return (
    <div>
      {/* Queue status */}
      <div style={{ padding: '10px 16px', borderBottom: `1px solid ${T.line}` }}>
        <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 8 }}>
          Backfill Queue — sequential
        </div>
        {QUEUE_ORDER.map((seg, idx) => {
          const done    = doneSeg.includes(seg)
          const current = currentSeg === seg && running
          const pending = !done && !current
          const color   = done ? T.green : current ? T.amber : T.ink3
          const icon    = done ? '✓' : current ? '●' : '○'
          return (
            <div key={seg} style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '4px 0',
              opacity: pending ? 0.5 : 1,
            }}>
              <span style={{ fontFamily: T.mono, fontSize: 10, color, minWidth: 14 }}>{icon}</span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color, minWidth: 80 }}>
                {seg.replace('_COMM','').replace('_EQ','').replace('_CDS','')}
              </span>
              <span style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3 }}>{QUEUE_LABELS[seg]}</span>
              {current && curChunk && (
                <span style={{ fontFamily: T.mono, fontSize: 9, color: T.amber, marginLeft: 'auto' }}>
                  {curChunk}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {/* Status dot + recent log */}
      <div style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: 10, borderBottom: `1px solid ${T.line}` }}>
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: running ? T.green : T.ink3,
          boxShadow: running ? `0 0 6px ${T.green}` : 'none',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 9, letterSpacing: '0.18em', color: running ? T.green : T.ink2 }}>
          {running ? 'RUNNING' : 'IDLE'}
        </span>
        {curSec && <span style={{ fontFamily: T.dot, fontSize: 18, color: T.ink1 }}>#{curSec}</span>}
      </div>
      <div style={{ padding: '10px 16px', maxHeight: 130, overflowY: 'auto' }}>
        {allLogs.length === 0
          ? <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink3 }}>No activity</span>
          : allLogs.map((l, i) => (
            <div key={i} style={{
              fontFamily: T.mono, fontSize: 9, lineHeight: 1.8,
              color: l.includes('ERROR') ? T.red : l.includes('done') || l.includes('upserted') ? T.green : T.ink2,
              borderLeft: `2px solid ${l.includes('ERROR') ? T.red : l.includes('done') || l.includes('upserted') ? T.green : T.line}`,
              paddingLeft: 8,
            }}>
              {stripPrefix(l)}
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
    <div style={{ padding: '10px 16px', display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{
          width: 8, height: 8, borderRadius: '50%',
          background: running ? T.green : T.red,
          boxShadow: running ? `0 0 6px ${T.green}` : 'none',
        }} />
        <span style={{ fontFamily: T.mono, fontSize: 9, letterSpacing: '0.18em', color: running ? T.green : T.red }}>
          {running ? 'GATEWAY ONLINE' : 'GATEWAY OFFLINE'}
        </span>
      </div>
      {data?.model && (
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.ink1 }}>{data.model}</span>
      )}
      {data?.provider && (
        <span style={{ fontFamily: T.mono, fontSize: 10, color: T.cyan }}>via {data.provider}</span>
      )}
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 14 }}>
        {[
          ['Pre-market', '08:45 IST', 'daily'],
          ['EOD review', '15:45 IST', 'daily'],
          ['Health rpt', '09:00 IST', 'weekly'],
        ].map(([name, time, freq]) => (
          <div key={name} style={{ textAlign: 'right' }}>
            <div style={{ fontFamily: T.mono, fontSize: 9, color: T.ink3, letterSpacing: '0.18em', textTransform: 'uppercase' }}>{name}</div>
            <div style={{ fontFamily: T.dot, fontSize: 18, color: T.amber }}>{time} · {freq}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function DataTab({ dbStats, backfill, hermes }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* TimescaleDB — now with per-segment breakdown */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader right="10.0.1.155:5432 / dhan_trading">TIMESCALEDB</PanelHeader>
        <DBStats dbStats={dbStats} />
      </div>

      {/* Backfill — sequential queue view */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader right="NSE_EQ → NSE_CDS → BSE_EQ → MCX_COMM">BACKFILL QUEUE</PanelHeader>
        <BackfillStatus backfill={backfill} dbStats={dbStats} />
      </div>

      {/* Hermes */}
      <div style={{ background: T.bg1, border: `1px solid ${T.line}` }}>
        <PanelHeader>HERMES GATEWAY · @farshoribot</PanelHeader>
        <HermesStatus hermes={hermes} />
      </div>
    </div>
  )
}
