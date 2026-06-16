/**
 * SignalsTab — shadcn/ui redesign
 *
 * Single-file component. All data comes from the `data` prop that the parent
 * (App.jsx) computes via useDashboardData(). Zero mock data — every binding
 * points at the same fields the original SignalsTab / ORBCockpit / GatePanel /
 * ExecutionsFeed / TodayPnlCard used.
 *
 * Layout (matches /tmp/dhan-redesign/index.html):
 *   · 4-card KPI row
 *   · 1fr / 372px two-column grid (collapses to 1col < lg)
 *       LEFT : Intraday P&L area-sparkline + ORB Cockpit 3-col grid
 *       RIGHT: Executions feed + Kronos Gate verdict list + API Spend mini bars
 */

import {
  AreaChart, Area, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid,
} from 'recharts'
import {
  Panel, PanelHeader,
  StatCard, Badge, Pill, Tag,
} from '@/components/ui'
import { INR0, fmtTime } from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

function istMinutes() {
  const s = new Date().toLocaleTimeString('en-GB', {
    timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit',
  })
  const [h, m] = s.split(':').map(Number)
  return h * 60 + m
}
const OR_WINDOW_END = 9 * 60 + 30 // 09:30 IST

// Rate-limit category config (mirrors App.jsx RATE_LIMIT_*)
const RL_KEYS   = ['orders', 'data', 'quote', 'non_trading']
const RL_LABELS = { orders: 'Orders', data: 'Data', quote: 'Quote', non_trading: 'Non-trading' }

// ─── KPI: Today P&L ──────────────────────────────────────────────────────────

function KpiPnl({ risk }) {
  const total = risk?.data?.total_pnl    ?? 0
  const rpnl  = risk?.data?.realised_pnl ?? 0
  const upnl  = risk?.data?.unrealised_pnl ?? 0
  const isUp  = total >= 0
  const sub = (
    <span className="text-[11.5px] text-muted-foreground">
      realised{' '}
      <span className={`mono ${isUp ? 'text-profit' : 'text-loss'}`}>
        {rpnl >= 0 ? '+' : ''}{INR0(rpnl)}
      </span>
      {' · '}open{' '}
      <span className={`mono ${upnl >= 0 ? 'text-profit' : 'text-loss'}`}>
        {upnl >= 0 ? '+' : ''}{INR0(upnl)}
      </span>
    </span>
  )
  return (
    <StatCard
      label="Today P&L"
      right={<span className="icn">↗</span>}
      value={<span className={isUp ? 'text-profit' : 'text-loss'}>{isUp ? '+' : ''}{INR0(total)}</span>}
      sub={sub}
    />
  )
}

// ─── KPI: Open Risk ───────────────────────────────────────────────────────────

function KpiOpenRisk({ risk, limits }) {
  const openPositions = risk?.data?.open_positions ?? 0
  const maxPos        = limits?.max_open_positions  ?? 10
  const total         = risk?.data?.total_pnl       ?? 0
  const limit         = limits?.max_daily_loss       ?? 5000
  // "Open risk" is the loss consumed toward the daily limit
  const riskConsumed  = Math.abs(Math.min(total, 0))
  const budgetPct     = Math.min((riskConsumed / limit) * 100, 100)
  const budgetColor   = budgetPct > 75
    ? 'hsl(var(--loss))'
    : budgetPct > 50
      ? 'hsl(var(--amber))'
      : 'hsl(var(--profit))'
  return (
    <StatCard
      label="Open Risk"
      right={<Pill tone="neu">{openPositions} / {maxPos} pos</Pill>}
      value={<span className="text-foreground">{INR0(riskConsumed)}</span>}
      sub={<span className="text-[11.5px] text-muted-foreground">of {INR0(limit)} daily budget</span>}
      bar={{ value: budgetPct, color: budgetColor }}
    />
  )
}

// ─── KPI: Win Rate ────────────────────────────────────────────────────────────

function KpiWinRate({ tradelog }) {
  const summary   = tradelog?.data?.summary ?? {}
  const closed    = summary.closed_today ?? 0
  const wins      = summary.wins_today   ?? 0
  const winRate   = closed > 0 ? Math.round((wins / closed) * 100) : null
  const pf        = summary.profit_factor ?? null
  return (
    <StatCard
      label="Win Rate"
      right={<Pill tone={winRate != null && winRate >= 50 ? 'up' : 'neu'}>{closed} trades</Pill>}
      value={<span className="text-foreground mono">{winRate != null ? `${winRate}%` : '—'}</span>}
      sub={
        <span className="text-[11.5px] text-muted-foreground">
          profit factor <span className="mono">{pf != null ? pf.toFixed(2) : '—'}</span>
        </span>
      }
    />
  )
}

