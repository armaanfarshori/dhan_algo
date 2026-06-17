import { useState } from 'react'
import { Button, Dialog, DialogContent, DialogTitle, DialogBody, DialogFooter } from '@/components/ui'

/** Top-bar kill switch — opens a confirm dialog, then POSTs /api/killswitch. */
export function KillSwitch() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [fired, setFired] = useState(false)
  const [err, setErr]   = useState(null)

  async function confirm() {
    setBusy(true)
    setErr(null)
    try {
      const headers = { 'Content-Type': 'application/json' }
      const tok = localStorage.getItem('dashboard_token')
      if (tok) headers['X-Dashboard-Token'] = tok
      const r = await fetch('/api/killswitch', { method: 'POST', headers, body: '{}' })
      const d = await r.json().catch(() => ({}))
      if (r.ok && d.ok) {
        setFired(true)
        setOpen(false)
      } else if (r.status === 401 || r.status === 403) {
        setErr('Unauthorised — a dashboard token is required for this control.')
      } else {
        // NEVER close silently on failure: the operator must know the kill
        // did not take effect (positions may still be open).
        setErr(d?.error?.message || `Kill switch failed (HTTP ${r.status}). Positions may NOT be halted.`)
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
      <Button variant="destructive" onClick={openDialog} disabled={fired} className="shrink-0 whitespace-nowrap">
        <span className={`mr-1.5 inline-block h-2 w-2 rounded-full bg-loss ${fired ? '' : 'animate-pulse-soft'}`} />
        {fired ? 'HALTED' : (
          <>
            <span className="sm:hidden">KILL</span>
            <span className="hidden sm:inline">KILL SWITCH</span>
          </>
        )}
      </Button>
      <Dialog open={open} onOpenChange={(v) => { if (!busy) setOpen(v) }}>
        <DialogContent>
          <DialogTitle>Confirm kill switch</DialogTitle>
          <DialogBody>
            This halts the risk loop and flattens all open positions within ~10s.
            You can re-arm afterwards with the Resume control.
          </DialogBody>
          {err && (
            <div className="mt-3 rounded-[var(--radius-md)] border border-loss/40 bg-loss/[.10] px-3 py-2 text-[11.5px] text-loss" role="alert">
              {err}
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={confirm} disabled={busy}>
              {busy ? 'Halting…' : 'Halt & flatten'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
