import { useState, useEffect } from 'react'

export function usePoller(url, interval = 5000) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)

  useEffect(() => {
    let cancelled = false
    let controller = null

    const run = async () => {
      controller?.abort()           // cancel any still-in-flight request
      controller = new AbortController()
      try {
        const r = await fetch(url, { signal: controller.signal })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const json = await r.json()
        if (cancelled) return
        setData(json)
        setError(null)
      } catch (e) {
        if (cancelled || e.name === 'AbortError') return
        // keep the last good data, but surface the error so consumers can
        // flag the panel as stale rather than silently showing old numbers
        setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    run()
    // Pause while the tab is hidden; refresh immediately on return.
    const id = setInterval(() => { if (!document.hidden) run() }, interval)
    const onVis = () => { if (!document.hidden) run() }
    document.addEventListener('visibilitychange', onVis)

    return () => {
      cancelled = true
      controller?.abort()
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [url, interval])

  return { data, loading, error }
}