// ─── KPI: Kronos Gate ─────────────────────────────────────────────────────────

function KpiKronosGate({ trader, gate }) {
  const gateMode = trader?.kronos_gate ?? 'SHADOW'
  const cal      = gate?.data?.calibration
  const freshN   = cal?.fresh_n ?? 0
  const isShadow = gateMode === 'SHADOW'
  return (
    <StatCard
      label="Kronos Gate"
      right={<Pill tone="neu">{isShadow ? 'shadow' : 'live'}</Pill>}
      value={<span className="text-foreground" style={{ fontSize: 22 }}>scorer v2</span>}
      sub={
        <span className="text-[11.5px] text-muted-foreground">
          calibration <span className="mono">n={freshN} / 30</span> fresh
        </span>
      }
    />
  )
}

// ─── Intraday P&L sparkline ───────────────────────────────────────────────────

function IntradaySparkline({ equity }) {
  // equity?.data?.intraday = [{t: "09:15", pnl: 0}, ...]
  const pts  = equity?.data?.intraday ?? []
  const last = pts[pts.length - 1]
  const val  = last?.pnl ?? 0
  const isUp = val >= 0

  const profitColor = 'hsl(var(--profit))'
  const lossColor   = 'hsl(var(--loss))'
  const lineColor   = isUp ? profitColor : lossColor

  const metaText = pts.length
    ? `${isUp ? '+' : ''}${INR0(val)} · ${pts[0]?.t ?? '09:15'} → ${last?.t ?? '15:15'} IST`
    : 'No session data'

  return (
    <Panel>
      <PanelHeader
        title="Intraday P&L"
        meta={<span className={`mono ${isUp ? 'text-profit' : 'text-loss'}`}>{metaText}</span>}
      />
      <div className="px-4 pb-4 pt-3">
        {pts.length < 2 ? (
          <div className="flex h-24 items-center justify-center text-[11px] text-muted-foreground mono">
            No session data yet — curve starts at 09:15 IST
          </div>
        ) : (
          <>
            <ResponsiveContainer width="100%" height={96}>
              <AreaChart data={pts} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="pnlGrad" x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stopColor={lineColor} stopOpacity={0.22} />
                    <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid
                  stroke="hsl(var(--border))"
                  strokeDasharray="2 4"
                  vertical={false}
                />
                <XAxis
                  dataKey="t"
                  tick={{ fontFamily: 'var(--font-mono,monospace)', fontSize: 9, fill: 'hsl(var(--faint))' }}
                  tickLine={false}
                  axisLine={{ stroke: 'hsl(var(--border))' }}
                  minTickGap={60}
                />
                <YAxis hide />
                <Tooltip
                  contentStyle={{
                    background: 'hsl(var(--card))',
                    border: '1px solid hsl(var(--border2))',
                    fontFamily: 'var(--font-mono,monospace)',
                    fontSize: 10,
                    borderRadius: 6,
                  }}
                  labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
                  formatter={v => ['₹' + Number(v).toLocaleString('en-IN'), 'P&L']}
                />
                <Area
                  type="monotone"
                  dataKey="pnl"
                  stroke={lineColor}
                  strokeWidth={1.6}
                  fill="url(#pnlGrad)"
                  dot={false}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </>
        )}
      </div>
    </Panel>
  )
}

// ─── ORB range ladder ─────────────────────────────────────────────────────────

function RangeLadder({ s }) {
  const lo = s.or_low, hi = s.or_high, px = s.last_price
  if (!lo || !hi || hi <= lo) {
    const windowOver = istMinutes() > OR_WINDOW_END
    return (
      <div className="flex h-[24px] items-center">
        <span className="text-[9.5px] text-muted-foreground mono">
          {s.or_locked
            ? 'zero-width range — no trade'
            : windowOver
              ? 'no opening range today'
              : 'building opening range…'}
        </span>
      </div>
    )
  }
  const range = hi - lo
  const pad   = Math.max(range * 0.6, hi * 0.002)
  const wLo   = lo - pad
  const span  = (hi + pad) - wLo
  const pct   = v => Math.min(96, Math.max(4, ((v - wLo) / span) * 100))

  return (
    <div className="py-1">
      <div className="relative h-[24px]">
        {/* track */}
        <div
          className="absolute inset-x-0 top-[11px] h-[3px] rounded-sm"
          style={{
            background:
              'linear-gradient(90deg,hsl(var(--loss)/.35),hsl(var(--border2)),hsl(var(--profit)/.35))',
          }}
        />
        {/* price marker dot */}
        {px > 0 && (
          <div
            className="absolute top-[6px] h-[13px] w-[13px] -translate-x-1/2 rounded-full border-[3px] border-card"
            style={{
              left: `${pct(px)}%`,
              background: 'hsl(var(--foreground))',
              boxShadow: `0 0 0 1px hsl(var(--border2))`,
            }}
          />
        )}
        {/* lo / hi labels */}
        <span className="absolute left-0 top-0 mono text-[9.5px] text-faint">
          {lo.toFixed(2)}
        </span>
        <span className="absolute right-0 top-0 mono text-[9.5px] text-faint">
          {hi.toFixed(2)}
        </span>
      </div>
    </div>
  )
}

