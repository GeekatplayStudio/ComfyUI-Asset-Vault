import { useEffect, useState } from 'react'

/**
 * Delay a value. Search uses 140 ms so a fast typist issues one request, not ten.
 */
export default function useDebounced(value, delay) {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay === undefined ? 140 : delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return settled
}
