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
      applyTheme(next)
      try { localStorage.setItem('tessera-theme', next) } catch { /* private mode */ }
      return next
    })
  }, [])

  return { theme, toggle }
}

/**
 * Flip the `.dark` class with CSS transitions suppressed for exactly one frame.
 *
 * WHY (verified in Chrome, both directions): every theme colour resolves through
 * `hsl(var(--foreground))` etc., and those custom properties are redefined on
 * <html>. Blink does NOT start a transition when a transitioned property's value
 * changes only because an INHERITED custom property changed on an ancestor — the
 * element keeps its pre-change computed colour until something else forces a
 * style recalc on it. Any element carrying `transition-colors` therefore strands
 * the *previous* theme's colour.
 *
 * It showed up on the active tab because <Tabs> renders identical className
 * strings on every poll, so React never mutates that DOM (no recalc), and the
 * active label is the one element painted in `--foreground` — near-black vs
 * near-white, i.e. white-on-white after switching to light. Clicking another tab
 * changed the classNames, forcing the recalc that "fixed" it.
 *
 * Killing transitions for the swap makes the new values apply instantly (the
 * no-transition path was always correct), then restores them for hover states.
 */
export function applyTheme(next) {
  const root = document.documentElement
  root.classList.add('theme-switching')
  root.classList.toggle('dark', next === 'dark')
  // Force a synchronous style/layout flush so the new colours are committed
  // while transitions are still off. Without this the class removal below
  // lands in the same frame and the transition (and the bug) comes back.
  void root.offsetHeight
  root.classList.remove('theme-switching')
}
