import { cn } from '@/lib/utils'
import { Card, Label } from './card'
import { Progress } from './progress'

/** KPI stat card: label + optional right chip, big mono value, sub line, optional bar. */
export function StatCard({ label, value, valueClassName, right, sub, bar, className }) {
  return (
    <Card className={cn('relative flex flex-col gap-[9px] px-4 py-3', className)}>
      <div className="flex items-center justify-between gap-2">
        <Label className="truncate">{label}</Label>
        {right && <div className="shrink-0">{right}</div>}
      </div>
      {/* Geist Mono (tabular) — matches the approved mockup's big numbers */}
      <div className={cn('mono text-[27px] font-semibold leading-none tracking-[-.03em]', valueClassName)}>
        {value}
      </div>
      {sub != null && <div className="text-[11.5px] text-muted-foreground">{sub}</div>}
      {bar && <Progress value={bar.value} color={bar.color} className="mt-0.5" />}
    </Card>
  )
}
