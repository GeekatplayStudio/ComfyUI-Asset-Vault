import React, { useMemo, useCallback, useEffect, useRef, useState } from 'react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useDebounced from '../../hooks/useDebounced.js'
import useRoleCoverage from '../../hooks/useRoleCoverage.js'
import { useVault, useTabView } from '../../state/VaultContext.jsx'
import StorageOverview from '../storage/StorageOverview.jsx'
import CandidatesPanel from '../storage/CandidatesPanel.jsx'
import CleanupDialog from '../storage/CleanupDialog.jsx'
import DuplicatesPanel from '../storage/DuplicatesPanel.jsx'
import TrashPanel from '../storage/TrashPanel.jsx'
import ComfyUIPanel from '../storage/ComfyUIPanel.jsx'
import { humanise } from '../../services/format.js'

/*
 * StorageTab - "where did my terabyte go, and what can I safely delete?"
 *
 * One workspace, five layers (C11): the summary answers the first half of the
 * question at a glance, and every number in it drills into the paged table that
 * answers the second half. The rail picks the layer; nothing here invents a new
 * interaction model.
 *
 * Requests are made per section, so opening the tab costs one summary call.
 */

const STALE_OPTIONS = [30, 90, 180, 365]

/* §18 filters are CSV, not repeated params - the server keeps only the last
   value of a repeated key, which would silently narrow a multi-select. */
function csv(value) {
  if (value === undefined || value === null) return undefined
  const list = Array.isArray(value) ? value : [value]
  const clean = list.filter((v) => v !== null && v !== undefined && v !== '')
  return clean.length ? clean.join(',') : undefined
}