// ─── Single ORB security card ─────────────────────────────────────────────────

function ORBSecCard({ s, gateDec, maxEntries }) {
  const inPos  = s.position !== 0
  const px = s.last_price
  const lpColor = inPos
    ? (s.position > 0 ? 'text-profit' : 'text-loss')
    : 'text-foreground'

  // Build tag content
  let tagContent
  if (inPos) {
    tagContent = (
      <Tag tone={s.position > 0 ? 'long' : 'short'}>
        {s.position > 0 ? 'LONG' : 'SHORT'} {Math.abs(s.position)} @ {(s.entry_price ?? 0).toFixed(2)}
      </Tag>
    )
  } else if (gateDec) {
    tagContent = (
      <Tag tone="gate">
        GATE {Math.round((gateDec.confidence ?? 0) * 100)}%
      </Tag>
    )
  } else {
    tagContent = <Tag tone="default">watching</Tag>
  }

  return (
    <div className="flex flex-col gap-[9px] bg-card p-[12px_13px] transition-colors hover:bg-panel">
      <div className="flex items-center justify-between">
        <span className="text-[12.5px] font-semibold tracking-[-0.01em] text-foreground">
          {s.ticker ?? s.security_id}
        </span>
        <span className={`mono text-[13px] font-semibold ${lpColor}`}>
          {px > 0 ? px.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}
        </span>
      </div>
      <RangeLadder s={s} />
      <div className="flex items-center gap-[5px]">
        {tagContent}
        <span className="ml-auto mono text-[9px] text-faint">
          {s.entries_today ?? 0}/{maxEntries}
        </span>
      </div>
    </div>
  )
}

// ─── ORB Cockpit ─────────────────────────────────────────────────────────────

function ORBCockpit({ data }) {
  const strategies = data.trader?.strategies ?? []
  const maxEntries = data.limits?.max_orders_per_session ?? 4
  const gateBySid  = {}
  ;(data.gate?.data?.decisions ?? []).forEach(d => {
    if (!gateBySid[d.security_id]) gateBySid[d.security_id] = d
  })

  return (
    <Panel>
      <PanelHeader
        title={
          <span className="flex items-center gap-2">
            ORB Cockpit
            <Badge variant="default" className="text-[9.5px] px-[7px] py-[1px]">
              {strategies.length} securities
            </Badge>
          </span>
        }
        meta="OR locked · 09:15–09:30"
      />
      {strategies.length === 0 ? (
        <div className="p-5 mono text-[10px] text-muted-foreground">
          {data.alive
            ? 'No runners — screener returned 0 securities'
            : 'Engine offline — no live strategy state'}
        </div>
      ) : (
        /* 3-column grid separated by 1px border gaps (bg-border + gap-[1px]) */
        <div
          className="grid gap-[1px] bg-border"
          style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}
        >
          {strategies.map(s => (
            <ORBSecCard
              key={s.security_id}
              s={s}
              gateDec={gateBySid[s.security_id]}
              maxEntries={maxEntries}
            />
          ))}
        </div>
      )}
    </Panel>
  )
}

// ─── Executions feed ──────────────────────────────────────────────────────────

