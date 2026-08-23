import { useMemo, useCallback, useEffect, useRef } from 'react'
import { useVault, useTabView } from '../state/VaultContext.jsx'
import { makeGroupKey } from '../state/actions.js'
import useResource from './useResource.js'
import useDebounced from './useDebounced.js'
import { humanise } from '../services/format.js'

/*
 * useAssetTab - everything the four tabs do identically: build the query from
 * the view state, load the page, keep the selection, expose the keyboard API
 * to the shell, and turn the filter map into removable chips.
 *
 * What differs per tab (which endpoint, which facets, which empty state) stays
 * in the tab itself.
 */

export function toArray(value) {
  if (value === undefined || value === null || typeof value === 'boolean') return []
  return (Array.isArray(value) ? value : [value]).map(String)
}

export default function useAssetTab(options) {
  const { tab, scope, loader, onStatus, onOpen, registerApi, cacheKey } = options
  const { state } = useVault()
  const { view, patch, setFilter, clearFilters } = useTabView(tab)
  const scrollRef = useRef(null)
  const debouncedQuery = useDebounced(view.q, 140)

  const query = useMemo(() => ({
    q: debouncedQuery || undefined,
    smart: debouncedQuery && view.smart ? true : undefined,
    sort: view.sort,
    group: view.group !== 'none' ? view.group : undefined,
    limit: view.limit,
    offset: view.offset,
    ...view.filters
  }), [debouncedQuery, view.smart, view.sort, view.group, view.limit, view.offset, view.filters])

  const list = useResource(
    (cacheKey || scope) + ':' + JSON.stringify(query),
    (signal) => loader(query, signal),
    { epoch: state.dataEpoch }
  )

  const items = useMemo(() => (list.data && list.data.items) || [], [list.data])
  const groups = (list.data && list.data.groups) || []
  const page = list.data && list.data.page
  const meta = list.data && list.data.meta

  const selection = useMemo(() => new Set(view.selection), [view.selection])
  const groupKeyOf = useMemo(() => makeGroupKey(scope, view.group), [scope, view.group])

  useEffect(() => {
    onStatus({ page, elapsed: meta && meta.elapsed_ms, mode: meta && meta.mode })
  }, [page, meta, onStatus])

  /* -------------------------------------------------------------- selection */
  const select = useCallback((item, event) => {
    if (event && (event.ctrlKey || event.metaKey)) {
      const next = view.selection.includes(item.uid)
        ? view.selection.filter((u) => u !== item.uid)
        : [...view.selection, item.uid]
      patch({ selection: next, focusUid: item.uid, detailUid: item.uid }, { keepOffset: true })
      return
    }
    if (event && event.shiftKey && view.focusUid) {
      const from = items.findIndex((i) => i.uid === view.focusUid)
      const to = items.findIndex((i) => i.uid === item.uid)
      if (from >= 0 && to >= 0) {
        const lo = Math.min(from, to)
        const hi = Math.max(from, to)
        patch({
          selection: items.slice(lo, hi + 1).map((i) => i.uid),
          focusUid: item.uid,
          detailUid: item.uid
        }, { keepOffset: true })
        return
      }
    }
    patch({ selection: [item.uid], focusUid: item.uid, detailUid: item.uid }, { keepOffset: true })
  }, [view.selection, view.focusUid, items, patch])

  const toggleCheck = useCallback((item) => {
    const next = view.selection.includes(item.uid)
      ? view.selection.filter((u) => u !== item.uid)
      : [...view.selection, item.uid]
    patch({ selection: next }, { keepOffset: true })
  }, [view.selection, patch])

  const selectedItems = useMemo(
    () => items.filter((i) => selection.has(i.uid)), [items, selection]
  )

  /* -------------------------------------------------- keyboard bridge */
  const openRef = useRef(onOpen)
  openRef.current = onOpen
  const requestOpRef = useRef(null)

  const setRequestOp = useCallback((fn) => { requestOpRef.current = fn }, [])

  useEffect(() => {
    registerApi({
      items,
      selectAll: () => patch({ selection: items.map((i) => i.uid) }, { keepOffset: true }),
      clearSelection: () => patch({ selection: [], focusUid: null }, { keepOffset: true }),
      requestOp: (kind, uids) => {
        if (requestOpRef.current) requestOpRef.current(kind, uids)
      },
      openFocused: () => {
        const focused = items.find((i) => i.uid === view.focusUid) || items[0]
        if (focused && openRef.current) openRef.current(focused)
      },
      move: (delta) => {
        // The true column count comes from the rendered grid track, so an
        // up/down arrow always moves exactly one visual row.
        let columns = 1
        const row = scrollRef.current && scrollRef.current.querySelector('.gp-vgrid__row')
        if (row && view.view !== 'list') {
          columns = Math.max(1,
            window.getComputedStyle(row).gridTemplateColumns.split(' ').length)
        }
        const idx = items.findIndex((i) => i.uid === view.focusUid)
        const step = Array.isArray(delta)
          ? (delta[1] !== 0 ? delta[1] * columns : delta[0])
          : 0
        let next = idx < 0 ? 0 : idx + step
        if (delta === 'home') next = 0
        if (delta === 'end') next = items.length - 1
        next = Math.max(0, Math.min(items.length - 1, next))
        const target = items[next]
        if (target) {
          patch({ focusUid: target.uid, detailUid: target.uid, selection: [target.uid] },
            { keepOffset: true })
        }
      },
      refresh: list.refresh
    })
  }, [items, view.focusUid, view.view, patch, registerApi, list.refresh])

  /* ------------------------------------------------------- active filters */
  const activeFilters = useMemo(() => {
    const out = []
    for (const [field, value] of Object.entries(view.filters)) {
      if (typeof value === 'boolean') {
        out.push({
          field,
          value: String(value),
          label: humanise(field) + ': ' + (value ? 'yes' : 'no'),
          remove: null
        })
        continue
      }
      for (const v of toArray(value)) {
        out.push({
          field,
          value: v,
          label: humanise(field) + ': ' + v,
          remove: toArray(value).filter((x) => x !== v)
        })
      }
    }
    return out
  }, [view.filters])

  /** Turn a value list into toolbar chips bound to one repeatable filter. */
  const buildFacet = useCallback((field, label, values, limit) => ({
    field,
    label,
    values: (values || [])
      .filter((v) => v.count === undefined || v.count > 0)
      .slice(0, limit || 8)
      .map((v) => ({
        value: v.value,
        label: v.label || String(v.value),
        count: v.count,
        selected: toArray(view.filters[field]).includes(String(v.value))
      })),
    onToggle: (v) => {
      const current = toArray(view.filters[field])
      const key = String(v.value)
      setFilter(field, current.includes(key)
        ? current.filter((x) => x !== key)
        : [...current, key])
    }
  }), [view.filters, setFilter])

  /** Chips for a boolean filter, which toggles rather than accumulating. */
  const buildBoolFacet = useCallback((field, label, choices) => ({
    field,
    label,
    values: choices.map((c) => ({
      value: String(c.value),
      label: c.label,
      count: c.count,
      selected: view.filters[field] === c.value
    })),
    onToggle: (v) => {
      const want = v.value === 'true'
      setFilter(field, view.filters[field] === want ? null : want)
    }
  }), [view.filters, setFilter])

  return {
    view,
    patch,
    setFilter,
    clearFilters,
    scrollRef,
    list,
    items,
    groups,
    page,
    meta,
    selection,
    selectedItems,
    groupKeyOf,
    select,
    toggleCheck,
    activeFilters,
    buildFacet,
    buildBoolFacet,
    setRequestOp
  }
}
