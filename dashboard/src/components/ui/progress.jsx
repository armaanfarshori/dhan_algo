import { cn } from '@/lib/utils'

/** Thin progress bar. value 0–100. color = any CSS color (default sky token). */
export function Progress({ value = 0, color, className, trackClassName }) {
  const pct = Math.max(0, Math.min(100, value))
  return (
    <div className={cn('h-1 overflow-hidden rounded-[3px] bg-border', trackClassName, className)}>
      <div
        className="h-full rounded-[3px] transition-[width] duration-500"
        style={{ width: `${pct}%`, background: color ?? 'hsl(var(--sky))' }}
      />
    </div>
  )
}
