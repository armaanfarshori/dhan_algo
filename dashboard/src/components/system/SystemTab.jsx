import { Panel, PanelHeader, StatCard, Badge, Pill } from '@/components/ui'
import { fmtUptime, fmtTime } from '@/tokens'

// ─── Constants ────────────────────────────────────────────────────────────────

const LEVEL_COLOR_CLASS = {
  INFO:     'text-sky',
  WARNING:  'text-amber',
  WARN:     'text-amber',
  ERROR:    'text-loss',
  CRITICAL: 'text-loss',
  DEBUG:    'text-faint',
}

const RATE_LIMIT_LABELS = {
  orders:      'orders',
  data:        'data',
  quote:       'quote',
  non_trading: 'non-trading',
}

const RATE_LIMIT_KEYS = ['orders', 'data', 'quote', 'non_trading']

const INFRA_ROWS = [
  { k: 'agent',         v: 't4g.small · 2 vCPU · 2 GB' },
  { k: 'db',            v: 't4g.medium · 2 vCPU · 4 GB' },
  { k: 'EBS snapshots', v: 'DLM daily' },
  { k: 'TF state',      v: 'S3 + DynamoDB lock' },
  { k: 'schema head',   v: '007' },
  { k: 'region',        v: 'ap-south-1' },
]

const MIGRATION_ROWS = [
  { id: '007', desc: 'api_usage table',               head: true  },
  { id: '006', desc: 'features_snapshot jsonb + GIN', head: false },
  { id: '005', desc: 'drop ohlcv_1min mirror',        head: false },
  { id: '004', desc: 'engine_positions + signals',    head: false },
  { id: '003', desc: 'journals + portfolio ledger',   head: false },
  { id: '002', desc: 'instruments + watchlist',       head: false },
  { id: '001', desc: 'bars hypertable + init schema', head: false },
]

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtByProcess(byProcess) {
  if (!byProcess || typeof byProcess !== 'object') return null
  const entries = Object.entries(byProcess).filter(([, v]) => (v ?? 0) > 0)
  if (entries.length === 0) return null
  return entries.map(([k, v]) => `${k} ${Number(v).toLocaleString('en-IN')}`).join(' · ')
}

function rlBarColor(pct) {
  if (pct >= 80) return 'hsl(var(--loss))'
  if (pct >= 50) return 'hsl(var(--amber))'
  return 'hsl(var(--sky))'
}

// ─── KPI Row ─────────────────────────────────────────────────────────────────

