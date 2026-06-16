import { fmtTime } from '@/tokens'

/** Full-width alarm strip when the engine or the API itself is gone. */
export function OfflineBanner({ data }) {
  const apiDown = !!data.snapshot.error
  const stale = !apiDown && data.snapshot.data != null && !data.alive
  if (!apiDown && !stale) return null
  const msg = apiDown
    ? 'Dashboard API unreachable — data frozen · check dhan-api service'
    : `Trading engine offline — last heartbeat ${fmtTime(data.trader?.ts)} · positions are NOT being managed`
  return (
    <div className="flex items-center gap-2.5 border-b border-loss/40 bg-loss/[.12] px-6 py-2">
      <span className="text-[11px] font-bold text-loss">⛔</span>
      <span className="mono text-[10.5px] tracking-wide text-loss">{msg}</span>
    </div>
  )
}
