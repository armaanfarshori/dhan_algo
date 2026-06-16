import { cn } from '@/lib/utils'
import { Card, Label } from './card'
import { Progress } from './progress'

/** KPI stat card: label + optional right chip, big mono value, sub line, optional bar. */
export function StatCard({ label, value, valueClassName, right, sub, bar, className }) {
  return (
    <Card className={cn('relative flex flex-col gap-[9px] overflow-hidden px-[17px] py-4', className)}>
      <div className="flex items-center justify-between">
        <Label>{label}</Label>
        {right}
      </div>
      <div className={cn('mono text-[27px] font-semibold tracking-[-.03em] leading-none', valueClassName)}>
        {value}
      </div>
      {sub != null && <div className="text-[11.5px] text-muted-foreground">{sub}</div>}
      {bar && <Progress value={bar.value} color={bar.color} className="mt-0.5" />}
    </Card>
  )
}
