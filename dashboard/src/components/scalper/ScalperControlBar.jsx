import { useState } from 'react'
import { Panel, Button, Badge, Pill } from '@/components/ui'

/**
 * Scalper control bar — start / stop the scalper via POST /api/scalper/control.
 *
 * Mirrors the KillSwitch POST convention: sends X-Dashboard-Token from
 * localStorage when present; surfaces 401 distinctly so the operator knows a
 * token is required. PAPER-only; the trader consumes the run/ flag within ~10s.
 *
 * Prop: live = /api/scalper/live body (for the running/off pill).
 */
export default function ScalperControlBar({ live }) {
  const [busy, setBusy] = useState(null)   // 'start' | 'stop' | null
  const [msg, setMsg]   = useState(null)
  const [err, setErr]   = useState(null)

  const running = !!(live && live.available)
  // scalper_enabled from GET /api/scalper/live. With the flag off the trader
  // builds no orchestrator, so Start would write run/scalper_start and have it
  // silently ignored — disable the button instead of faking a control surface.
  // `undefined` (older API build) → leave enabled rather than lock the operator out.
  const featureOff = live?.enabled === false

  async function send(action) {
    setBusy(action)
    setErr(null)
    setMsg(null)
    try {
      const headers = { 'Content-Type': 'application/json' }
      const tok = localStorage.getItem('dashboard_token')
      if (tok) headers['X-Dashboard-Token'] = tok
      const r = await fetch('/api/scalper/control', {
        method: 'POST', headers, body: JSON.stringify({ action }),
      })
      const d = await r.json().catch(() => ({}))
      if (r.ok && d.ok) {
        setMsg(d.message || `Scalper ${action} flag set.`)
      } else if (r.status === 401 || r.status === 403) {
        setErr('Unauthorised — a dashboard token is required for this control.')
      } else {
        setErr(d?.error || `Scalper ${action} failed (HTTP ${r.status}).`)
      }
    } catch (e) {
      setErr(`Network error: ${e.message}.`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel className="mb-3.5">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-semibold text-foreground">Scalper Control</span>
          {running
            ? <Badge variant="profit">RUNNING</Badge>
            : <Badge variant="default">OFF</Badge>}
          <Pill tone="neu">PAPER</Pill>
          {featureOff && <Pill tone="neu">flag off</Pill>}
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="profit" size="sm"
            disabled={busy != null || featureOff}
            title={featureOff
              ? 'scalper_enabled=false on the trader — set it in .env and restart dhan-trader before Start can do anything'
              : undefined}
            onClick={() => send('start')}
          >
            {busy === 'start' ? 'Starting…' : 'Start'}
          </Button>
          {/* Stop is never gated: it must stay reachable whatever the flag says. */}
          <Button
            variant="destructive" size="sm"
            disabled={busy != null}
            onClick={() => send('stop')}
          >
            {busy === 'stop' ? 'Stopping…' : 'Stop'}
          </Button>
        </div>
      </div>

      {featureOff && (
        <div className="mono border-t border-border px-4 py-2 text-[10px] leading-relaxed text-faint">
          Start disabled — <span className="text-amber">scalper_enabled=false</span> on the
          trader (ships dark). Set it in <span className="text-amber">.env</span> and restart
          dhan-trader; PAPER-only either way.
        </div>
      )}

      {(msg || err) && (
        <div className="border-t border-border px-4 py-2">
          {err && (
            <div className="rounded-[var(--radius-md)] border border-loss/40 bg-loss/[.10] px-3 py-2 text-[11.5px] text-loss" role="alert">
              {err}
            </div>
          )}
          {msg && !err && (
            <div className="mono text-[11px] text-muted-foreground">{msg}</div>
          )}
        </div>
      )}
    </Panel>
  )
}
