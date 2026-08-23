import React, {
  createContext, useContext, useReducer, useMemo, useCallback, useEffect
} from 'react'
import api, { isAbort } from '../services/api.js'
import { reducer, initialState, TAB_IDS } from './actions.js'

const VaultContext = createContext(null)

/** Read the current tab from the URL hash, e.g. "#/outputs". */
function tabFromHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').split('?')[0]
  return TAB_IDS.includes(raw) ? raw : null
}

export function VaultProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState, (base) => ({
    ...base,
    tab: tabFromHash() || base.tab
  }))

  /* ---- boot: info + config + stats, with a ping retry while the API warms up.
     No mounted-once guard here on purpose: React's development double-invoke
     aborts the first attempt, and a guard would leave the app on the splash. */
  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false

    async function boot() {
      for (let attempt = 0; attempt < 12 && !cancelled; attempt += 1) {
        try {
          await api.ping(controller.signal)
          break
        } catch (err) {
          if (isAbort(err) || cancelled) return
          if (attempt === 11) {
            dispatch({ type: 'boot-error', error: err })
            return
          }
          await new Promise((r) => setTimeout(r, 500))
        }
      }
      if (cancelled) return
      try {
        const [info, config, stats] = await Promise.all([
          api.info(controller.signal),
          api.config(controller.signal),
          api.stats(controller.signal).catch(() => null)
        ])
        if (!cancelled) dispatch({ type: 'boot', info, config, stats })
      } catch (err) {
        if (!isAbort(err) && !cancelled) dispatch({ type: 'boot-error', error: err })
      }
    }
    boot()
    return () => { cancelled = true; controller.abort() }
  }, [])

  /* ---- keep the URL hash and the active tab in step (both directions) ---- */
  useEffect(() => {
    const onHash = () => {
      const tab = tabFromHash()
      if (tab) dispatch({ type: 'set-tab', tab })
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const desired = '#/' + state.tab
    if (window.location.hash !== desired) {
      window.history.replaceState(null, '', desired)
    }
  }, [state.tab])

  /* ---------------------------------------------------------- helpers ---- */

  const toast = useCallback((payload) => {
    dispatch({ type: 'toast', toast: payload })
  }, [])

  const toastError = useCallback((err, title) => {
    if (isAbort(err)) return
    dispatch({
      type: 'toast',
      toast: {
        tone: 'danger',
        title: title || 'Action failed',
        message: err && err.message ? err.message : 'Unexpected error',
        detail: err && err.requestId ? 'request ' + err.requestId : null
      }
    })
  }, [])

  const refreshStats = useCallback(async () => {
    try {
      const stats = await api.stats()
      dispatch({ type: 'set-stats', stats })
    } catch (err) { /* status bar keeps its previous numbers */ }
  }, [])

  const refreshConfig = useCallback(async () => {
    const config = await api.config()
    dispatch({ type: 'set-config', config })
    return config
  }, [])

  /** Force every useResource subscriber to refetch. */
  const invalidate = useCallback(() => {
    dispatch({ type: 'invalidate' })
    refreshStats()
  }, [refreshStats])

  const value = useMemo(() => ({
    state,
    dispatch,
    view: state.views[state.tab] || state.views.models,
    toast,
    toastError,
    refreshStats,
    refreshConfig,
    invalidate
  }), [state, toast, toastError, refreshStats, refreshConfig, invalidate])

  return <VaultContext.Provider value={value}>{children}</VaultContext.Provider>
}

export function useVault() {
  const ctx = useContext(VaultContext)
  if (!ctx) throw new Error('useVault must be used inside VaultProvider')
  return ctx
}

/** Convenience: the view state for one tab plus a patcher bound to it. */
export function useTabView(tab) {
  const { state, dispatch } = useVault()
  const view = state.views[tab]
  const patch = useCallback((next, opts) => {
    dispatch({ type: 'patch-view', tab, patch: next, resetOffset: opts && opts.keepOffset ? false : undefined })
  }, [dispatch, tab])
  const setFilter = useCallback((field, value) => {
    dispatch({ type: 'set-filter', tab, field, value })
  }, [dispatch, tab])
  const clearFilters = useCallback(() => {
    dispatch({ type: 'clear-filters', tab })
  }, [dispatch, tab])
  return { view, patch, setFilter, clearFilters }
}
