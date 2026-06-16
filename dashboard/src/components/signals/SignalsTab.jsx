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
  Panel, PanelHeader,
  StatCard, Badge, Pill, Tag,
} from '@/components/ui'
import { PnlAreaChart } from '@/components/charts/PnlAreaChart'
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
          profit factor{' '}
          {(pf != null && !Number.isNaN(pf))
            ? <span className="mono">{pf.toFixed(2)}</span>
            : <span className="text-faint">n/a</span>}
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
  const all = equity?.data?.intraday ?? []
  // Reflect the trading session: clamp to 09:15–15:30 IST when those points exist.
  const session = all.filter(p => p.t >= '09:15' && p.t <= '15:30')
  const pts = (session.length >= 2 ? session : all).map(p => ({ t: p.t, v: p.pnl }))
  const val  = pts.length ? pts[pts.length - 1].v : 0
  const isUp = val >= 0

  const metaText = pts.length
    ? `${isUp ? '+' : ''}${INR0(val)} · 09:15 → 15:30 IST`
    : 'No session data'

  return (
    <Panel>
      <PanelHeader
        title="Intraday P&L"
        meta={<span className={`mono ${isUp ? 'text-profit' : 'text-loss'}`}>{metaText}</span>}
      />
      <div className="px-3 pb-3 pt-3">
        <PnlAreaChart data={pts} height={240} />
      </div>
    </Panel>
  )
}

// ─── ORB range ladder ─────────────────────────────────────────────────────────

function RangeLadder({ s }) {
  const lo = s.or_low, hi = s.or_high, px = s.last_price
  if (!lo || !hi || hi <= lo) {
    // No opening range yet (pre-09:30, or after-hours when engine state resets).
    // Render a tidy flat track so the cockpit grid stays uniform, not text-cluttered.
    const note = s.or_locked
      ? 'no range'
      : istMinutes() > OR_WINDOW_END ? 'no OR today' : 'building…'
    return (
      <div className="py-1">
        <div className="relative h-[18px]">
          <div className="absolute inset-x-0 top-[11px] h-[3px] rounded-sm bg-border" />
          <span className="absolute right-0 top-0 mono text-[9px] text-faint">{note}</span>
        </div>
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
      <div className="relative h-[18px]">
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
    <div className="flex min-w-0 flex-col gap-[6px] bg-card px-3 py-[9px] transition-colors hover:bg-panel">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="min-w-0 truncate text-[12.5px] font-semibold tracking-[-0.01em] text-foreground">
          {s.ticker ?? s.security_id}
        </span>
        <span className={`mono flex-shrink-0 text-[13px] font-semibold ${lpColor}`}>
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
        /* 3-column grid: row gaps rendered as 1px border lines (bg-border bleed),
           column gap widened to gap-x-2 (8 px) so adjacent price numbers never touch */
        <div
          className="grid gap-x-2 gap-y-[1px] bg-border"
          style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 215px), 1fr))' }}
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

// The /api/signals rows look like:
//   { action:'EXIT', price:436.56, reason:'ORB exit  PnL ₹+48.00',
//     timestamp:'...+00:00', source:'ORB APOLLO' }
// The symbol lives in `source` and the realised P&L is embedded in `reason`.
function parseExec(s) {
  const symbol = (s.source || '').replace(/^ORB\s+/i, '').trim() || null
  let pnl = null
  if (s.reason) {
    const m = s.reason.match(/PnL\s*₹?\s*([+-]?[\d,]+(?:\.\d+)?)/i)
    if (m) pnl = parseFloat(m[1].replace(/,/g, ''))
  }
  return { symbol, pnl }
}

function ExecutionsFeed({ signals }) {
  const raw  = signals?.data
  const feed = (Array.isArray(raw) ? raw : []).slice(0, 30)
  const exits = feed.filter(s => s.action === 'EXIT').length

  return (
    <Panel className="flex h-full flex-col">
      <PanelHeader title="Executions" meta={`today · ${exits} round trips`} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {feed.length === 0 ? (
          <div className="p-4 mono text-[10px] text-muted-foreground">
            No executions today — entries appear here when ORB fires
          </div>
        ) : (
          feed.map((s, i) => {
            const { symbol, pnl } = parseExec(s)
            const isExit = s.action === 'EXIT'
            const actCls = s.action === 'BUY'
              ? 'text-profit bg-profit/10'
              : (s.action === 'SELL' || isExit)
                ? 'text-loss bg-loss/10'
                : 'text-muted-foreground bg-border'
            const pnlColor = pnl != null
              ? (pnl >= 0 ? 'text-profit' : 'text-loss')
              : 'text-muted-foreground'

            return (
              <div
                key={i}
                className="grid items-center gap-[10px] border-b border-border px-4 py-[10px] last:border-0"
                style={{ gridTemplateColumns: 'auto minmax(0,1fr) auto' }}
              >
                <span className={`mono w-[42px] rounded-[5px] py-0.5 text-center text-[9.5px] font-bold tracking-[.05em] ${actCls}`}>
                  {s.action}
                </span>
                <div className="min-w-0">
                  <div className="truncate text-[12px] font-medium text-foreground">
                    {symbol ?? <span className="text-faint">—</span>}
                  </div>
                  <div className="mono text-[10px] text-faint">{fmtTime(s.timestamp)} IST</div>
                </div>
                <span className={`mono text-right text-[12.5px] font-semibold ${pnlColor}`}>
                  {pnl != null
                    ? (pnl >= 0 ? '+' : '') + INR0(pnl)
                    : (s.price ? '₹' + s.price.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '')}
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
    <Panel className="flex h-full flex-col">
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
        <div className="min-h-0 flex-1 p-4 mono text-[10px] text-muted-foreground">
          No gate decisions today — they fire on ORB breakouts
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
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

  return (
    <div className="px-4 pb-16 pt-5 sm:px-[22px]">

      {/* ── KPI row ── */}
      <section className="mb-[14px] grid grid-cols-2 gap-[14px] lg:grid-cols-4">
        <KpiPnl     risk={risk} />
        <KpiOpenRisk risk={risk}   limits={limits} />
        <KpiWinRate  tradelog={tradelog} />
        <KpiKronosGate trader={trader} gate={gate} />
      </section>

      {/* Intraday P&L — full-width hero */}
      <div className="mb-[14px]">
        <IntradaySparkline equity={equity} />
      </div>

      {/* ORB Cockpit — full-width (uses the width for more columns, so it
          stays short even with many securities) */}
      <div className="mb-[14px]">
        <ORBCockpit data={data} />
      </div>

      {/* Executions + Kronos Gate — side by side at the bottom, equal height */}
      <div className="grid grid-cols-1 items-stretch gap-[14px] lg:grid-cols-2 [&>*]:min-w-0">
        <ExecutionsFeed signals={signals} />
        <GatePanel gate={gate} />
      </div>
    </div>
  )
}
