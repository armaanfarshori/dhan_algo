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

// NOTE: instance types are static config (no live EC2-metadata source in the
// heartbeat) — keep in sync with infra/terraform.tfvars. The DB is on r7g.2xlarge
// temporarily for the M2.5 build; it reverts to t4g.medium after the downgrade.
// 'schema head' is overridden live from db_stats.alembic in InfraPanel; '008'
// here is only the offline fallback.
const INFRA_ROWS = [
  { k: 'agent',         v: 't4g.large · 2 vCPU · 8 GB' },
  { k: 'db',            v: 'r7g.2xlarge · 64 GB (M2.5; → t4g.medium)' },
  { k: 'db engine',     v: 'PostgreSQL 16 + TimescaleDB (bare-metal)' },
  { k: 'EBS snapshots', v: 'DLM daily' },
  { k: 'TF state',      v: 'S3 + DynamoDB lock' },
  { k: 'schema head',   v: '008' },
  { k: 'region',        v: 'ap-south-1' },
]

const MIGRATION_ROWS = [
  { id: '008', desc: 'daily_screen audit table',      head: true  },
  { id: '007', desc: 'api_usage table',               head: false },
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

// Seconds since the trader's last heartbeat (derived from its ts; the payload
// has no heartbeat_age_s field). Returns null if no timestamp.
function heartbeatAgeS(t) {
  if (!t?.ts) return null
  const ms = Date.now() - new Date(t.ts).getTime()
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 1000)) : null
}

// ─── KPI Row ─────────────────────────────────────────────────────────────────

function KpiRow({ data }) {
  const t   = data.trader
  const db  = data.dbStats?.data
  const bf  = data.backfill?.data
  const ck  = bf?.checkpoint ?? {}
  // Compute pct client-side from index/total — the API rounds pct to 0.1%
  // (~9 securities), so it only ticks every few minutes; 2-decimal here moves visibly.
  // Clamp to [0,100] so a transient index>total (or a bad pct) can't render a
  // negative "% left" (line below shows 100 - pct).
  const pct = Math.min(100, Math.max(0,
    (ck.index != null && ck.total) ? (ck.index / ck.total * 100) : (ck.pct ?? 0)))

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
  const ageS    = heartbeatAgeS(t)
  const hbAge   = ageS != null ? `heartbeat ${ageS}s ago` : 'heartbeat stale'
  const uptimeSub = data.alive ? `trader · ${hbAge}` : 'heartbeat stale'

  return (
    <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4 mb-3.5">
      <StatCard
        label="Backfill"
        value={pct > 0 ? `${Number(pct).toFixed(2)}%` : '—'}
        valueClassName="text-foreground"
        right={<Pill tone="neu">NSE_EQ</Pill>}
        sub={
          ck.index != null
            ? `${ck.index?.toLocaleString('en-IN')}/${ck.total?.toLocaleString('en-IN')} · ~${Number(100 - pct).toFixed(2)}% left`
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
    <div
      className="grid items-center gap-3 border-b border-border px-4 py-[11px] last:border-b-0"
      style={{ gridTemplateColumns: 'minmax(0,1fr) auto 78px' }}
    >
      {/* name + dot — left */}
      <div className="flex min-w-0 items-center gap-2.5">
        <span style={dotStyle} />
        <div className="min-w-0">
          <div className="mono text-[12px] font-medium text-foreground">{name}</div>
          {sub && <div className="text-[9.5px] text-faint mt-[1px]">{sub}</div>}
        </div>
      </div>
      {/* info text — middle */}
      <span className="mono whitespace-nowrap text-right text-[10.5px] text-faint">{uptime || ''}</span>
      {/* status — consistent right column */}
      <div className="flex justify-end">
        <Badge variant={badgeVariant}>{state}</Badge>
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
        title="API Spend"
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
                    // Only floor the width when there is actual spend — a 0-usage
                    // endpoint must render an empty track, not a 3% sliver.
                    width: usagePct ? `${Math.max(usagePct, 3)}%` : '0%',
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
      <div className="mono max-h-[460px] overflow-auto px-4 py-3 text-[10.5px] leading-[1.7]">
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
  const maxPos       = limits.max_open_positions ?? '—'
  const feedSubs     = t?.feed?.subscribed ?? 0
  const feedConn     = t?.feed?.connected
  const barsPending  = t?.bars?.pending ?? 0
  // kill-switch row reflects the kill switch SPECIFICALLY (not a loss halt).
  // kill_switch is the precise heartbeat flag; fall back to halted for older
  // trader builds that don't emit it yet.
  const ksArmed      = !(t?.risk?.kill_switch ?? t?.risk?.halted)

  // Heartbeat staleness — derive from the heartbeat timestamp (no heartbeat_age_s field)
  const hbAge  = heartbeatAgeS(t)
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
  const dbHead  = db?.alembic ?? '008'

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
    <div className="px-4 pb-10 pt-5 sm:px-[22px]">
      {/* KPI row */}
      <KpiRow data={data} />

      {/* Services · Heartbeat · Infrastructure · Schema — one responsive row,
          all cards stretched to equal height (clean aligned bottoms). */}
      <div
        className="mb-3.5 grid items-stretch gap-3.5 [&>*]:h-full"
        style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 270px), 1fr))' }}
      >
        <ServicesPanel data={data} />
        <HeartbeatPanel data={data} />
        <InfraPanel dbStats={data.dbStats} />
        <SchemaPanel dbStats={data.dbStats} />
      </div>

      {/* API Spend — full width */}
      <div className="mb-3.5">
        <RateLimitPanel rateLimitsData={data.rateLimitsData} />
      </div>

      {/* Recent Logs — full width, at the bottom */}
      <LogsPanel logs={data.logs} />
    </div>
  )
}
