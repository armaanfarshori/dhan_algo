import { useState } from 'react'
import { Button, Dialog, DialogContent, DialogTitle, DialogBody, DialogFooter } from '@/components/ui'

/** Top-bar kill switch — opens a confirm dialog, then POSTs /api/killswitch. */
export function KillSwitch() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [fired, setFired] = useState(false)

  async function confirm() {
    setBusy(true)
    try {
      const headers = { 'Content-Type': 'application/json' }
      const tok = localStorage.getItem('dashboard_token')
      if (tok) headers['X-Dashboard-Token'] = tok
      const r = await fetch('/api/killswitch', { method: 'POST', headers, body: '{}' })
      const d = await r.json().catch(() => ({}))
      if (d.ok) setFired(true)
    } finally {
      setBusy(false)
      setOpen(false)
    }
  }

  return (
    <>
      <Button variant="destructive" onClick={() => setOpen(true)} disabled={fired}>
        <span className={`mr-1.5 inline-block h-2 w-2 rounded-full bg-loss ${fired ? '' : 'animate-pulse-soft'}`} />
        {fired ? 'HALTED' : 'KILL SWITCH'}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>Confirm kill switch</DialogTitle>
          <DialogBody>
            This halts the risk loop and flattens all open positions within ~10s.
            It cannot be undone from the dashboard.
          </DialogBody>
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
