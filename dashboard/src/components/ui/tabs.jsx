import { cn } from '@/lib/utils'

/** Underline tab nav (shadcn-style, controlled).
 *  items: [{ value, label }]  ·  value, onValueChange */
export function Tabs({ items, value, onValueChange, className }) {
  return (
    <nav
      role="tablist"
      className={cn('flex h-[46px] items-center gap-[18px] border-b border-border', className)}
    >
      {items.map((it) => {
        const active = it.value === value
        return (
          <button
            key={it.value}
            role="tab"
            aria-selected={active}
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
