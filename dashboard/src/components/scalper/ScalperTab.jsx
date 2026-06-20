/**
 * ScalperTab — intraday options-scalper forward-paper panel.
 *
 * The scalper is un-backtestable (no intraday option-premium series) → it is
 * validated by forward paper-trading. This tab surfaces that track: KPI strip,
 * a start/stop control bar, the Daily Risk Governor (limits vs today's totals),
 * the live signal + open-tranche snapshot (with a client-side hold timer), and
 * the recent-trades log.
 *
 * The scalper may be OFF — every sub-panel renders its own empty/loading state,
 * so the tab is never a broken grid (same discipline as FnoTab).
 */
import ScalperKpiRow from './ScalperKpiRow'
import ScalperControlBar from './ScalperControlBar'
import GovernorPanel from './GovernorPanel'
import LiveSignalPanel from './LiveSignalPanel'
import ScalperTradesTable from './ScalperTradesTable'

export default function ScalperTab({ data }) {
  const live     = data?.scalperLive?.data ?? null
  const governor = data?.scalperGovernor?.data ?? null
  const trades   = data?.scalperTrades?.data ?? null

  return (
    <div className="px-4 pb-10 pt-5 sm:px-[22px]">
      {/* Row 1 — KPI strip */}
      <ScalperKpiRow live={live} governor={governor} trades={trades} />

      {/* Row 2 — control bar (start/stop) */}
      <ScalperControlBar live={live} />

      {/* Row 3 — governor (sidebar) + live signal (wide) */}
      <div className="mb-3.5 grid grid-cols-1 items-start gap-3.5 lg:grid-cols-[360px_minmax(0,1fr)] [&>*]:min-w-0">
        <GovernorPanel governor={governor} />
        <LiveSignalPanel live={live} />
      </div>

      {/* Row 4 — recent trades */}
      <ScalperTradesTable trades={trades} />

      {/* Honesty footer */}
      <div className="mono mt-3.5 px-1 text-[10px] leading-relaxed text-faint">
        The scalper is <span className="text-amber">un-backtestable</span> (no intraday
        option-premium data) — this is a <span className="text-amber">forward paper</span>{' '}
        track, not a validated live edge. PAPER throughout.
      </div>
    </div>
  )
}
