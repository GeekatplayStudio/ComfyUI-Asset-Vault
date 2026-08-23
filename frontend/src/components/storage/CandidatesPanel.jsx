import React, { useMemo, useCallback, useRef, useEffect } from 'react'
import {
  ArrowDown, ArrowUp, Trash2, X, Search, AlertTriangle, HardDrive
} from 'lucide-react'
import Button from '../common/Button.jsx'
import Chip from '../common/Chip.jsx'
import Toggle from '../common/Toggle.jsx'
import SearchInput from '../common/SearchInput.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import CandidateRow from './CandidateRow.jsx'
import useVirtualGrid from '../../hooks/useVirtualGrid.js'
import { bytes as fmtBytes, count as fmtCount, humanise } from '../../services/format.js'

/*
 * CandidatesPanel - the one paged detail table behind "largest files",
 * "oldest / stale content" and "most reclaimable" (API_CONTRACT §18).
 *
 * C10.5 asks for size AND age as first-class sorts in the same view, so the
 * three orderings are the column headers themselves rather than a buried
 * dropdown: the control sits on the column it orders.
 *
 * The selection bar carries the running reclaimable total and is the ONLY
 * route to a destructive action here. It sits on its own row, and the two
 * destructive buttons are separated from everything routine by a spacer and a
 * rule, so a mis-click cannot land on them.
 */

const KINDS = [
  { value: 'model', label: 'Models' },
  { value: 'output', label: 'Outputs' }
]

/* API_CONTRACT §18 batch cap. Stated in the UI rather than silently chunked. */
export const BATCH_CAP = 200

const SORTS = [
  { value: 'reclaim', label: 'Score', title: 'Combined reclaim score, 0-100' },
  { value: 'size', label: 'Size', title: 'Largest first' },
  { value: 'age', label: 'Age', title: 'Oldest first' }
]

function SortHeader({ id, label, title, active, onSort }) {
  return (
    <button
      type="button"
      className={'gp-btn gp-btn--ghost gp-btn--sm' + (active ? ' gp-btn--on' : '')}
      aria-pressed={active}
      title={title}
      onClick={() => onSort(id)}
    >
      <span className="gp-btn__label">{label}</span>
      {active
        ? (id === 'name'
          ? <ArrowUp className="gp-btn__icon" aria-hidden="true" />
          : <ArrowDown className="gp-btn__icon" aria-hidden="true" />)
        : null}
    </button>
  )
}

