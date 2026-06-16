import { useEffect } from 'react'
import { cn } from '@/lib/utils'

/** Lightweight modal (no Radix). Controlled via open / onOpenChange. */
export function Dialog({ open, onOpenChange, children }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') onOpenChange(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onOpenChange])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/55 backdrop-blur-[2px]"
      onClick={() => onOpenChange(false)}
    >
      <div onClick={(e) => e.stopPropagation()}>{children}</div>
    </div>
  )
}

export function DialogContent({ className, children }) {
  return (
    <div
      role="dialog"
      aria-modal="true"
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
  return <h2 className={cn('text-[15px] font-semibold text-foreground', className)}>{children}</h2>
}

export function DialogBody({ className, children }) {
  return <div className={cn('mt-2 text-[12.5px] leading-relaxed text-muted-foreground', className)}>{children}</div>
}

export function DialogFooter({ className, children }) {
  return <div className={cn('mt-5 flex justify-end gap-2', className)}>{children}</div>
}
