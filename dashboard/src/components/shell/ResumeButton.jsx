import { useState } from 'react'
import { Button, Dialog, DialogContent, DialogTitle, DialogBody, DialogFooter } from '@/components/ui'

/**
 * Resume control — shown only while the engine is halted. Opens a confirm
 * dialog, then POSTs /api/resume (auth-gated like the kill switch). The trader
 * re-arms the risk loop within ~10s; the heartbeat then clears `risk.halted`
 * and this button unmounts. A still-breached loss budget re-halts immediately.
 */
export function ResumeButton() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr]   = useState(null)

  async function confirm() {
    setBusy(true)
    setErr(null)
    try {
      const headers = { 'Content-Type': 'application/json' }
      const tok = localStorage.getItem('dashboard_token')
      if (tok) headers['X-Dashboard-Token'] = tok
      const r = await fetch('/api/resume', { method: 'POST', headers, body: '{}' })
      const d = await r.json().catch(() => ({}))
      if (r.ok && d.ok) {
        setOpen(false)   // heartbeat will clear `halted` and unmount this button
      } else if (r.status === 401 || r.status === 403) {
        setErr('Unauthorised — a dashboard token is required for this control.')
      } else {
        setErr(d?.error?.message || `Resume failed (HTTP ${r.status}). Engine is still halted.`)
      }
    } catch (e) {
      setErr(`Network error: ${e.message}. Could not reach the engine.`)
    } finally {
      setBusy(false)
    }
  }

  function openDialog() { setErr(null); setOpen(true) }

  return (
    <>
      <Button variant="profit" onClick={openDialog} className="shrink-0 whitespace-nowrap">
        <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-profit animate-pulse-soft" />
        <span className="sm:hidden">RESUME</span>
        <span className="hidden sm:inline">RESUME TRADING</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Resume trading</DialogTitle>
          <DialogBody>
            This clears the halt and re-arms the risk loop within ~10s. New entries
            become allowed again. If a daily or weekly loss budget is still breached,
            the engine re-halts immediately — by design.
          </DialogBody>
          {err && (
            <div className="mt-3 rounded-[var(--radius-md)] border border-loss/40 bg-loss/[.10] px-3 py-2 text-[11.5px] text-loss" role="alert">
              {err}
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="profit" onClick={confirm} disabled={busy}>
              {busy ? 'Resuming…' : 'Resume trading'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