function KpiRow({ data }) {
  const t   = data.trader
  const db  = data.dbStats?.data
  const bf  = data.backfill?.data
  const ck  = bf?.checkpoint ?? {}
  const pct = ck.pct ?? 0

  // API calls today: sum total across all capped + uncapped categories
  const cats = data.rateLimitsData?.data?.categories ?? {}
  const totalCalls = Object.values(cats).reduce((sum, c) => sum + (c?.total ?? 0), 0)
  const ordersCalls = cats.orders?.total ?? 0
  const dataCalls   = cats.data?.total   ?? 0

  // DB size label
  const dbSize = db?.db_size ?? '—'
  const hypertableCount = (db?.hypertables ?? []).length
  // approximate total rows in bars
  const barsHyper = (db?.hypertables ?? []).find(h => h.name === 'bars')
  const barsRows  = barsHyper
    ? barsHyper.approx_rows >= 1e6
      ? `~${(barsHyper.approx_rows / 1e6).toFixed(0)}M rows`
      : `~${barsHyper.approx_rows.toLocaleString('en-IN')} rows`
    : db?.up ? '5 hypertables' : '—'

  // Uptime
  const uptime  = data.alive ? fmtUptime(t?.uptime_seconds ?? 0) : 'DOWN'
  const hbAge   = t?.heartbeat_age_s != null ? `heartbeat ${t.heartbeat_age_s}s ago` : 'heartbeat stale'
  const uptimeSub = data.alive ? `trader · ${hbAge}` : 'heartbeat stale'

  return (
    <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4 mb-3.5">
      <StatCard
        label="Backfill"
        value={pct > 0 ? `${Number(pct).toFixed(1)}%` : '—'}
        valueClassName="text-foreground"
        right={<Pill tone="neu">NSE_EQ</Pill>}
        sub={
          ck.index != null
            ? `${ck.index}/${ck.total} · ~${Number(100 - pct).toFixed(1)}% left`
            : 'no checkpoint'
        }
        bar={{ value: pct, color: 'hsl(var(--sky))' }}
      />
      <StatCard
        label="Uptime"
        value={uptime}
        valueClassName={data.alive ? 'text-foreground' : 'text-loss'}
        right={<Pill tone={data.alive ? 'up' : 'down'}>{data.alive ? 'healthy' : 'down'}</Pill>}
        sub={uptimeSub}
      />
      <StatCard
        label="DB Size"
        value={dbSize}
        valueClassName="text-foreground"
        right={<Pill tone="neu">TimescaleDB</Pill>}
        sub={
          db?.up
            ? `${barsRows} · ${hypertableCount} hypertables`
            : db ? 'unreachable' : 'waiting…'
        }
      />
      <StatCard
        label="API Calls today"
        value={totalCalls > 0 ? totalCalls.toLocaleString('en-IN') : '—'}
        valueClassName="text-foreground"
        right={<Pill tone="neu">of 100k</Pill>}
        sub={
          totalCalls > 0
            ? `orders ${ordersCalls.toLocaleString('en-IN')} · data ${dataCalls.toLocaleString('en-IN')}`
            : data.rateLimitsData?.loading ? 'loading…' : 'no data'
        }
      />
    </div>
  )
}

// ─── Services Panel ───────────────────────────────────────────────────────────

function ServiceRow({ dot, name, sub, state, uptime }) {
  // dot: 'active' | 'running' | 'idle'
  const dotStyle = {
    width: 8,
    height: 8,
    borderRadius: '50%',
    flexShrink: 0,
    background:
      dot === 'active'  ? 'hsl(var(--profit))' :
      dot === 'running' ? 'hsl(var(--sky))'    :
                          'hsl(var(--border2))',
    boxShadow:
      dot === 'active'  ? '0 0 0 3px hsl(151 60% 53% / .15)' :
      dot === 'running' ? '0 0 0 3px hsl(205 85% 62% / .15)' :
                          'none',
  }

  const badgeVariant =
    state === 'active'  ? 'profit' :
    state === 'running' ? 'sky'    :
    'default'

  return (
    <div className="flex items-center justify-between border-b border-border px-4 py-[11px] last:border-b-0">
      <div className="flex items-center gap-2.5">
        <span style={dotStyle} />
        <div>
          <div className="mono text-[12px] font-medium text-foreground">{name}</div>
          {sub && <div className="text-[9.5px] text-faint mt-[1px]">{sub}</div>}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0 ml-4">
        <Badge variant={badgeVariant}>{state}</Badge>
        {uptime && (
          <span className="mono text-[10.5px] text-faint w-[64px] text-right">{uptime}</span>
        )}
      </div>
    </div>
  )
}

