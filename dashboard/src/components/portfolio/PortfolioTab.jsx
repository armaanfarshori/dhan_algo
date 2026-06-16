/**
 * PortfolioTab — redesigned with shadcn/ui primitives.
 * Single-file, all sub-components colocated.
 * Data wiring preserved 1-to-1 from the original App.jsx components.
 */
import { useState } from 'react'
import { DayPicker } from 'react-day-picker'
import { format } from 'date-fns'
import 'react-day-picker/style.css'

import { Panel, PanelHeader } from '@/components/ui'
import { StatCard } from '@/components/ui'
import { Pill, Tag } from '@/components/ui'
import { PnlAreaChart } from '@/components/charts/PnlAreaChart'
import { INR0, istDateKey } from '@/tokens'

// ─── helpers ─────────────────────────────────────────────────────────────────

/** Compact INR: shows one decimal K when >= 1000, otherwise plain ₹N */
function compact(v) {
  const abs = Math.abs(v)
  if (abs >= 1000) return (v >= 0 ? '+₹' : '−₹') + (abs / 1000).toFixed(1) + 'k'
  return (v >= 0 ? '+₹' : '−₹') + Math.round(abs)
}

/** IST weekday from an YYYY-MM-DD key. Returns 0=Sun … 6=Sat (same as Date.getDay). */
// ─── KPI row ─────────────────────────────────────────────────────────────────

function KpiRow({ tradelog }) {
  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null)
  const wins   = exits.filter(t => (t.pnl ?? 0) > 0)
  const losses = exits.filter(t => (t.pnl ?? 0) <= 0)

  const winRate     = exits.length ? (wins.length / exits.length * 100) : null
  const avgWin      = wins.length   ? wins.reduce((s, t) => s + t.pnl, 0) / wins.length : 0
  const avgLoss     = losses.length ? losses.reduce((s, t) => s + t.pnl, 0) / losses.length : 0
  const profitFactor = avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : null

  const totalPnl = exits.reduce((s, t) => s + (t.pnl ?? 0), 0)

  // Peak-to-trough drawdown (same algorithm as original PortfolioMetrics)
  let peak = 0, eq = 0, maxDDPct = 0, maxDDVal = 0
  exits.forEach(t => {
    eq += t.pnl ?? 0
    if (eq > peak) peak = eq
    if (peak > 0) {
      const dd = (peak - eq) / peak * 100
      const ddv = peak - eq
      if (dd > maxDDPct) { maxDDPct = dd; maxDDVal = ddv }
    }
  })

  // Intraday Sharpe: daily returns from byDate used in CalendarPnL
  const byDate = {}
  exits.forEach(t => {
    if (t.exit_ts) {
      const d = istDateKey(t.exit_ts)
      byDate[d] = (byDate[d] ?? 0) + (t.pnl ?? 0)
    }
  })
  const dailyReturns = Object.values(byDate)
  let sharpe = null
  if (dailyReturns.length >= 2) {
    const mean = dailyReturns.reduce((a, b) => a + b, 0) / dailyReturns.length
    const variance = dailyReturns.reduce((s, r) => s + (r - mean) ** 2, 0) / dailyReturns.length
    const std = Math.sqrt(variance)
    if (std > 0) sharpe = ((mean / std) * Math.sqrt(252)).toFixed(2)
  }

  // Avg R: proxy via expectancy / avg risk (avgWin + |avgLoss| / 2)
  const avgR = (avgWin > 0 && avgLoss < 0)
    ? (avgWin / Math.abs(avgLoss)).toFixed(2)
    : null

  const tradingDays = Object.keys(byDate).length

  return (
    <div className="grid grid-cols-2 gap-3.5 sm:grid-cols-4 mb-3.5">
      {/* Net P&L */}
      <StatCard
        label="Net P&L"
        value={
          <span className={totalPnl >= 0 ? 'text-profit' : 'text-loss'}>
            {totalPnl >= 0 ? '+' : ''}{INR0(totalPnl)}
          </span>
        }
        right={
          <Pill tone="neu">{tradingDays} day{tradingDays !== 1 ? 's' : ''}</Pill>
        }
        sub={<span className="text-muted-foreground">realised · {tradingDays} trading day{tradingDays !== 1 ? 's' : ''}</span>}
      />

      {/* Win Rate */}
      <StatCard
        label="Win Rate"
        value={
          <span>{winRate != null ? `${winRate.toFixed(0)}%` : '—'}</span>
        }
        right={
          winRate != null ? (
            <Pill tone={winRate >= 50 ? 'up' : 'down'}>
              {wins.length}W / {losses.length}L
            </Pill>
          ) : null
        }
        sub={
          <span className="text-muted-foreground">
            profit factor{' '}
            {(profitFactor != null && !Number.isNaN(profitFactor))
              ? <span className="mono">{profitFactor.toFixed(2)}</span>
              : <span className="text-faint">n/a</span>}
          </span>
        }
      />

      {/* Max Drawdown */}
      <StatCard
        label="Max Drawdown"
        value={
          <span className={maxDDVal > 0 ? 'text-loss' : undefined}>
            {maxDDVal > 0 ? `−${INR0(maxDDVal)}` : '—'}
          </span>
        }
        right={
          maxDDPct > 0 ? (
            <Pill tone="down">{maxDDPct.toFixed(1)}%</Pill>
          ) : (
            <Pill tone="neu">—</Pill>
          )
        }
        sub={<span className="text-muted-foreground">peak-to-trough</span>}
      />

      {/* Sharpe */}
      <StatCard
        label="Sharpe (intraday)"
        value={<span>{sharpe ?? '—'}</span>}
        right={<Pill tone="neu">annualised</Pill>}
        sub={
          <span className="text-muted-foreground">
            avg R{' '}
            <span className="mono">{avgR ?? '—'}</span>
          </span>
        }
      />
    </div>
  )
}

