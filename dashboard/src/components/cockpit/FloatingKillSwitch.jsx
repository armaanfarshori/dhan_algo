import { useState, useEffect } from 'react'
import { T } from '../../tokens'

export default function FloatingKillSwitch({ onKill }) {
  const [armed, setArmed]   = useState(false)
  const [fired, setFired]   = useState(false)
  const [busy, setBusy]     = useState(false)
  const [pulse, setPulse]   = useState(false)

  // Pulse effect when armed
  useEffect(() => {
    if (!armed || fired) return
    const id = setInterval(() => setPulse(p => !p), 500)
    const timeout = setTimeout(() => { setArmed(false); setPulse(false) }, 4000)
    return () => { clearInterval(id); clearTimeout(timeout) }
  }, [armed, fired])

  async function handleClick() {
    if (fired) return
    if (!armed) { setArmed(true); return }
    setBusy(true)
    try {
      const r = await fetch('/api/killswitch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      })
      const d = await r.json()
      if (d.ok) { setFired(true); onKill?.() }
    } finally { setBusy(false) }
  }

  const borderColor = fired ? T.ink3 : T.red
  const bgColor     = fired ? '#111' : armed ? (pulse ? '#3d0f0f' : '#2a0a0a') : '#1a0808'
  const textColor   = fired ? T.ink3 : T.red

  return (
    <div style={{
      position: 'fixed', bottom: 24, right: 24, zIndex: 1000,
    }}>
      {armed && !fired && (
        <div style={{
          fontFamily: T.mono, fontSize: 9, color: T.amber, letterSpacing: '0.16em',
          textTransform: 'uppercase', textAlign: 'center', marginBottom: 6,
          animation: 'pulse 0.5s ease-in-out infinite',
        }}>
          CLICK AGAIN TO CONFIRM
        </div>
      )}
      <button onClick={handleClick} disabled={busy || fired} style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '12px 20px', cursor: fired ? 'default' : 'pointer',
        background: bgColor,
        border: `2px solid ${borderColor}`,
        color: textColor,
        fontFamily: T.mono, fontSize: 11, fontWeight: 600,
        letterSpacing: '0.2em', textTransform: 'uppercase',
        boxShadow: armed && !fired
          ? `0 0 30px oklch(0.68 0.22 25 / 0.5), 0 4px 20px rgba(0,0,0,0.8)`
          : '0 4px 20px rgba(0,0,0,0.6)',
        transition: 'all .15s',
        minWidth: 180,
        justifyContent: 'center',
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: fired ? T.ink3 : T.red,
          boxShadow: !fired ? `0 0 8px ${T.red}` : 'none',
          flexShrink: 0,
          animation: !fired ? 'pulse 1s ease-in-out infinite' : 'none',
        }} />
        {busy ? 'HALTING…' : fired ? 'HALTED' : armed ? 'CONFIRM KILL?' : 'KILL SWITCH'}
      </button>
    </div>
  )
}
