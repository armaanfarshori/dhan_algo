import { cn } from '@/lib/utils'

const VARIANTS = {
  default:    'border border-border2 bg-card text-foreground hover:border-faint',
  ghost:      'text-muted-foreground hover:text-foreground hover:bg-panel',
  destructive:'border border-loss/35 bg-loss/[.08] text-loss hover:bg-loss/[.14]',
  profit:     'border border-profit/35 bg-profit/[.08] text-profit hover:bg-profit/[.14]',
  outline:    'border border-border2 text-muted-foreground hover:text-foreground',
}

const SIZES = {
  default: 'h-9 px-[13px] text-[12px] gap-[7px] rounded-[var(--radius-md)]',
  sm:      'h-8 px-2.5 text-[11px] gap-1.5 rounded-[var(--radius-sm)]',
  icon:    'h-[34px] w-[34px] rounded-[var(--radius-md)]',
}

export function Button({ variant = 'default', size = 'default', className, ...props }) {
  return (
    <button
      className={cn(
        'inline-flex items-center justify-center font-semibold transition-colors outline-none focus-visible:ring-2 focus-visible:ring-sky/40 disabled:opacity-50',
        VARIANTS[variant] ?? VARIANTS.default,
        SIZES[size] ?? SIZES.default,
        className,
      )}
      {...props}
    />
  )
}