// ─── Equity Curve ─────────────────────────────────────────────────────────────

function EquityCurve({ equity, tradelog }) {
  // Same series + same chart component as the Signals "Intraday P&L" so the two
  // share one design.
  const pts = (equity?.data?.intraday ?? []).map(p => ({ t: p.t, v: p.pnl }))
  const last = pts.length ? pts[pts.length - 1].v : 0

  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null && t.exit_ts)
  const byDate = {}
  exits.forEach(t => { const d = istDateKey(t.exit_ts); byDate[d] = (byDate[d] ?? 0) + (t.pnl ?? 0) })
  const tradingDays = Object.keys(byDate).length
  const metaStr = `${last >= 0 ? '+' : ''}${INR0(last)} · ${tradingDays} day${tradingDays !== 1 ? 's' : ''}`

  return (
    <Panel>
      <PanelHeader
        title="Equity Curve"
        meta={<span className={`mono ${last >= 0 ? 'text-profit' : 'text-loss'}`}>{metaStr}</span>}
      />
      <div className="px-3 pb-3 pt-3">
        <PnlAreaChart data={pts} height={240} />
      </div>
    </Panel>
  )
}


// ─── Calendar P&L (react-day-picker) ─────────────────────────────────────────

function CalendarPnL({ tradelog }) {
  const trades = tradelog?.data?.trades ?? []
  const exits  = trades.filter(t => t.status === 'CLOSED' && t.pnl != null && t.exit_ts)
  const byDate = {}
  exits.forEach(t => { const d = istDateKey(t.exit_ts); byDate[d] = (byDate[d] ?? 0) + (t.pnl ?? 0) })

  const tradingDays = Object.keys(byDate).length
  const lastKey = Object.keys(byDate).sort().pop()
  const [month, setMonth] = useState(() => {
    if (lastKey) { const [y, m, d] = lastKey.split('-').map(Number); return new Date(y, m - 1, d) }
    return new Date()
  })
  const todayKey = istDateKey(new Date())

  // Custom cell: day number + realised P&L for that date (theme-tinted).
  function DayCell({ day, modifiers, ...props }) {
    const key = format(day.date, 'yyyy-MM-dd')
    const pnl = byDate[key]
    const has = pnl != null
    const up  = has && pnl >= 0
    const isToday = key === todayKey
    return (
      <td {...props}>
        <div
          className="flex h-full min-h-[44px] flex-col justify-between rounded-md border px-1.5 py-1"
          style={{
            borderColor: isToday ? 'hsl(var(--foreground))'
              : has ? (up ? 'hsl(var(--profit) / .22)' : 'hsl(var(--loss) / .20)') : 'transparent',
            background: has ? (up ? 'hsl(var(--profit) / .12)' : 'hsl(var(--loss) / .12)') : 'transparent',
            opacity: modifiers?.outside ? 0.4 : 1,
          }}
        >
          <span className="mono text-[9.5px] leading-none text-faint">{day.date.getDate()}</span>
          {has && (
            <span
              className="mono text-[11px] font-medium leading-none"
              style={{ color: up ? 'hsl(var(--profit))' : 'hsl(var(--loss))' }}
            >
              {compact(pnl)}
            </span>
          )}
        </div>
      </td>
    )
  }

  return (
    <Panel>
      <PanelHeader title="Calendar P&L" meta={`${tradingDays} trading day${tradingDays !== 1 ? 's' : ''}`} />
      <div className="dhan-cal px-3 py-3">
        <DayPicker
          month={month}
          onMonthChange={setMonth}
          captionLayout="dropdown"
          startMonth={new Date(2025, 0)}
          endMonth={new Date(2027, 11)}
          weekStartsOn={1}
          showOutsideDays
          components={{ Day: DayCell }}
        />
      </div>
    </Panel>
  )
}