function ServicesPanel({ data }) {
  const t  = data.trader
  const bf = data.backfill?.data
  const ck = bf?.checkpoint ?? {}
  const bars = t?.bars ?? {}

  // Derive service rows from the same logic as the original ServicesRow
  const engineUp   = data.alive
  const feedConn   = t?.feed?.connected
  const feedSubs   = t?.feed?.subscribed ?? 0
  const bfRunning  = bf?.running

  const svcs = [
    {
      name: 'dhan-trader',
      sub:  'order flow + heartbeat',
      dot:  engineUp  ? 'active' : 'idle',
      state: engineUp ? 'active' : 'stopped',
      uptime: engineUp ? fmtUptime(t?.uptime_seconds ?? 0) : '',
    },
    {
      name:  'dhan-api',
      sub:   'dashboard :8765',
      dot:   'active',
      state: 'active',
      uptime: '',
    },
    {
      name:  'timescaledb',
      sub:   ':5432 (DB EC2)',
      dot:   data.dbStats?.data?.up ? 'active' : 'idle',
      state: data.dbStats?.data?.up ? 'active' : 'stopped',
      uptime: data.dbStats?.data?.up ? `ping ${data.dbStats.data.ping_ms}ms` : '',
    },
    {
      name:   feedConn ? 'live-feed' : 'live-feed',
      sub:    feedConn ? `${feedSubs} WS subscriptions` : 'feed offline',
      dot:    feedConn ? 'running' : 'idle',
      state:  feedConn ? 'running' : 'offline',
      uptime: bars.bars_written != null ? `${(bars.bars_written ?? 0).toLocaleString('en-IN')} bars` : '',
    },
    {
      name:  'backfill',
      sub:   'charts/intraday (screen)',
      dot:   bfRunning ? 'running' : 'idle',
      state: bfRunning ? 'running' : 'stopped',
      uptime: ck.pct != null ? `${ck.pct}%` : '',
    },
    {
      name:  'dhan-alert@',
      sub:   'OnFailure notifier',
      dot:   'idle',
      state: 'enabled',
      uptime: '',
    },
  ]

  return (
    <Panel>
      <PanelHeader title="Services" meta="systemd · agent EC2" />
      {svcs.map((s, i) => (
        <ServiceRow key={i} {...s} />
      ))}
    </Panel>
  )
}

// ─── Rate-Limit Spend Panel ───────────────────────────────────────────────────

function RateLimitPanel({ rateLimitsData }) {
  const hasError   = !!rateLimitsData?.error
  const categories = rateLimitsData?.data?.categories ?? null
  const hasData    = categories != null

  // Max usage pct for panel badge
  let maxPct = 0
  if (hasData) {
    RATE_LIMIT_KEYS.forEach(key => {
      const cat = categories[key]
      if (cat?.per_day != null && cat.per_day > 0) {
        const p = (cat.total ?? 0) / cat.per_day * 100
        if (p > maxPct) maxPct = p
      }
    })
  }

  const metaLabel = hasData
    ? `${rateLimitsData?.data?.date ?? 'today'} · resets midnight IST`
    : hasError ? 'endpoint error' : 'loading…'

  // Build the by-process summary line across all categories
  let byProcessMerged = {}
  if (hasData) {
    RATE_LIMIT_KEYS.forEach(key => {
      const bp = categories[key]?.by_process ?? {}
      Object.entries(bp).forEach(([proc, val]) => {
        byProcessMerged[proc] = (byProcessMerged[proc] ?? 0) + (val ?? 0)
      })
    })
  }
  const processSummary = fmtByProcess(byProcessMerged)

  return (
    <Panel>
      <PanelHeader
        title="Rate-limit Spend"
        meta={
          <span className="mono">{metaLabel}</span>
        }
      />

      {!hasData && (
        <div className="mono px-3 py-3 text-[10px] text-faint">
          {hasError
            ? `Error: ${rateLimitsData?.error?.message ?? 'failed to fetch /api/rate-limits'}`
            : 'loading…'}
        </div>
      )}

      {RATE_LIMIT_KEYS.map(key => {
        const cat       = categories?.[key] ?? {}
        const label     = RATE_LIMIT_LABELS[key] ?? key
        const total     = cat.total   ?? null
        const perDay    = cat.per_day ?? null
        const capped    = perDay != null
        const usagePct  = capped && total != null
          ? Math.min((total / perDay) * 100, 100)
          : null
        const barColor  = usagePct != null ? rlBarColor(usagePct) : 'hsl(var(--faint))'
        const capTxt    = hasData ? (capped ? `/ ${perDay >= 1000 ? (perDay / 1000) + 'k' : perDay}` : 'no cap') : '—'

        return (
          <div key={key} className="flex items-center gap-2.5 border-b border-border px-3 py-2 last:border-b-0">
            {/* Label */}
            <span className="mono w-[84px] shrink-0 text-[11px] text-muted-foreground">{label}</span>
            {/* Bar */}
            <div className="flex-1">
              <div className="h-1.5 overflow-hidden rounded-full bg-border">
                <div
                  className="h-full rounded-full transition-[width] duration-500"
                  style={{
                    width: usagePct != null ? `${Math.max(usagePct, 3)}%` : '0%',
                    background: barColor,
                  }}
                />
              </div>
            </div>
            {/* Used count */}
            <span
              className="mono w-14 shrink-0 text-right text-[11px]"
              style={{ color: barColor }}
            >
              {total != null ? total.toLocaleString('en-IN') : '—'}
            </span>
            {/* Cap */}
            <span className="mono w-16 shrink-0 text-right text-[10px] text-faint">{capTxt}</span>
          </div>
        )
      })}

      {/* By-process breakdown line */}
      <div className="border-t border-border px-3 py-2.5 mono text-[10px] text-faint">
        {processSummary
          ? `by process — ${processSummary}`
          : 'account-wide across trader · api · backfill (resets IST midnight)'}
      </div>
    </Panel>
  )
}

