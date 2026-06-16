import { cn } from '@/lib/utils'

const VARIANTS = {
  default: 'border-border2 text-muted-foreground',
  profit:  'border-profit/30 bg-profit/10 text-profit',
  live:    'border-profit/30 bg-profit/10 text-profit',
  loss:    'border-loss/30 bg-loss/10 text-loss',
  amber:   'border-amber/30 bg-amber/10 text-amber',
  sky:     'border-sky/30 bg-sky/10 text-sky',
}

/** Pill badge. variant: default | profit/live | loss | amber | sky */
export function Badge({ variant = 'default', className, children, ...props }) {
  return (
    <span
      className={cn(
        'inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-full border px-[9px] py-[3px] text-[11px] font-semibold',
        VARIANTS[variant] ?? VARIANTS.default,
        className,
      )}
      {...props}
    >
      {children}
    </span>
  )
}

/** Soft pill chip for stat-card corners (smaller than Badge). tone: up | down | neu */
export function Pill({ tone = 'neu', className, children, ...props }) {
  const tones = {
    up:   'bg-profit/10 text-profit',
    down: 'bg-loss/10 text-loss',
    neu:  'bg-border text-muted-foreground',
  }
  return (
    <span
      className={cn('rounded-full px-2 py-0.5 text-[10.5px] font-semibold', tones[tone] ?? tones.neu, className)}
      {...props}
    >
      {children}
    </span>
  )
}

/** Small square-ish chip used inline (e.g. position/gate tags on cockpit cards). */
export function Tag({ tone = 'default', className, children, ...props }) {
  const tones = {
    default: 'border-border2 text-faint',
    long:    'border-profit/30 bg-profit/[.07] text-profit',
    short:   'border-loss/30 bg-loss/[.07] text-loss',
    gate:    'border-sky/30 bg-sky/[.07] text-sky',
  }
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-[5px] border px-1.5 py-0.5 text-[9.5px] font-semibold',
        tones[tone] ?? tones.default,
        className,
      )}
      {...props}
    >
      {children}
    </span>
  )
}
