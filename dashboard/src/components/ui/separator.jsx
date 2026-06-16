import { cn } from '@/lib/utils'

export function Separator({ orientation = 'horizontal', className }) {
  return (
    <div
      role="separator"
      className={cn(
        'shrink-0 bg-border',
        orientation === 'vertical' ? 'h-[26px] w-px' : 'h-px w-full',
        className,
      )}
    />
  )
}
