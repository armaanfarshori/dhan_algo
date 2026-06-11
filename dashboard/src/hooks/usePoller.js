import { useState, useEffect, useRef } from 'react'

export function usePoller(url, interval = 5000) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)
  const fetchFn = useRef(null)

  fetchFn.current = async () => {
    try {
      const r = await fetch(url)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json())
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchFn.current()
    // Pause while the tab is hidden — a backgrounded dashboard shouldn't
    // keep hammering the API; refresh immediately on return.
    const id = setInterval(() => { if (!document.hidden) fetchFn.current() }, interval)
    const onVis = () => { if (!document.hidden) fetchFn.current() }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearInterval(id); document.removeEventListener('visibilitychange', onVis) }
  }, [url, interval])

  return { data, loading, error }
}