// ─── Closed Trades list ───────────────────────────────────────────────────────

function ClosedTradesList({ tradelog }) {
  // Closed trades only, newest first (slice to keep list manageable)
  const trades = tradelog?.data?.trades ?? []
  const closed = trades
    .filter(t => t.status === 'CLOSED' && t.pnl != null)
    .slice(0, 30)

  // Today's count for panel meta
  const today   = istDateKey(new Date())
  const todayCt = closed.filter(t => t.exit_ts && istDateKey(t.exit_ts) === today).length

  return (
    <Panel>
      <PanelHeader
        title="Closed Trades"
        meta={`today · ${todayCt}`}
      />
      <div style={{ maxHeight: 380, overflowY: 'auto' }}>
        {closed.length === 0 ? (
          <div className="mono px-4 py-6 text-[10px] text-faint">
            No closed trades yet
          </div>
        ) : (
          closed.map((t, i) => {
            const side   = t.action === 'SELL' ? 'SHORT' : 'LONG'
            const tone   = side === 'LONG' ? 'long' : 'short'
            const pnl    = t.pnl ?? 0
            const isUp   = pnl >= 0

            // R-multiple: pnl / avg_loss_as_risk_unit (rough proxy)
            // We don't have stop-loss stored, so we omit the R line when unavailable
            const rMult  = t.r_multiple ?? null

            return (
              <div
                key={i}
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'auto 1fr auto',
                  gap: 10,
                  alignItems: 'center',
                  padding: '10px 16px',
                  borderBottom: i < closed.length - 1 ? '1px solid hsl(var(--border))' : 'none',
                }}
              >
                {/* Side tag */}
                <Tag tone={tone}>{side}</Tag>

                {/* Symbol + detail */}
                <div>
                  <div className="text-[12px] font-medium text-foreground">
                    {t.symbol}
                  </div>
                  <div className="mono mt-0.5 text-[10px] text-faint">
                    {Math.abs(t.qty)} qty
                    {t.entry_price != null ? ` · ₹${t.entry_price}` : ''}
                    {t.exit_price  != null ? ` → ₹${t.exit_price}` : ''}
                  </div>
                </div>

                {/* P&L + R */}
                <div className="text-right">
                  <div
                    className="mono text-[12.5px] font-semibold"
                    style={{ color: isUp ? 'hsl(var(--profit))' : 'hsl(var(--loss))' }}
                  >
                    {isUp ? '+' : ''}{INR0(pnl)}
                  </div>
                  {rMult != null && (
                    <div className="mono mt-0.5 text-[10px] text-faint">
                      R {rMult >= 0 ? '+' : ''}{Number(rMult).toFixed(1)}
                    </div>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>
    </Panel>
  )
}

// ─── Performance stats ────────────────────────────────────────────────────────
// All metrics reuse the exact same derivation as the original PortfolioMetrics.

function PerformancePanel({ tradelog }) {
  const trades  = tradelog?.data?.trades ?? []
  const exits   = trades.filter(t => t.status === 'CLOSED' && t.pnl != null)
  const wins    = exits.filter(t => (t.pnl ?? 0) > 0)
  const losses  = exits.filter(t => (t.pnl ?? 0) <= 0)

  const avgWin   = wins.length   ? wins.reduce((s, t) => s + t.pnl, 0) / wins.length : 0
  const avgLoss  = losses.length ? losses.reduce((s, t) => s + t.pnl, 0) / losses.length : 0
  const totalPnl = exits.reduce((s, t) => s + (t.pnl ?? 0), 0)

  const expectancy = exits.length ? totalPnl / exits.length : null

  const largestWin  = wins.length   ? Math.max(...wins.map(t => t.pnl))    : null
  const largestLoss = losses.length ? Math.min(...losses.map(t => t.pnl))  : null

  // Avg hold time (ms → minutes)
  const withTimes = exits.filter(t => t.entry_ts && t.exit_ts)
  const avgHoldMin = withTimes.length
    ? withTimes.reduce((s, t) => s + (new Date(t.exit_ts) - new Date(t.entry_ts)), 0)
      / withTimes.length / 60000
    : null

  const closedToday = (() => {
    const today = istDateKey(new Date())
    return exits.filter(t => t.exit_ts && istDateKey(t.exit_ts) === today).length
  })()

  const stats = [
    {
      label: 'Avg Win',
      value: avgWin > 0 ? `+${INR0(avgWin)}` : '—',
      tone:  avgWin > 0 ? 'profit' : null,
    },
    {
      label: 'Avg Loss',
      value: avgLoss < 0 ? `−${INR0(Math.abs(avgLoss))}` : '—',
      tone:  avgLoss < 0 ? 'loss' : null,
    },
    {
      label: 'Largest Win',
      value: largestWin  != null ? `+${INR0(largestWin)}`          : '—',
      tone:  largestWin  != null ? 'profit' : null,
    },
    {
      label: 'Largest Loss',
      value: largestLoss != null ? `−${INR0(Math.abs(largestLoss))}` : '—',
      tone:  largestLoss != null ? 'loss' : null,
    },
    {
      label: 'Expectancy',
      value: expectancy  != null
        ? `${expectancy >= 0 ? '+' : ''}${INR0(expectancy)} / trade`
        : '—',
      tone: expectancy != null && expectancy >= 0 ? 'profit' : null,
    },
    {
      label: 'Avg Hold',
      value: avgHoldMin != null ? `${Math.round(avgHoldMin)} min` : '—',
      tone: null,
    },
    {
      label: 'Trades closed today',
      value: String(closedToday),
      tone: null,
    },
  ]

  const toneClass = {
    profit: 'text-profit',
    loss:   'text-loss',
  }

  return (
    <Panel>
      <PanelHeader
        title="Performance"
        meta={`today · all closed`}
      />
      <div>
        {stats.map((s, i) => (
          <div
            key={s.label}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '10px 16px',
              borderBottom: i < stats.length - 1 ? '1px solid hsl(var(--border))' : 'none',
            }}
          >
            <span className="text-[12px] text-muted-foreground">{s.label}</span>
            <span
              className={`mono text-[12.5px] font-semibold ${s.tone ? toneClass[s.tone] : 'text-foreground'}`}
            >
              {s.value}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

// ─── Root export ──────────────────────────────────────────────────────────────

export default function PortfolioTab({ data }) {
  return (
    <div className="px-5 py-5 pb-16 lg:px-6">
      {/* KPI row */}
      <KpiRow tradelog={data.tradelog} risk={data.risk} />

      {/* Two-column body */}
      <div className="grid grid-cols-1 items-stretch gap-3.5 lg:grid-cols-[minmax(0,1fr)_372px] [&>*]:min-w-0">
        {/* Left: Equity Curve + Calendar */}
        <div className="flex flex-col gap-3.5 [&>*:last-child]:flex-1">
          <EquityCurve equity={data.equity} tradelog={data.tradelog} />
          <CalendarPnL tradelog={data.tradelog} />
        </div>

        {/* Right: Closed Trades + Performance */}
        <div className="flex flex-col gap-3.5 [&>*:last-child]:flex-1">
          <ClosedTradesList tradelog={data.tradelog} />
          <PerformancePanel tradelog={data.tradelog} />
        </div>
      </div>
    </div>
  )
}
