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
    const id = setInterval(() => fetchFn.current(), interval)
    return () => clearInterval(id)
  }, [url, interval])

  return { data, loading, error }
}
