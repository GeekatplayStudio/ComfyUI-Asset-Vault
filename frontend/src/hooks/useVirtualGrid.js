import { useState, useLayoutEffect, useRef, useMemo, useCallback } from 'react'

/*
 * useVirtualGrid - hand-rolled windowing for the asset grid and list.
 *
 * The page is laid out as sections: an optional group header in normal flow,
 * followed by a spacer of the section's full height inside which only the rows
 * that intersect the viewport are absolutely positioned.
 *
 * Group headers stay in normal flow on purpose: their height is then the
 * browser's problem, not an estimate this hook has to get right. Row pitch is
 * measured from the DOM and only ever grows, so a title that wraps to a second
 * line can never make two rows overlap.
 *
 * The mounted-item count is hard-capped so the <=150 budget holds at every
 * zoom level and every page size.
 */

export const MAX_MOUNTED = 150
const OVERSCAN_ROWS = 2

/** Observe an element's content-box width. */
export function useElementWidth(ref) {
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    // clientWidth INCLUDES padding, but the grid tracks are laid out in the
    // content box.  The scroll container is padded in grid mode and flush in
    // list mode, so measuring clientWidth made the JS column count disagree
    // with CSS auto-fill by one column exactly when switching to grid --
    // the extra card then wrapped into the absolutely positioned row below.
    const update = () => {
      const cs = getComputedStyle(el)
      const pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0)
      setWidth(Math.max(0, el.clientWidth - pad))
    }
    update()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update)
      return () => window.removeEventListener('resize', update)
    }
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return width
}

/** Track a scroll container's scrollTop and viewport height, rAF-throttled. */
function useScrollMetrics(ref) {
  const [metrics, setMetrics] = useState({ top: 0, height: 0 })
  const frame = useRef(0)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const read = () => {
      frame.current = 0
      const top = el.scrollTop
      const height = el.clientHeight
      setMetrics((prev) => (
        prev.top === top && prev.height === height ? prev : { top, height }
      ))
    }
    const onScroll = () => {
      if (frame.current) return
      frame.current = requestAnimationFrame(read)
    }
    read()
    el.addEventListener('scroll', onScroll, { passive: true })
    let ro = null
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(read)
      ro.observe(el)
    }
    return () => {
      el.removeEventListener('scroll', onScroll)
      if (ro) ro.disconnect()
      if (frame.current) cancelAnimationFrame(frame.current)
    }
  }, [ref])

  return metrics
}

/**
 * @param {object} opts
 *   scrollRef   the scrolling element ref
 *   items       the loaded page
 *   groups      groups[] from the list response (ordering hint + totals)
 *   groupKeyOf  (item) => group key, or null when ungrouped
 *   mode        'grid' | 'list'
 *   tile        tile size in px (grid only)
 *   gap         grid gap in px
 */
export default function useVirtualGrid(opts) {
  const { scrollRef, items, groups, groupKeyOf, mode, tile, gap = 12 } = opts

  const width = useElementWidth(scrollRef)
  const { top: scrollTop, height: viewport } = useScrollMetrics(scrollRef)

  const columns = mode === 'list'
    ? 1
    : Math.max(1, Math.floor((width + gap) / (tile + gap)))
  const columnWidth = mode === 'list'
    ? width
    : Math.max(tile, Math.floor((width - gap * (columns - 1)) / columns))

  const estimatedRow = mode === 'list' ? 34 : columnWidth + 66
  const [measuredRow, setMeasuredRow] = useState(0)

  // A change of mode or column width invalidates the measurement.
  const geometryKey = mode + ':' + columnWidth
  const geometryRef = useRef(geometryKey)
  if (geometryRef.current !== geometryKey) {
    geometryRef.current = geometryKey
    if (measuredRow !== 0) setMeasuredRow(0)
  }

  const rowPitch = (measuredRow || estimatedRow) + (mode === 'list' ? 0 : gap)
  const headerEstimate = 34

  /* ------------------------------------------------- bucket the page items */
  const buckets = useMemo(() => {
    const list = items || []
    if (!list.length) return []
    if (!groupKeyOf) return [{ key: '__all__', header: null, items: list }]

    const map = new Map()
    for (const item of list) {
      const raw = groupKeyOf(item)
      const key = raw === null || raw === undefined ? '' : String(raw)
      if (!map.has(key)) map.set(key, [])
      map.get(key).push(item)
    }
    // Follow the server's group ordering; anything it did not name trails behind.
    const ordered = []
    const seen = new Set()
    for (const g of groups || []) {
      const key = g.key === null || g.key === undefined ? '' : String(g.key)
      if (map.has(key) && !seen.has(key)) {
        ordered.push({ key, header: g, items: map.get(key) })
        seen.add(key)
      }
    }
    for (const [key, rows] of map) {
      if (!seen.has(key)) ordered.push({ key, header: { key, label: key, count: rows.length }, items: rows })
    }
    return ordered
  }, [items, groups, groupKeyOf])

  /* --------------------------------------------- lay the sections out */
  const sections = useMemo(() => {
    let offset = 0
    return buckets.map((bucket) => {
      if (bucket.header) offset += headerEstimate
      const rowCount = Math.ceil(bucket.items.length / columns)
      const height = rowCount * rowPitch
      const section = {
        key: bucket.key,
        header: bucket.header,
        shown: bucket.items.length,
        items: bucket.items,
        rowCount,
        height,
        offset
      }
      offset += height
      return section
    })
  }, [buckets, columns, rowPitch])

  /* ------------------------------------------------------ visible window */
  const windowed = useMemo(() => {
    const view = viewport || 800
    const from = scrollTop - OVERSCAN_ROWS * rowPitch
    const to = scrollTop + view + OVERSCAN_ROWS * rowPitch
    let mounted = 0

    return sections.map((section) => {
      const first = Math.max(0, Math.floor((from - section.offset) / rowPitch))
      const last = Math.min(
        section.rowCount,
        Math.ceil((to - section.offset) / rowPitch)
      )
      const rows = []
      for (let r = first; r < last && mounted < MAX_MOUNTED; r += 1) {
        const slice = section.items.slice(r * columns, r * columns + columns)
        if (!slice.length) break
        mounted += slice.length
        rows.push({ index: r, top: r * rowPitch, items: slice })
      }
      return { ...section, rows }
    })
  }, [sections, scrollTop, viewport, rowPitch, columns])

  /* ------------------------------------------------------- row measurement */
  const measureRef = useCallback((node) => {
    if (!node) return
    const h = node.offsetHeight
    if (!h) return
    setMeasuredRow((prev) => (h > prev ? h : prev))
  }, [])

  const mountedCount = windowed.reduce(
    (acc, s) => acc + s.rows.reduce((a, r) => a + r.items.length, 0), 0
  )

  return {
    columns,
    columnWidth,
    sections: windowed,
    measureRef,
    mountedCount,
    rowPitch,
    ready: width > 0
  }
}
