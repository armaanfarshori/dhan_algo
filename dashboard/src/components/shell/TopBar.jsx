import { useState, useEffect } from 'react'
import { Badge, Separator } from '@/components/ui'
import { KillSwitch } from './KillSwitch'
import { ResumeButton } from './ResumeButton'
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
  const bfck = data.backfill?.data?.checkpoint
  // Compute pct from index/total (API rounds to 0.1% → looks frozen for minutes)
  const backfillPct = (bfck?.index != null && bfck?.total)
    ? (bfck.index / bfck.total * 100)
    : bfck?.pct
  const { theme, toggle } = useTheme()

  return (
    <header
      className="sticky top-0 z-30 border-b border-border backdrop-blur-md"
      style={{ background: 'hsl(var(--top-bg) / var(--top-alpha))' }}
    >
      <div className="mx-auto flex h-[58px] max-w-[1320px] items-center gap-2.5 px-3 sm:gap-[18px] sm:px-[22px]">
        {/* LEFT — brand + badges + stats; shrinks/clips before the right cluster */}
        <div className="flex min-w-0 flex-1 items-center gap-2.5 overflow-hidden sm:gap-[18px]">
          {/* brand */}
          <div className="flex shrink-0 items-center gap-2.5 text-[15px] font-bold tracking-[-.02em]">
            <span
              className="h-[9px] w-[9px] rounded-full"
              style={{
                background: alive ? 'hsl(var(--profit))' : 'hsl(var(--loss))',
                boxShadow: `0 0 0 3px hsl(var(--${alive ? 'profit' : 'loss'}) / .15)`,
              }}
            />
            Tessera
          </div>

          {/* PAPER hidden on phones to make room for the kill switch; LIVE always shown (safety) */}
          <Badge variant={mode === 'LIVE' ? 'loss' : 'amber'} className={mode === 'LIVE' ? 'shrink-0' : 'hidden shrink-0 sm:inline-flex'}>{mode}</Badge>
          <Badge variant={gate === 'SHADOW' ? 'default' : 'sky'} className="hidden shrink-0 sm:inline-flex">GATE · {gate}</Badge>
          {halted && <Badge variant="loss" className="shrink-0">⛔ HALTED</Badge>}

          <Separator orientation="vertical" className="hidden sm:block" />

          <div className="hidden items-center gap-[18px] md:flex">
            <Stat k={`Feed · ${subs} WS`} v="●" vClass={feedOk ? 'text-profit' : 'text-faint'} />
            <Stat k="Uptime" v={alive ? fmtUptime(t?.uptime_seconds ?? 0) : '—'} />
            {backfillPct != null && (
              <Stat
                k={bfck?.index != null ? `Backfill · ${bfck.index.toLocaleString('en-IN')}/${bfck.total.toLocaleString('en-IN')}` : 'Backfill'}
                v={`${Number(backfillPct).toFixed(2)}%`}
              />
            )}
          </div>
        </div>

        {/* RIGHT — always fully visible */}
        <div className="flex shrink-0 items-center gap-2.5 sm:gap-4">
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
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            aria-pressed={theme === 'dark'}
            className="grid h-9 w-9 place-items-center rounded-[var(--radius-md)] border border-border2 bg-card text-[15px] text-muted-foreground transition-colors hover:border-faint hover:text-foreground"
          >
            {theme === 'dark' ? '☾' : '☀'}
          </button>
          {halted && alive ? <ResumeButton /> : <KillSwitch />}
        </div>
      </div>
    </header>
  )
}
