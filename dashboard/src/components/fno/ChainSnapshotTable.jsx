import { Panel, PanelHeader } from '@/components/ui'
import { fmtTime } from '@/tokens'

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Format a fraction IV to a percentage string e.g. 0.121 → "12.1%" */
function fmtIv(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return (Number(v) * 100).toFixed(1) + '%'
}

/** "2026-06-27" → "27 Jun 2026" (falls back to raw on parse failure) */
function fmtExpiry(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

/** Format OI — compact thousands e.g. 1234567 → "12.3L" */
function fmtOi(v) {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  if (n >= 1e7) return (n / 1e7).toFixed(1) + 'Cr'
  if (n >= 1e5) return (n / 1e5).toFixed(1) + 'L'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k'
  return String(n)
}

/** Format LTP — show 2 decimals, blank for null */
function fmtLtp(v) {
  if (v == null) return '—'
  return Number(v).toFixed(2)
}

// ─── Column header ────────────────────────────────────────────────────────────

function ColHead({ children, right }) {
  return (
    <div
      className={`mono text-[9px] font-semibold uppercase tracking-[.07em] text-faint ${right ? 'text-right' : 'text-left'}`}
    >
      {children}
    </div>
  )
}

// ─── Single chain row ─────────────────────────────────────────────────────────

// gridTemplateColumns mirrors the header — keep in sync
const GRID_COLS = 'minmax(52px,auto) 44px 44px 52px 52px 50px 50px'

function ChainRow({ row, isAtm }) {
  const ce = row?.ce ?? null
  const pe = row?.pe ?? null

  const rowStyle = isAtm
    ? {
        borderTop:    '1px solid hsl(var(--foreground) / .25)',
        borderBottom: '1px solid hsl(var(--foreground) / .25)',
        background:   'hsl(var(--foreground) / .04)',
      }
    : {}

  return (
    <div
      className="grid items-center gap-x-2 border-b border-border px-3 py-[5px] last:border-b-0"
      style={{ gridTemplateColumns: GRID_COLS, ...rowStyle }}
    >
      {/* Strike — centre-aligned, slightly brighter when ATM */}
      <div
        className={`mono text-[11px] ${isAtm ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}
      >
        {row?.strike ?? '—'}
      </div>

      {/* CE IV */}
      <div className="mono text-right text-[10px] text-sky">
        {fmtIv(ce?.iv)}
      </div>

      {/* PE IV */}
      <div className="mono text-right text-[10px] text-amber">
        {fmtIv(pe?.iv)}
      </div>

      {/* CE OI */}
      <div className="mono text-right text-[10px] text-muted-foreground">
        {fmtOi(ce?.oi)}
      </div>

      {/* PE OI */}
      <div className="mono text-right text-[10px] text-muted-foreground">
        {fmtOi(pe?.oi)}
      </div>

      {/* LTP CE */}
      <div className="mono text-right text-[10px] text-foreground">
        {fmtLtp(ce?.ltp)}
      </div>

      {/* LTP PE */}
      <div className="mono text-right text-[10px] text-foreground">
        {fmtLtp(pe?.ltp)}
      </div>
    </div>
  )
}

// ─── Header row ───────────────────────────────────────────────────────────────

function TableHeader() {
  return (
    <div
      className="grid items-center gap-x-2 border-b border-border bg-card px-3 py-[6px]"
      style={{ gridTemplateColumns: GRID_COLS }}
    >
      <ColHead>Strike</ColHead>
      <ColHead right>CE IV</ColHead>
      <ColHead right>PE IV</ColHead>
      <ColHead right>CE OI</ColHead>
      <ColHead right>PE OI</ColHead>
      <ColHead right>LTP CE</ColHead>
      <ColHead right>LTP PE</ColHead>
    </div>
  )
}

// ─── ChainSnapshotTable ───────────────────────────────────────────────────────

/**
 * Prop contract
 * -------------
 * chain: null  →  shows "no data" empty state
 * chain: {
 *   available:     boolean,          // false → empty state
 *   snapshot_time: ISO string,       // rendered as IST HH:MM:SS
 *   age_min:       number,           // minutes since snapshot
 *   stale:         boolean,          // tone meta amber + "· stale" suffix
 *   expiry:        string,           // e.g. "27-Jun-2026"
 *   spot:          number,           // NIFTY spot
 *   atm_strike:    number,           // ATM strike for row highlight
 *   iv_unit:       "fraction",       // IVs are fractions (×100 for %)
 *   expiries:      string[],         // available expiries (not rendered)
 *   rows: [{
 *     strike: number,
 *     ce: { ltp, oi, iv, delta } | null,
 *     pe: { ltp, oi, iv, delta } | null,
 *   }],
 * }
 */
export default function ChainSnapshotTable({ chain }) {
  // ── Derived state ────────────────────────────────────────────────────────

  const available = chain?.available !== false
  const rows      = chain?.rows ?? []
  const empty     = !chain || !available || rows.length === 0

  const stale      = chain?.stale ?? false
  const expiry     = fmtExpiry(chain?.expiry)
  const spot       = (chain?.spot != null && Number.isFinite(Number(chain.spot)))
    ? Number(chain.spot).toLocaleString('en-IN', { maximumFractionDigits: 2 })
    : '—'
  const atmStrike  = chain?.atm_strike ?? null
  const snapTime   = fmtTime(chain?.snapshot_time)  // IST HH:MM:SS via tokens.fmtTime

  // ── Meta string ──────────────────────────────────────────────────────────

  const metaNode = (
    <span
      className={`mono text-[11px] ${stale ? 'text-amber' : 'text-muted-foreground'}`}
    >
      {`expiry ${expiry} · spot ${spot} · as of ${snapTime}`}
      {stale && ' · stale'}
    </span>
  )

  // ── Empty state ───────────────────────────────────────────────────────────

  if (empty) {
    return (
      <Panel>
        <PanelHeader title="Option Chain — ATM" meta={metaNode} />
        <div className="mono px-4 py-5 text-[10px] text-faint">
          No option-chain snapshot yet
        </div>
      </Panel>
    )
  }

  // ── Table ─────────────────────────────────────────────────────────────────

  return (
    <Panel>
      <PanelHeader title="Option Chain — ATM" meta={metaNode} />
      <div className="overflow-x-auto">
        <div style={{ minWidth: 360 }}>
          <TableHeader />
          {rows.map((row, i) => (
            <ChainRow
              key={row?.strike ?? i}
              row={row}
              isAtm={atmStrike != null && row?.strike === atmStrike}
            />
          ))}
        </div>
      </div>
    </Panel>
  )
}
