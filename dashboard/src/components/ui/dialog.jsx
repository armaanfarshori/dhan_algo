import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'

/** Lightweight modal (no Radix). Controlled via open / onOpenChange.
 *  Traps Tab focus inside the dialog, focuses it on open, restores focus on close. */
export function Dialog({ open, onOpenChange, children }) {
  const panelRef = useRef(null)
  const restoreRef = useRef(null)

  useEffect(() => {
    if (!open) return
    restoreRef.current = document.activeElement
    const node = panelRef.current
    const focusables = () =>
      node ? Array.from(node.querySelectorAll(
        'button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])',
      )).filter(el => !el.disabled) : []
    ;(focusables()[0] || node)?.focus?.()

    const onKey = (e) => {
      if (e.key === 'Escape') { onOpenChange(false); return }
      if (e.key !== 'Tab') return
      const f = focusables()
      if (f.length === 0) { e.preventDefault(); return }
      const i = f.indexOf(document.activeElement)
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus() }
      else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus() }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      restoreRef.current?.focus?.()
    }
  }, [open, onOpenChange])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/55 backdrop-blur-[2px]"
      onClick={() => onOpenChange(false)}
    >
      <div ref={panelRef} tabIndex={-1} onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>
  )
}

export function DialogContent({ className, children }) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="tessera-dialog-title"
      className={cn(
        'w-[380px] max-w-[92vw] rounded-[var(--radius-lg)] border border-border bg-card p-5 shadow-2xl',
        className,
      )}
    >
      {children}
    </div>
  )
}

export function DialogTitle({ className, children }) {
  return <h2 id="tessera-dialog-title" className={cn('text-[15px] font-semibold text-foreground', className)}>{children}</h2>
}

export function DialogBody({ className, children }) {
  return <div className={cn('mt-2 text-[12.5px] leading-relaxed text-muted-foreground', className)}>{children}</div>
}

export function DialogFooter({ className, children }) {
  return <div className={cn('mt-5 flex justify-end gap-2', className)}>{children}</div>
}
