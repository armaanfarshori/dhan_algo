import { cn } from '@/lib/utils'

/** Underline tab nav (shadcn-style, controlled).
 *  items: [{ value, label }]  ·  value, onValueChange */
export function Tabs({ items, value, onValueChange, className }) {
  const onKeyDown = (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const i = items.findIndex(it => it.value === value)
    const n = items.length
    const next = e.key === 'ArrowRight' ? (i + 1) % n : (i - 1 + n) % n
    onValueChange(items[next].value)
  }
  return (
    <nav
      role="tablist"
      aria-label="Dashboard sections"
      onKeyDown={onKeyDown}
      className={cn('flex h-[46px] items-center gap-[18px] border-b border-border', className)}
    >
      {items.map((it) => {
        const active = it.value === value
        return (
          <button
            key={it.value}
            role="tab"
            id={`tab-${it.value}`}
            aria-selected={active}
            aria-controls={`panel-${it.value}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onValueChange(it.value)}
            className={cn(
              'relative flex h-[46px] cursor-pointer items-center px-1 text-[13px] font-medium transition-colors',
              active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {it.label}
            {active && (
              <span className="absolute inset-x-0 -bottom-px h-0.5 rounded-sm bg-foreground" />
            )}
          </button>
        )
      })}
    </nav>
  )
}
