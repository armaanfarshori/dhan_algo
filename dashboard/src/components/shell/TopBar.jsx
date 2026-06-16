import { useState, useEffect } from 'react'
import { Badge, Separator } from '@/components/ui'
import { KillSwitch } from './KillSwitch'
import { useTheme } from '@/hooks/useTheme'
import { INR0, colorPnl, fmtUptime } from '@/tokens'

function ClockIST() {
  const [t, setT] = useState(new Date())
  useEffect(() => { const i = setInterval(() => setT(new Date()), 1000); return () => clearInterval(i) }, [])
  return (
    <span className="mono text-[11px] text-faint">
      {t.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST
    </span>
  )
}

function Stat({ k, v, vClass }) {
  return (
    <div className="flex flex-col gap-px">
      <span className={`mono text-[13px] font-semibold ${vClass ?? ''}`}>{v}</span>
      <span className="text-[9.5px] uppercase tracking-[.07em] text-faint">{k}</span>
    </div>
  )
}

export function TopBar({ data }) {
  const t = data.trader
  const alive = data.alive
  const mode = t?.mode ?? 'PAPER'
  const gate = (t?.kronos_gate ?? 'shadow').toUpperCase()
  const feedOk = !!t?.feed?.connected
  const subs = t?.feed?.subscribed ?? 0
  // Use the RiskEngine total (realised+unrealised) — same source as the
  // Signals "Today P&L" KPI. trader.portfolio.total_pnl can read 0 after EOD.
  const pnl = t?.risk?.total_pnl ?? t?.portfolio?.total_pnl ?? 0
  const halted = !!t?.risk?.halted
  const backfillPct = data.backfill?.data?.checkpoint?.pct
  const { theme, toggle } = useTheme()

  return (
    <header
      className="sticky top-0 z-30 border-b border-border backdrop-blur-md"
      style={{ background: 'hsl(var(--top-bg) / var(--top-alpha))' }}
    >
      <div className="mx-auto flex h-[58px] max-w-[1320px] items-center gap-[18px] px-[22px]">
        {/* brand */}
        <div className="flex items-center gap-2.5 text-[15px] font-bold tracking-[-.02em]">
          <span
            className="h-[9px] w-[9px] rounded-full"
            style={{
              background: alive ? 'hsl(var(--profit))' : 'hsl(var(--loss))',
              boxShadow: `0 0 0 3px hsl(var(--${alive ? 'profit' : 'loss'}) / .15)`,
            }}
          />
          DhanAI
        </div>

        <Badge variant={mode === 'LIVE' ? 'loss' : 'amber'}>{mode}</Badge>
        <Badge variant={gate === 'SHADOW' ? 'default' : 'sky'}>GATE · {gate}</Badge>
        {halted && <Badge variant="loss">⛔ HALTED</Badge>}

        <Separator orientation="vertical" className="hidden sm:block" />

        <div className="hidden items-center gap-[18px] md:flex">
          <Stat k={`Feed · ${subs} WS`} v="●" vClass={feedOk ? 'text-profit' : 'text-faint'} />
          <Stat k="Uptime" v={alive ? fmtUptime(t?.uptime_seconds ?? 0) : '—'} />
          {backfillPct != null && <Stat k="Backfill" v={`${backfillPct}%`} />}
        </div>

        <div className="ml-auto flex items-center gap-4">
          <div className="hidden lg:block"><ClockIST /></div>
          <div className="hidden text-right sm:block">
            <div className="text-[9.5px] font-semibold uppercase tracking-[.07em] text-faint">Today P&L</div>
            <div className="mono text-[21px] font-semibold leading-none" style={{ color: colorPnl(pnl) }}>
              {pnl >= 0 ? '+' : ''}{INR0(pnl)}
            </div>
          </div>
          <button
            onClick={toggle}
            title="Toggle light / dark"
            aria-label="Toggle theme"
            className="grid h-[34px] w-[34px] place-items-center rounded-[var(--radius-md)] border border-border2 bg-card text-[15px] text-muted-foreground transition-colors hover:border-faint hover:text-foreground"
          >
            {theme === 'dark' ? '☾' : '☀'}
          </button>
          <KillSwitch />
        </div>
      </div>
    </header>
  )
}