// ─── Recent Logs Panel ────────────────────────────────────────────────────────

function LogsPanel({ logs }) {
  const all  = logs?.data?.logs ?? []
  const rows = all.slice(-50)

  return (
    <Panel>
      <PanelHeader
        title="Recent Logs"
        meta={<span className="mono">trader.log · IST</span>}
      />
      <div className="mono max-h-[340px] overflow-auto px-4 py-3 text-[10.5px] leading-[1.7]">
        {rows.length === 0 ? (
          <span className="text-faint">No log output</span>
        ) : (
          rows.map((l, i) => {
            const lvl = l.level ?? ''
            const colorClass = LEVEL_COLOR_CLASS[lvl] ?? 'text-muted-foreground'
            const bgStyle =
              lvl === 'ERROR' || lvl === 'CRITICAL'
                ? { background: 'hsl(var(--loss) / .05)' }
                : lvl === 'WARNING' || lvl === 'WARN'
                ? { background: 'hsl(var(--amber) / .05)' }
                : {}
            const fullText = `${fmtTime(l.ts)} ${lvl} ${l.name} ${l.msg}`
            return (
              <div
                key={i}
                className="flex gap-1.5 min-w-0 overflow-hidden px-0 py-0"
                style={bgStyle}
                title={fullText}
              >
                <span className="text-faint shrink-0 w-[58px]">{fmtTime(l.ts)}</span>
                <span className={`shrink-0 w-[38px] ${colorClass}`}>{lvl}</span>
                <span className="text-faint shrink-0 w-[68px] overflow-hidden text-ellipsis whitespace-nowrap">
                  {l.name}
                </span>
                <span
                  className={`min-w-0 overflow-hidden text-ellipsis whitespace-nowrap ${colorClass}`}
                >
                  {l.msg}
                </span>
              </div>
            )
          })
        )}
      </div>
    </Panel>
  )
}

// ─── Heartbeat Panel ──────────────────────────────────────────────────────────

function StatRow({ label, children }) {
  return (
    <div className="flex items-center justify-between border-b border-border px-4 py-[9px] last:border-b-0">
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span className="mono text-[12px] text-foreground">{children}</span>
    </div>
  )
}

