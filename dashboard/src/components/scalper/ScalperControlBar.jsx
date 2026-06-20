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
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="profit" size="sm"
            disabled={busy != null}
            onClick={() => send('start')}
          >
            {busy === 'start' ? 'Starting…' : 'Start'}
          </Button>
          <Button
            variant="destructive" size="sm"
            disabled={busy != null}
            onClick={() => send('stop')}
          >
            {busy === 'stop' ? 'Stopping…' : 'Stop'}
          </Button>
        </div>
      </div>

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
