import { useCallback, useState } from 'react'

/** Light/dark theme, persisted in localStorage. Default = dark.
 *  The pre-paint script in index.html sets the initial class to avoid a flash. */
export function useTheme() {
  const [theme, setTheme] = useState(() =>
    document.documentElement.classList.contains('dark') ? 'dark' : 'light',
  )

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark'
      document.documentElement.classList.toggle('dark', next === 'dark')
      try { localStorage.setItem('dhan-theme', next) } catch { /* private mode */ }
      return next
    })
  }, [])

  return { theme, toggle }
}
