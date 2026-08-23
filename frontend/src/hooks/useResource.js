import { useEffect, useRef, useState, useCallback } from 'react'
import { isAbort } from '../services/api.js'

/*
 * useResource - fetch + cache + abort + stale-while-revalidate.
 *
 * Keeping the previous payload visible while a new one loads is what stops the
 * grid from flashing empty on every keystroke, sort change or page step.
 */

const cache = new Map()
const MAX_ENTRIES = 120

function remember(key, value) {
  if (cache.has(key)) cache.delete(key)
  cache.set(key, value)
  while (cache.size > MAX_ENTRIES) {
    cache.delete(cache.keys().next().value)
  }
}

export function clearResourceCache() {
  cache.clear()
}

/**
 * @param {string|null} key   cache identity; null disables the request
 * @param {(signal: AbortSignal) => Promise<any>} loader
 * @param {{epoch?: number, keepPrevious?: boolean}} options
 */
export default function useResource(key, loader, options) {
  const opts = options || {}
  const epoch = opts.epoch || 0
  const keepPrevious = opts.keepPrevious !== false
  const cacheKey = key === null || key === undefined ? null : key + '|' + epoch

  const [state, setState] = useState(() => ({
    data: cacheKey && cache.has(cacheKey) ? cache.get(cacheKey) : null,
    error: null,
    loading: Boolean(cacheKey)
  }))

  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const [nonce, setNonce] = useState(0)

  const refresh = useCallback(() => {
    if (cacheKey) cache.delete(cacheKey)
    setNonce((n) => n + 1)
  }, [cacheKey])

  useEffect(() => {
    if (!cacheKey) {
      setState({ data: null, error: null, loading: false })
      return undefined
    }
    const cached = cache.get(cacheKey)
    if (cached !== undefined) {
      setState({ data: cached, error: null, loading: false })
      return undefined
    }
    const controller = new AbortController()
    let alive = true
    setState((prev) => ({
      data: keepPrevious ? prev.data : null,
      error: null,
      loading: true
    }))
    loaderRef.current(controller.signal)
      .then((data) => {
        if (!alive) return
        remember(cacheKey, data)
        setState({ data, error: null, loading: false })
      })
      .catch((err) => {
        if (!alive || isAbort(err)) return
        setState({ data: null, error: err, loading: false })
      })
    return () => { alive = false; controller.abort() }
  }, [cacheKey, nonce, keepPrevious])

  return { ...state, refresh }
}
