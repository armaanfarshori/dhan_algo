import { useState, useEffect, useRef, Component } from 'react'
import { useDashboardData } from './hooks/useDashboardData'
import { Tabs } from '@/components/ui'
import { TopBar } from '@/components/shell/TopBar'
import { OfflineBanner } from '@/components/shell/OfflineBanner'
import SignalsTab from '@/components/signals/SignalsTab'
import PortfolioTab from '@/components/portfolio/PortfolioTab'
import SystemTab from '@/components/system/SystemTab'

// ── Error boundary — never blank-screen on a render error ──────────────────
class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(e) { return { error: e } }
  componentDidCatch(e, info) { console.error('Tessera render error:', e, info) }
  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-background p-8 text-foreground">
          <div className="mb-3 text-[13px] font-semibold text-loss">⚠ Render error</div>
          <pre className="mono max-w-[800px] whitespace-pre-wrap text-[11px] text-muted-foreground">
            {this.state.error?.message}
          </pre>
          <button
            onClick={() => this.setState({ error: null })}
            className="mono mt-4 rounded-[var(--radius-sm)] border border-border2 px-3.5 py-1.5 text-[10px] text-muted-foreground hover:text-foreground"
          >
            RETRY
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

const TAB_ITEMS = [
  { value: 'signals',   label: 'Signals' },
  { value: 'portfolio', label: 'Portfolio' },
  { value: 'system',    label: 'System' },
]
const TAB_ORDER = TAB_ITEMS.map(t => t.value)

export default function App() {
  const data = useDashboardData()
  const [tab, setTab] = useState(() => {
    const q = new URLSearchParams(window.location.search).get('tab')
    return ['signals', 'portfolio', 'system'].includes(q) ? q : 'signals'
  })

  // Jump tabs with the 1 / 2 / 3 keys
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return
      const map = { 1: 'signals', 2: 'portfolio', 3: 'system' }
      if (map[e.key]) setTab(map[e.key])
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Swipe left/right between tabs on touch devices
  const touch = useRef(null)
  const onTouchStart = (e) => {
    const t = e.touches[0]
    touch.current = { x: t.clientX, y: t.clientY }
  }
  const onTouchEnd = (e) => {
    if (!touch.current) return
    const t = e.changedTouches[0]
    const dx = t.clientX - touch.current.x
    const dy = t.clientY - touch.current.y
    touch.current = null
    // horizontal swipe only — ignore taps and vertical scrolls
    if (Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.4) return
    const i = TAB_ORDER.indexOf(tab)
    if (dx < 0 && i < TAB_ORDER.length - 1) setTab(TAB_ORDER[i + 1])
    else if (dx > 0 && i > 0) setTab(TAB_ORDER[i - 1])
  }

  return (
    <ErrorBoundary>
      <div className="min-h-screen overflow-x-clip bg-background text-foreground">
        <TopBar data={data} />
        <OfflineBanner data={data} />
        <div className="mx-auto max-w-[1320px] overflow-x-clip px-[22px]">
          <Tabs items={TAB_ITEMS} value={tab} onValueChange={setTab} />
          <ErrorBoundary>
            <div
              role="tabpanel"
              id={`panel-${tab}`}
              aria-labelledby={`tab-${tab}`}
              onTouchStart={onTouchStart}
              onTouchEnd={onTouchEnd}
              className="touch-pan-y"
            >
              {tab === 'signals'   && <SignalsTab   data={data} />}
              {tab === 'portfolio' && <PortfolioTab data={data} />}
              {tab === 'system'    && <SystemTab    data={data} />}
            </div>
          </ErrorBoundary>
        </div>
      </div>
    </ErrorBoundary>
  )
}
