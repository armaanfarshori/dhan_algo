import { useState, useEffect } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { INR, colorVar } from './utils'
import Header from './components/Header'
import KpiCard from './components/KpiCard'
import RiskPanel from './components/RiskPanel'
import StrategyPanel from './components/StrategyPanel'
import SignalFeed from './components/SignalFeed'
import PositionsList from './components/PositionsList'
import EquityCurve from './components/EquityCurve'

const grid = {
  main: {
    padding: '20px 24px',
    display: 'grid',
    gridTemplateColumns: 'repeat(4, 1fr)',
    gap: 16,
    maxWidth: 1400,
    margin: '0 auto',
    width: '100%',
  },
}

export default function App() {
  const { status, risk, signals, funds, positions, scalper } = useDashboardData()
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    setRefreshing(true)
    const t = setTimeout(() => setRefreshing(false), 300)
    return () => clearTimeout(t)
  }, [status.data, risk.data])

  const f   = funds?.data?.data
  const r   = risk?.data
  const d   = status?.data
  const totalPnl = r?.total_pnl ?? 0

  return (
    <>
      <Header status={status} refreshing={refreshing} />
      <main style={grid.main}>

        {/* ── KPI row ── */}
        <KpiCard
          label="Available Balance"
          value={f ? INR(f.availabelBalance) : '—'}
          sub={f ? `SOD Limit: ${INR(f.sodLimit)}` : '—'}
        />
        <KpiCard
          label="Total P&L"
          value={r ? INR(totalPnl) : '—'}
          valueColor={colorVar(totalPnl)}
          sub={r ? `Realised ${INR(r.realised_pnl)} | Unrealised ${INR(r.unrealised_pnl)}` : '—'}
        />
        <KpiCard
          label="Open Positions"
          value={r?.open_positions ?? '—'}
          sub={r?.halted ? '⛔ Trading halted' : r?.open_positions === 0 ? 'No open positions' : 'Active'}
        />
        <KpiCard
          label="Orders Placed"
          value={d?.orders_placed ?? '—'}
          sub={d ? `Client ${d.client_id}` : '—'}
        />

        {/* ── Risk + Strategy row ── */}
        <div style={{ gridColumn: '1 / 3' }}>
          <RiskPanel risk={risk} />
        </div>
        <div style={{ gridColumn: '3 / 5' }}>
          <StrategyPanel status={status} scalper={scalper} />
        </div>

        {/* ── Equity curve (full width) ── */}
        <div style={{ gridColumn: '1 / 5' }}>
          <EquityCurve equity={null} />
        </div>

        {/* ── Signals + Positions ── */}
        <div style={{ gridColumn: '1 / 4' }}>
          <SignalFeed signals={signals} />
        </div>
        <div style={{ gridColumn: '4' }}>
          <PositionsList positions={positions} />
        </div>

      </main>
    </>
  )
}