function HeartbeatPanel({ data }) {
  const t      = data.trader
  const alive  = data.alive
  const limits = data.limits ?? {}

  const mode         = t?.mode ?? '—'
  const kronosGate   = t?.kronos_gate ?? '—'
  const openPos      = t?.portfolio?.open_positions?.length ?? 0
  const maxPos       = limits.max_positions ?? '—'
  const feedSubs     = t?.feed?.subscribed ?? 0
  const feedConn     = t?.feed?.connected
  const barsPending  = t?.bars?.pending ?? 0
  const ksArmed      = !t?.kill_switch_active

  // How stale is heartbeat (if the trader exposes heartbeat_age_s)
  const hbAge  = t?.heartbeat_age_s
  const metaTxt = alive
    ? hbAge != null ? `● ${hbAge}s ago` : '● live'
    : '● stale'

  return (
    <Panel>
      <PanelHeader
        title="Heartbeat"
        meta={
          <span className={alive ? 'text-profit text-[11px]' : 'text-loss text-[11px]'}>
            {metaTxt}
          </span>
        }
      />

      <StatRow label="mode">
        <Badge variant={mode === 'LIVE' ? 'profit' : 'amber'}>{mode}</Badge>
      </StatRow>

      <StatRow label="gate">
        <Badge variant="sky">{kronosGate}</Badge>
      </StatRow>

      <StatRow label="positions">
        {openPos} / {maxPos}
      </StatRow>

      <StatRow label="feed">
        {feedConn ? `${feedSubs} WS subs` : 'offline'}
      </StatRow>

      <StatRow label="bars pending">
        {barsPending}
      </StatRow>

      <StatRow label="kill-switch">
        <span className={ksArmed ? 'text-profit' : 'text-loss'}>
          {ksArmed ? 'armed' : 'TRIGGERED'}
        </span>
      </StatRow>
    </Panel>
  )
}

// ─── Infrastructure Panel ─────────────────────────────────────────────────────

function InfraPanel({ dbStats }) {
  const db = dbStats?.data

  return (
    <Panel>
      <PanelHeader title="Infrastructure" meta="ap-south-1" />
      {INFRA_ROWS.map(({ k, v }) => (
        <div key={k} className="flex items-center justify-between border-b border-border px-4 py-[9px] last:border-b-0">
          <span className="text-[12px] text-muted-foreground">{k}</span>
          <span className="mono text-[12px] text-foreground">
            {/* Live-override schema head from DB when available */}
            {k === 'schema head' && db?.alembic ? db.alembic : v}
          </span>
        </div>
      ))}
    </Panel>
  )
}

// ─── Schema / Migrations Panel ────────────────────────────────────────────────

function SchemaPanel({ dbStats }) {
  const db      = dbStats?.data
  const dbHead  = db?.alembic ?? '007'

  return (
    <Panel>
      <PanelHeader
        title="Schema"
        meta={<span className="mono">alembic head {dbHead}</span>}
      />
      {MIGRATION_ROWS.map(({ id, desc, head }) => (
        <div
          key={id}
          className="flex items-center gap-2.5 border-b border-border px-4 py-2 last:border-b-0"
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              flexShrink: 0,
              background: head ? 'hsl(var(--profit))' : 'hsl(var(--border2))',
            }}
          />
          <span className="mono w-[26px] shrink-0 text-[10px] text-faint">{id}</span>
          <span className="mono text-[10.5px] text-muted-foreground">{desc}</span>
        </div>
      ))}
    </Panel>
  )
}

// ─── SystemTab (root) ─────────────────────────────────────────────────────────

export default function SystemTab({ data }) {
  return (
    <div className="px-[22px] pb-10 pt-[18px]">
      {/* KPI row */}
      <KpiRow data={data} />

      {/* Two-column layout: main (1fr) + sidebar (372px) */}
      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-[minmax(0,1fr)_372px] items-start [&>*]:min-w-0">

        {/* LEFT — Services, Rate-limit, Logs */}
        <div className="flex flex-col gap-3.5">
          <ServicesPanel data={data} />
          <RateLimitPanel rateLimitsData={data.rateLimitsData} />
          <LogsPanel logs={data.logs} />
        </div>

        {/* RIGHT — Heartbeat, Infra, Schema */}
        <div className="flex flex-col gap-3.5">
          <HeartbeatPanel data={data} />
          <InfraPanel dbStats={data.dbStats} />
          <SchemaPanel dbStats={data.dbStats} />
        </div>
      </div>
    </div>
  )
}