function ExecutionsFeed({ signals }) {
  const raw  = signals?.data
  const feed = (Array.isArray(raw) ? raw : []).slice(0, 20)

  // Count round trips for the meta label
  const exits = feed.filter(s => s.action === 'EXIT').length

  return (
    <Panel>
      <PanelHeader title="Executions" meta={`today · ${exits} round trips`} />
      <div className="overflow-y-auto" style={{ maxHeight: 260 }}>
        {feed.length === 0 ? (
          <div className="p-4 mono text-[10px] text-muted-foreground">
            No executions today — entries appear here when ORB fires
          </div>
        ) : (
          feed.map((s, i) => {
            const pnlColor = s.pnl != null
              ? (s.pnl >= 0 ? 'text-profit' : 'text-loss')
              : 'text-muted-foreground'
            const actCls = s.action === 'BUY'
              ? 'text-profit bg-profit/10'
              : s.action === 'SELL' || s.action === 'EXIT'
                ? 'text-loss bg-loss/10'
                : 'text-muted-foreground bg-border'

            return (
              <div
                key={i}
                className="grid items-center gap-[10px] border-b border-border px-4 py-[10px] last:border-0"
                style={{ gridTemplateColumns: 'auto 1fr auto' }}
              >
                {/* action chip */}
                <span
                  className={`mono w-[42px] rounded-[5px] py-0.5 text-center text-[9.5px] font-bold tracking-[.05em] ${actCls}`}
                >
                  {s.action}
                </span>
                {/* ticker + time */}
                <div>
                  <div className="text-[12px] font-medium text-foreground">
                    {s.ticker ?? s.security_id ?? '—'}
                  </div>
                  <div className="mono text-[10px] text-faint">
                    {fmtTime(s.timestamp)} IST
                  </div>
                </div>
                {/* price / pnl */}
                <span className={`mono text-right text-[12.5px] font-semibold ${pnlColor}`}>
                  {s.pnl != null
                    ? (s.pnl >= 0 ? '+' : '') + INR0(s.pnl)
                    : s.price
                      ? `x${s.qty ?? ''}`
                      : ''}
                </span>
              </div>
            )
          })
        )}
      </div>
    </Panel>
  )
}

// ─── Kronos Gate panel ────────────────────────────────────────────────────────

