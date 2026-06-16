import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** cn — merge conditional class names, dedup Tailwind conflicts. */
export function cn(...inputs) {
  return twMerge(clsx(inputs))
}