export default function CandidatesPanel(props) {
  const {
    view, patch, setFilter, clearFilters, list, items, page, meta,
    coverage, scrollRef, onSelectRow, onCleanup, reasonFacets, staleDays
  } = props

  const selection = useMemo(() => new Set(view.selection), [view.selection])

  /* Sizes are remembered as rows are seen, so the running total survives paging
     and filter changes. The authoritative figure still comes from
     /storage/estimate when the confirmation opens. */
  const sizes = useRef(new Map())
  useEffect(() => {
    for (const item of items) sizes.current.set(item.uid, item.size || 0)
  }, [items])

  const selectedBytes = useMemo(() => {
    let total = 0
    for (const uid of view.selection) total += sizes.current.get(uid) || 0
    return total
  }, [view.selection])

  const selectedProtected = useMemo(
    () => items.filter((i) => selection.has(i.uid) && i.protected).length,
    [items, selection]
  )

  const toggle = useCallback((item) => {
    const next = view.selection.includes(item.uid)
      ? view.selection.filter((u) => u !== item.uid)
      : [...view.selection, item.uid]
    patch({ selection: next }, { keepOffset: true })
  }, [view.selection, patch])

  const selectRow = useCallback((item, event) => {
    if (event && (event.ctrlKey || event.metaKey)) { toggle(item); return }
    if (event && event.shiftKey && view.focusUid) {
      const from = items.findIndex((i) => i.uid === view.focusUid)
      const to = items.findIndex((i) => i.uid === item.uid)
      if (from >= 0 && to >= 0) {
        const lo = Math.min(from, to)
        const hi = Math.max(from, to)
        const add = items.slice(lo, hi + 1).map((i) => i.uid)
        const merged = Array.from(new Set([...view.selection, ...add]))
        patch({ selection: merged, focusUid: item.uid, detailUid: item.uid },
          { keepOffset: true })
        return
      }
    }
    patch({ focusUid: item.uid, detailUid: item.uid }, { keepOffset: true })
    if (onSelectRow) onSelectRow(item)
  }, [items, view.selection, view.focusUid, patch, toggle, onSelectRow])

  const pageAllChecked = items.length > 0 && items.every((i) => selection.has(i.uid))
  const togglePage = useCallback(() => {
    const pageUids = items.map((i) => i.uid)
    if (pageAllChecked) {
      const drop = new Set(pageUids)
      patch({ selection: view.selection.filter((u) => !drop.has(u)) }, { keepOffset: true })
    } else {
      patch({ selection: Array.from(new Set([...view.selection, ...pageUids])) },
        { keepOffset: true })
    }
  }, [items, pageAllChecked, view.selection, patch])

  const setSort = useCallback((sort) => patch({ sort }), [patch])

  const { sections, measureRef } = useVirtualGrid({
    scrollRef, items, groups: null, groupKeyOf: null, mode: 'list', tile: view.tile
  })

  const activeFilters = useMemo(() => {
    const out = []
    for (const [field, value] of Object.entries(view.filters)) {
      const values = Array.isArray(value) ? value : [value]
      for (const v of values) {
        out.push({ field, value: String(v), label: humanise(field) + ': ' + humanise(String(v)) })
      }
    }
    return out
  }, [view.filters])

  const asArray = (field) => {
    const v = view.filters[field]
    if (v === undefined || v === null) return []
    return (Array.isArray(v) ? v : [v]).map(String)
  }

  const toggleIn = (field, value) => {
    const current = asArray(field)
    const next = current.includes(value)
      ? current.filter((x) => x !== value)
      : [...current, value]
    setFilter(field, next.length ? next : null)
  }

  const gapActive = coverage.categoryFilterExact &&
    coverage.categories.length > 0 &&
    coverage.categories.every((c) => asArray('category').includes(c)) &&
    asArray('category').length === coverage.categories.length

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the candidates"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (activeFilters.length || view.q ? (
    <EmptyState icon={Search} title="Nothing matches"
      text="No file matches the current filters."
      actions={<Button onClick={() => { patch({ q: '' }); clearFilters() }}>
        Clear the filters</Button>} />
  ) : (
    <EmptyState icon={HardDrive} title="Nothing to reclaim"
      text={'No model or output is flagged at ' + fmtCount(staleDays) + ' days.'} />
  ))

  return (
    <>
      {/* .gp-main is a three-row grid: controls, facets, body. The selection bar
          shares the controls row so the row count never changes. */}
      <div>
      {/* ------------------------------------------------------------ toolbar */}
      <div className="gp-toolbar">
        <div className="gp-toolbar__group">
          <span className="gp-toolbar__label">Show</span>
          {KINDS.map((k) => (
            <Chip
              key={k.value}
              label={k.label}
              selected={asArray('kind').includes(k.value)}
              onClick={() => toggleIn('kind', k.value)}
            />
          ))}
        </div>

        <span className="gp-divider gp-divider--v" />

        <div className="gp-toolbar__group gp-u-grow">
          <SearchInput
            value={view.q}
            onChange={(q) => patch({ q })}
            placeholder="Filter by name or folder"
          />
        </div>

        <div className="gp-toolbar__group">
          <Toggle
            id="storage-include-protected"
            checked={view.includeProtected}
            label="Include kept"
            title="Favourites and 4+ ratings are flagged, never hidden. Switch this off to leave them out of a cleanup pass."
            onChange={(v) => patch({ includeProtected: v })}
          />
          <span className="gp-toolbar__label gp-u-num">
            {page ? fmtCount(page.total) + ' files' : '-'}
          </span>
          <span className="gp-toolbar__label gp-u-num"
            title="Total size of everything matching the current filters">
            {meta ? fmtBytes(meta.matched_bytes) : ''}
          </span>
        </div>
      </div>

      {/* ------------------------------------------------- the selection bar */}
      {view.selection.length ? (
        <div className="gp-toolbar gp-u-bg-raised" role="region" aria-label="Cleanup selection">
          <div className="gp-toolbar__group">
            <span className="gp-u-num gp-u-fw-600 gp-u-fs-15">
              {fmtBytes(selectedBytes)}
            </span>
            <span className="gp-u-fs-11 gp-u-meta">
              would be reclaimed from {fmtCount(view.selection.length)} file
              {view.selection.length === 1 ? '' : 's'}
            </span>
            {selectedProtected ? (
              <span className="gp-u-fs-11 gp-u-warn">
                {fmtCount(selectedProtected)} of them you marked as keepers
              </span>
            ) : null}
            {view.selection.length > BATCH_CAP ? (
              <span className="gp-u-fs-11 gp-u-danger">
                over the {BATCH_CAP}-file limit for one action
              </span>
            ) : null}
          </div>

          <div className="gp-toolbar__group">
            <Button size="sm" variant="ghost" icon={X} label="Clear selection"
              onClick={() => patch({ selection: [] }, { keepOffset: true })} />
          </div>

          {/* Destructive actions live past the spacer and the rule, never beside
              a routine control. */}
          <div className="gp-toolbar__spacer" />
          <span className="gp-divider gp-divider--v" />
          <div className="gp-toolbar__group">
            <Button
              size="sm"
              variant="dangerGhost"
              icon={Trash2}
              label="Move to trash"
              count={fmtCount(view.selection.length)}
              disabled={view.selection.length > BATCH_CAP}
              title={view.selection.length > BATCH_CAP
                ? 'Select at most ' + BATCH_CAP + ' files for one cleanup action'
                : 'Move ' + view.selection.length + ' file(s) to the vault trash, recoverable'}
              onClick={() => onCleanup('trash')}
            />
            <Button
              size="sm"
              variant="danger"
              label="Delete permanently"
              disabled={view.selection.length > BATCH_CAP}
              title={view.selection.length > BATCH_CAP
                ? 'Select at most ' + BATCH_CAP + ' files for one cleanup action'
                : 'Remove ' + view.selection.length + ' file(s) from disk with no way back'}
              onClick={() => onCleanup('permanent')}
            />
          </div>
        </div>
      ) : null}
      </div>

      {/* ------------------------------------------------------------ facets */}
      <div className={'gp-facetbar' + (
        !reasonFacets.length && !activeFilters.length ? ' gp-facetbar--empty' : ''
      )}
      >
        <span className="gp-toolbar__label">Reason</span>
        {reasonFacets.map((f) => (
          <Chip
            key={f.value}
            label={f.label}
            count={f.count}
            selected={asArray('reason').includes(f.value)}
            title={f.title}
            tone={f.confidence === 'inferred' ? 'inferred' : undefined}
            onClick={() => toggleIn('reason', f.value)}
          />
        ))}

        {coverage.flagged.length && coverage.categoryFilterExact ? (
          <>
            <span className="gp-divider gp-divider--v" />
            <Chip
              label="Whole role unreferenced"
              count={coverage.count}
              tone="inferred"
              selected={gapActive}
              title={'Roles where every model shows zero references: ' +
                coverage.flagged.map((r) => humanise(r.role) + ' ' + r.unused + '/' + r.total)
                  .join(', ') +
                '. More likely a gap in what the indexer can read than genuine disuse.'}
              onClick={() => setFilter('category', gapActive ? null : coverage.categories)}
            />
          </>
        ) : null}

        {activeFilters.length ? (
          <>
            <span className="gp-divider gp-divider--v" />
            <Button size="sm" variant="ghost" icon={X} label="Clear"
              onClick={clearFilters} />
          </>
        ) : null}
      </div>

      {/* -------------------------------------------------------------- list */}
      {list.loading && !items.length ? (
        <div className="gp-main__body gp-main__body--flush"><SkeletonRows rows={14} /></div>
      ) : !items.length ? (
        <div className="gp-main__body">{empty}</div>
      ) : (
        <div className="gp-main__body gp-main__body--flush" ref={scrollRef} tabIndex={-1}>
          {/* Column header. Same .gp-row geometry as the data rows, so the three
              numeric columns line up under the controls that order them. */}
          <div className="gp-row gp-u-sticky-top gp-u-bg-raised gp-u-caps gp-u-fs-10">
            <label className="gp-check gp-u-shrink0"
              title={pageAllChecked ? 'Deselect this page' : 'Select this page'}>
              <input className="gp-check__input" type="checkbox"
                checked={pageAllChecked}
                aria-label="Select every file on this page"
                onChange={togglePage} />
              <span className="gp-check__box">
                <svg viewBox="0 0 12 12" aria-hidden="true">
                  <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor"
                    strokeWidth="1.8" />
                </svg>
              </span>
            </label>
            <span className="gp-row__name gp-u-meta">
              <SortHeader id="name" label="File and folder" title="Name A-Z"
                active={view.sort === 'name'} onSort={setSort} />
            </span>
            <span className="gp-row__cell">Why it is listed</span>
            <span className="gp-row__cell gp-row__cell--num">
              <SortHeader id="reclaim" label="Score" title={SORTS[0].title}
                active={view.sort === 'reclaim'} onSort={setSort} />
            </span>
            <span className="gp-row__cell gp-row__cell--num">
              <SortHeader id="size" label="Size" title={SORTS[1].title}
                active={view.sort === 'size'} onSort={setSort} />
            </span>
            <span className="gp-row__cell gp-row__cell--num">
              <SortHeader id="age" label="Age" title={SORTS[2].title}
                active={view.sort === 'age'} onSort={setSort} />
            </span>
          </div>

          <div className="gp-vgrid">
            {sections.map((section) => (
              <div className="gp-vgrid__spacer" key={section.key}
                style={{ height: section.height }}>
                {section.rows.map((row) => (
                  <div
                    key={section.key + ':' + row.index}
                    className="gp-vgrid__row"
                    ref={measureRef}
                    style={{ top: row.top }}
                  >
                    {row.items.map((item) => (
                      <CandidateRow
                        key={item.uid}
                        item={item}
                        checked={selection.has(item.uid)}
                        focused={view.focusUid === item.uid}
                        wholeRoleGap={coverage.uids.has(item.uid)}
                        onSelect={selectRow}
                        onToggle={toggle}
                      />
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