function GatePanel({ gate }) {
  const decisions = gate?.data?.decisions ?? []
  const cal       = gate?.data?.calibration
  const isShadow  = decisions.some(d => d.shadow)

  return (
    <Panel>
      <PanelHeader
        title={
          <span className="flex items-center gap-2">
            Kronos Gate
            <Badge variant={isShadow ? 'live' : 'profit'} className="text-[9.5px] px-[7px] py-[1px]">
              {isShadow ? 'SHADOW' : 'LIVE'}
            </Badge>
          </span>
        }
        meta="would-allow / block"
      />

      {cal && (
        <div className="border-b border-border px-4 py-[10px] mono text-[9px] text-muted-foreground leading-relaxed">
          <span className="text-faint tracking-[.12em]">CALIBRATION · </span>
          {cal.recommendation}
          {cal.fresh_n != null && (
            <span className="text-faint">
              {' '}({cal.fresh_n} fresh
              {cal.fresh_accuracy != null ? `, acc ${cal.fresh_accuracy}` : ''})
            </span>
          )}
        </div>
      )}

      {decisions.length === 0 ? (
        <div className="p-4 mono text-[10px] text-muted-foreground">
          No gate decisions today — they fire on ORB breakouts
        </div>
      ) : (
        <div className="overflow-y-auto" style={{ maxHeight: 220 }}>
          {decisions.map((d, i) => {
            const isAllow  = d.verdict === 'ALLOW'
            const verdictCls = isAllow
              ? 'text-profit bg-profit/10'
              : 'text-amber bg-amber/10'
            const conf = Math.round((d.confidence ?? 0) * 100)
            return (
              <div
                key={i}
                className="flex items-center justify-between border-b border-border px-4 py-[10px] last:border-0"
              >
                <div className="flex items-center gap-[9px]">
                  <span
                    className={`mono rounded-[5px] px-[7px] py-0.5 text-[9.5px] font-bold tracking-[.04em] ${verdictCls}`}
                  >
                    {d.shadow ? `~${d.verdict}` : d.verdict}
                  </span>
                  <span className="text-[12px] font-medium text-foreground">{d.ticker}</span>
                </div>
                <div className="flex items-center gap-2">
                  {/* confidence mini-bar */}
                  <div className="h-[4px] w-[46px] overflow-hidden rounded-[3px] bg-border">
                    <div
                      className="h-full rounded-[3px]"
                      style={{
                        width: `${conf}%`,
                        background: 'hsl(var(--sky))',
                      }}
                    />
                  </div>
                  <span className="mono text-[10px] text-faint">{(conf / 100).toFixed(2)}</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Panel>
  )
}

// ─── API Spend mini panel ─────────────────────────────────────────────────────

function ApiSpendMini({ rateLimitsData }) {
  const categories = rateLimitsData?.data?.categories ?? null
  const date       = rateLimitsData?.data?.date ?? 'today'
  const hasData    = categories != null
  const loading    = rateLimitsData?.loading ?? true

  return (
    <Panel>
      <PanelHeader title="API Spend · today" meta={<span className="mono">{hasData ? `${date} · account-wide` : loading ? 'loading…' : 'endpoint error'}</span>} />
      <div className="py-1 px-1">
        {RL_KEYS.map(key => {
          const cat    = categories?.[key] ?? {}
          const total  = cat.total ?? null
          const perDay = cat.per_day ?? null
          const capped = perDay != null
          const pct    = capped && total != null
            ? Math.min((total / perDay) * 100, 100)
            : 0
          const barColor = pct > 80
            ? 'hsl(var(--loss))'
            : pct > 50
              ? 'hsl(var(--amber))'
              : 'hsl(var(--sky))'
          const capLabel = capped
            ? '/ ' + (perDay >= 1000 ? (perDay / 1000).toFixed(0) + 'k' : perDay)
            : 'no cap'

          return (
            <div key={key} className="flex items-center gap-[11px] px-3 py-2">
              <span className="mono w-[80px] flex-shrink-0 text-[11px] text-muted-foreground">
                {RL_LABELS[key]}
              </span>
              <div className="flex-1">
                <div className="h-[4px] overflow-hidden rounded-[3px] bg-border">
                  <div
                    className="h-full rounded-[3px] transition-[width] duration-500"
                    style={{
                      width: `${capped ? Math.max(3, pct) : 0}%`,
                      background: barColor,
                    }}
                  />
                </div>
              </div>
              <span className="mono w-[56px] text-right text-[11px] text-foreground">
                {total != null ? total.toLocaleString('en-IN') : '—'}
              </span>
              <span className="mono w-[52px] text-right text-[10px] text-faint">
                {hasData ? capLabel : '—'}
              </span>
            </div>
          )
        })}
        {/* by-process breakdown */}
        {hasData && (
          <div className="mt-1 border-t border-border px-3 py-[8px] mono text-[10px] text-faint">
            {RL_KEYS.map(key => {
              const bp = categories?.[key]?.by_process
              if (!bp || typeof bp !== 'object') return null
              const parts = Object.entries(bp)
                .filter(([, v]) => (v ?? 0) > 0)
                .map(([k, v]) => `${k} ${Number(v).toLocaleString('en-IN')}`)
                .join(' · ')
              return parts ? (
                <div key={key} className="leading-relaxed">
                  <span className="text-muted-foreground">{RL_LABELS[key]}: </span>{parts}
                </div>
              ) : null
            })}
          </div>
        )}
      </div>
    </Panel>
  )
}

// ─── Root SignalsTab ───────────────────────────────────────────────────────────

export default function SignalsTab({ data }) {
  // Derived from the same snapshot the original SignalsTab read
  const risk    = data.risk        // { data: { realised_pnl, unrealised_pnl, total_pnl, open_positions, halted } }
  const limits  = data.limits      // { max_daily_loss, max_open_positions, max_orders_per_session, ... }
  const trader  = data.trader      // heartbeat trader block: { strategies, risk, portfolio, kronos_gate, ... }
  const gate    = data.gate        // { data: { decisions, calibration } }
  const equity  = data.equity      // { data: { intraday: [{t, pnl}] } }
  const signals = data.signals     // { data: [...signal rows] }
  const tradelog = data.tradelog   // { data: { summary: { closed_today, wins_today, profit_factor } } }
  const rateLimitsData = data.rateLimitsData  // { data: { date, categories: { orders, data, quote, non_trading } } }

  return (
    <div className="px-[22px] pb-16 pt-[22px]">

      {/* ── KPI row ── */}
      <section
        className="mb-[14px] grid gap-[14px]"
        style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}
      >
        <KpiPnl     risk={risk}    limits={limits} />
        <KpiOpenRisk risk={risk}   limits={limits} />
        <KpiWinRate  tradelog={tradelog} />
        <KpiKronosGate trader={trader} gate={gate} />
      </section>

      {/* ── Two-column body ── */}
      <div className="grid grid-cols-1 items-start gap-[14px] lg:grid-cols-[1fr_372px]">

        {/* LEFT: sparkline + cockpit */}
        <div className="flex flex-col gap-[14px]">
          <IntradaySparkline equity={equity} />
          <ORBCockpit data={data} />
        </div>

        {/* RIGHT: executions + gate + API spend */}
        <div className="flex flex-col gap-[14px]">
          <ExecutionsFeed signals={signals} />
          <GatePanel gate={gate} />
          <ApiSpendMini rateLimitsData={rateLimitsData} />
        </div>

      </div>
    </div>
  )
}
