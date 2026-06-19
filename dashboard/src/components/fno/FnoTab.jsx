/**
 * FnoTab — F&O (NIFTY index-options) research panel.
 * Surfaces the foundation: VRP (India VIX vs realized vol) + live vol-gate,
 * the forward real-IV condor paper-log, the latest ATM option-chain snapshot,
 * and the (preliminary) condor backtest summary.
 *
 * Visual integration is hand-assembled (no agent drift); leaf components are
 * colocated under components/fno/, the dual-line chart under components/charts/.
 * Every sub-panel renders its own empty/loading state, so the tab is never a
 * broken grid when the F&O tables are empty (data accrues via the 16:00 IST cron).
 */
import { Panel, PanelHeader } from '@/components/ui'
import VrpChart from '@/components/charts/VrpChart'
import FnoKpiRow from './FnoKpiRow'
import PaperLogTable from './PaperLogTable'
import ChainSnapshotTable from './ChainSnapshotTable'
import BacktestSummaryCard from './BacktestSummaryCard'

export default function FnoTab({ data }) {
  const vrp      = data?.fnoVrp?.data ?? null
  const series   = data?.fnoVrpSeries?.data?.series ?? []
  const paper    = data?.fnoPaper?.data ?? null
  const chain    = data?.fnoChain?.data ?? null
  const backtest = data?.fnoBacktest?.data ?? null

  return (
    <div className="px-4 pb-10 pt-5 sm:px-[22px]">
      {/* Row 1 — KPI strip: gate / VIX / realized / VRP edge */}
      <FnoKpiRow vrp={vrp} />

      {/* Row 2 — hero VRP chart (wide) + backtest summary (sidebar) */}
      <div className="mb-3.5 grid grid-cols-1 items-stretch gap-3.5 lg:grid-cols-[minmax(0,1fr)_360px] [&>*]:min-w-0">
        <Panel>
          <PanelHeader
            title="Volatility Risk Premium"
            meta="India VIX vs NIFTY realized (20d)"
          />
          <div className="px-1 pb-1 pt-2">
            <VrpChart series={series} height={248} />
          </div>
          <div className="mono px-3 pb-3 pt-1 text-[10px] leading-relaxed text-faint">
            VRP = India VIX (implied) − NIFTY realized vol. India VIX (~30d) is only
            a proxy for weekly (~4–7 DTE) IV; that relationship is{' '}
            <span className="text-amber">regime-dependent</span> — in calm regimes
            real weekly IV can sit below VIX, so the edge is not reliably conservative.
          </div>
        </Panel>
        <BacktestSummaryCard backtest={backtest} />
      </div>

      {/* Row 3 — forward real-IV condor paper-log */}
      <div className="mb-3.5">
        <PaperLogTable paper={paper} />
      </div>

      {/* Row 4 — latest ATM option-chain snapshot */}
      <ChainSnapshotTable chain={chain} />
    </div>
  )
}
