import { cn } from '@/lib/utils'

/** Generic surface card (KPI stats, etc.) */
export function Card({ className, ...props }) {
  return (
    <div
      className={cn(
        'rounded-[var(--radius-lg)] border border-border bg-card shadow-[var(--shadow)]',
        className,
      )}
      {...props}
    />
  )
}

/** Panel = card with a header strip + body, for lists/feeds/tables. */
export function Panel({ className, ...props }) {
  return (
    <div
      className={cn(
        'overflow-hidden rounded-[var(--radius-lg)] border border-border bg-card shadow-[var(--shadow)]',
        className,
      )}
      {...props}
    />
  )
}

/** Panel header: title (left) + optional meta (right). */
export function PanelHeader({ title, meta, icon, className }) {
  return (
    <div
      className={cn(
        'flex items-center justify-between border-b border-border px-4 py-[13px]',
        className,
      )}
    >
      <h3 className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
        {icon}
        {title}
      </h3>
      {meta != null && (
        <span className="text-[11px] text-muted-foreground">{meta}</span>
      )}
    </div>
  )
}

/** Uppercase micro-label used inside stat cards / panels. */
export function Label({ className, ...props }) {
  return (
    <span
      className={cn(
        'text-[10.5px] font-semibold uppercase tracking-[.09em] text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
}