export default function StorageTab({ onStatus, onOpenUid, registerApi }) {
  const { state, dispatch } = useVault()
  const { view, patch, setFilter, clearFilters } = useTabView('storage')
  const epoch = state.dataEpoch
  const scrollRef = useRef(null)
  const [cleanup, setCleanup] = useState(null)
  const [staleDays, setStaleDays] = useState(180)
  const [footprintNonce, setFootprintNonce] = useState(0)

  const section = view.section || 'overview'
  const debouncedQuery = useDebounced(view.q, 140)

  /* ------------------------------------------------------------- summary */
  const summary = useResource(
    'storage:summary:' + staleDays + ':' + footprintNonce,
    (s) => api.storageSummary(
      { stale_days: staleDays, refresh: footprintNonce > 0 ? true : undefined }, s
    ),
    { epoch }
  )
  const roots = useResource('storage:roots', (s) => api.storageRoots(s), { epoch })

  /* The false-positive guard: which roles are 100% unused (see the hook). */
  const coverage = useRoleCoverage(epoch)

  /* ---------------------------------------------------------- candidates */
  const candidateQuery = useMemo(() => ({
    sort: view.sort,
    kind: csv(view.filters.kind),
    reason: csv(view.filters.reason),
    category: csv(view.filters.category),
    root_id: csv(view.filters.root_id),
    q: debouncedQuery || undefined,
    include_protected: view.includeProtected,
    stale_days: staleDays,
    limit: view.limit,
    offset: view.offset
  }), [view.sort, view.filters, view.includeProtected, view.limit, view.offset,
    debouncedQuery, staleDays])

  const candidates = useResource(
    section === 'cleanup' ? 'storage:candidates:' + JSON.stringify(candidateQuery) : null,
    (s) => api.storageCandidates(candidateQuery, s),
    { epoch }
  )

  /* ---------------------------------------------------------- duplicates */
  const duplicates = useResource(
    section === 'duplicates' ? 'storage:duplicates:' + view.dupMethod : null,
    (s) => api.storageDuplicates({ method: view.dupMethod, limit: 100 }, s),
    { epoch }
  )

  /* --------------------------------------------------------------- trash */
  const trash = useResource(
    section === 'trash' ? 'storage:trash' : null,
    (s) => api.trash({ limit: 200 }, s),
    { epoch }
  )

  /* ------------------------------------------------------------- ComfyUI */
  const comfyInfo = useResource(
    section === 'comfyui' ? 'storage:comfy:info' : null,
    (s) => api.comfyInfo(s),
    { epoch }
  )
  const [latestNonce, setLatestNonce] = useState(0)
  const comfyLatest = useResource(
    section === 'comfyui' ? 'storage:comfy:latest:' + latestNonce : null,
    (s) => api.comfyLatest(latestNonce > 0 ? true : undefined, s),
    { epoch }
  )
  const comfyPlan = useResource(
    section === 'comfyui' ? 'storage:comfy:plan' : null,
    // A 404 here means no updater was discovered; the panel says so rather than
    // inventing a path.
    (s) => api.comfyUpdatePlan(undefined, s).catch(() => null),
    { epoch }
  )
  const comfyTemplates = useResource(
    section === 'comfyui' ? 'storage:comfy:templates' : null,
    (s) => api.comfyTemplates({ limit: 1 }, s).catch(() => null),
    { epoch }
  )

  /* -------------------------------------------------- status bar reporting */
  const page = section === 'cleanup' ? (candidates.data && candidates.data.page) : null
  const meta = section === 'cleanup' ? (candidates.data && candidates.data.meta) : null

  useEffect(() => {
    onStatus({
      page,
      elapsed: meta ? meta.elapsed_ms : (summary.data && summary.data.footprint
        ? summary.data.footprint.elapsed_ms : null),
      mode: null
    })
  }, [page, meta, summary.data, onStatus])

  /* ------------------------------------------------------ keyboard bridge */
  const items = useMemo(
    () => (candidates.data && candidates.data.items) || [], [candidates.data]
  )

  const requestCleanup = useCallback((mode) => {
    if (!view.selection.length) return
    setCleanup({ mode, uids: view.selection })
  }, [view.selection])

  useEffect(() => {
    registerApi({
      items,
      selectAll: () => patch({ selection: items.map((i) => i.uid) }, { keepOffset: true }),
      clearSelection: () => patch({ selection: [], focusUid: null }, { keepOffset: true }),
      requestOp: (kind) => { if (kind === 'delete') requestCleanup('trash') },
      openFocused: () => {
        const focused = items.find((i) => i.uid === view.focusUid) || items[0]
        if (focused && onOpenUid) onOpenUid(focused.uid)
      },
      move: (delta) => {
        const idx = items.findIndex((i) => i.uid === view.focusUid)
        let next = idx < 0 ? 0 : idx + (Array.isArray(delta) ? (delta[1] || delta[0]) : 0)
        if (delta === 'home') next = 0
        if (delta === 'end') next = items.length - 1
        next = Math.max(0, Math.min(items.length - 1, next))
        const target = items[next]
        if (target) {
          patch({ focusUid: target.uid, detailUid: target.uid }, { keepOffset: true })
        }
      },
      refresh: candidates.refresh
    })
  }, [items, view.focusUid, patch, registerApi, candidates.refresh, requestCleanup, onOpenUid])

  /* --------------------------------------------------- reason facet counts */
  const reasonFacets = useMemo(() => {
    const groups = (summary.data && summary.data.reclaim && summary.data.reclaim.groups) || []
    const byReason = new Map()
    for (const g of groups) {
      if (!g.reason || !g.count) continue
      const hit = byReason.get(g.reason)
      if (hit) {
        hit.count += g.count
        hit.title += ' / ' + g.label
      } else {
        byReason.set(g.reason, {
          value: g.reason,
          label: humanise(g.reason),
          count: g.count,
          confidence: g.confidence,
          title: g.label
        })
      }
    }
    // "Large" has no summary group of its own - it is a size threshold applied
    // to everything - so it is offered without a count rather than hidden.
    const out = Array.from(byReason.values())
    out.push({
      value: 'large', label: 'Large', count: undefined, confidence: 'measured',
      title: 'Files big enough to matter on their own'
    })
    return out
  }, [summary.data])

  /* ------------------------------------------------------------- handlers */
  const goToCleanup = useCallback((filters) => {
    patch({ section: 'cleanup', selection: [], offset: 0, q: '' })
    if (filters) {
      setFilter('reason', filters.reason || null)
      setFilter('category', filters.category || null)
    }
  }, [patch, setFilter])

  const refreshSummary = useCallback((hard) => {
    if (hard) setFootprintNonce((n) => n + 1)
    else summary.refresh()
    roots.refresh()
  }, [summary, roots])

  const toggleDuplicateMember = useCallback((member) => {
    const next = view.selection.includes(member.uid)
      ? view.selection.filter((u) => u !== member.uid)
      : [...view.selection, member.uid]
    patch({ selection: next }, { keepOffset: true })
  }, [view.selection, patch])

  const names = useMemo(() => {
    if (!cleanup) return []
    const known = new Map(items.map((i) => [i.uid, i.filename || i.name]))
    for (const g of (duplicates.data && duplicates.data.items) || []) {
      for (const m of g.items || []) known.set(m.uid, m.name)
    }
    return cleanup.uids.map((u) => known.get(u) || u)
  }, [cleanup, items, duplicates.data])

  /* --------------------------------------------------------------- render */
  let body = null

  if (section === 'overview') {
    body = (
      <>
        <div className="gp-toolbar">
          <span className="gp-toolbar__label">Storage and maintenance</span>
          <span className="gp-toolbar__spacer" />
          <div className="gp-toolbar__group">
            <span className="gp-toolbar__label">Count as stale after</span>
            <span className="gp-selectwrap">
              <select
                className="gp-select gp-select--bare"
                value={String(staleDays)}
                aria-label="Days before content counts as stale"
                onChange={(e) => setStaleDays(Number(e.target.value))}
              >
                {STALE_OPTIONS.map((d) => (
                  <option key={d} value={d}>{d} days</option>
                ))}
              </select>
              <span className="gp-selectwrap__caret" aria-hidden="true" />
            </span>
          </div>
        </div>
        <div className="gp-facetbar gp-facetbar--empty" />
        <div className="gp-main__body" ref={scrollRef}>
          <StorageOverview
            summary={summary.data}
            roots={roots.data}
            coverage={coverage}
            loading={summary.loading}
            error={summary.error}
            staleDays={staleDays}
            onRefresh={refreshSummary}
            onReview={goToCleanup}
          />
        </div>
      </>
    )
  } else if (section === 'cleanup') {
    body = (
      <CandidatesPanel
        view={view}
        patch={patch}
        setFilter={setFilter}
        clearFilters={clearFilters}
        list={candidates}
        items={items}
        page={page}
        meta={meta}
        coverage={coverage}
        scrollRef={scrollRef}
        reasonFacets={reasonFacets}
        staleDays={staleDays}
        onSelectRow={(item) => { if (onOpenUid) onOpenUid(item.uid) }}
        onCleanup={requestCleanup}
      />
    )
  } else if (section === 'duplicates') {
    body = (
      <DuplicatesPanel
        data={duplicates.data}
        loading={duplicates.loading}
        error={duplicates.error}
        method={view.dupMethod}
        selection={view.selection}
        onMethod={(m) => patch({ dupMethod: m, selection: [] })}
        onToggle={toggleDuplicateMember}
        onCleanup={() => requestCleanup('trash')}
        onClearSelection={() => patch({ selection: [] }, { keepOffset: true })}
        onRefresh={duplicates.refresh}
        onHash={() => dispatch({
          type: 'open-modal',
          name: 'hash',
          props: { presetUids: view.selection }
        })}
      />
    )
  } else if (section === 'trash') {
    body = (
      <TrashPanel
        data={trash.data}
        summary={summary.data && summary.data.trash}
        loading={trash.loading}
        error={trash.error}
        onRefresh={() => { trash.refresh(); summary.refresh() }}
      />
    )
  } else {
    body = (
      <ComfyUIPanel
        info={comfyInfo.data}
        latest={comfyLatest.data}
        plan={comfyPlan.data}
        templates={comfyTemplates.data}
        loading={comfyInfo.loading}
        error={comfyInfo.error}
        onRefresh={comfyInfo.refresh}
        onCheckLatest={() => setLatestNonce((n) => n + 1)}
      />
    )
  }

  return (
    <>
      {body}

      {cleanup ? (
        <CleanupDialog
          uids={cleanup.uids}
          mode={cleanup.mode}
          names={names}
          wholeRoleUids={coverage.uids}
          onClose={() => setCleanup(null)}
          onCompleted={() => {
            patch({ selection: [], focusUid: null, detailUid: null }, { keepOffset: true })
            summary.refresh()
            roots.refresh()
          }}
        />
      ) : null}
    </>
  )
}
