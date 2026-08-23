import React, { useCallback, useMemo, useRef, useState } from 'react'
import {
  Activity, AlertTriangle, RefreshCw, Search, X, ChevronLeft, ChevronRight
} from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useDebounced from '../../hooks/useDebounced.js'
import useVirtualGrid from '../../hooks/useVirtualGrid.js'
import Button from '../common/Button.jsx'
import Chip from '../common/Chip.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import ActivitySummary from './ActivitySummary.jsx'
import ActivityDetail from './ActivityDetail.jsx'
import ActivityRow from './ActivityRow.jsx'
import { count as fmtCount } from '../../services/format.js'

/*
 * ActivityPanel - Settings -> Activity, the reading half of DECISIONS C5 rail 3.
 *
 * The owner deliberately granted external MCP clients full file-operation
 * access, delete included, over a library that takes hours to re-download. This
 * view is how that grant stays reviewable, so it is built to answer, in order:
 *
 *   1. has anything outside this app changed my library?   (the summary)
 *   2. did any of it fail?                                 (the summary)
 *   3. what exactly did that one call ask for?             (a row, on demand)
 *
 * The log is append-only. There is no control anywhere in this panel that
 * edits, prunes or clears a row, because the API offers none - a log the app
 * can erase is not a log.
 */

const PAGE_SIZE = 100
/* Deep enough to show a dozen rows without the modal growing past the viewport;
   the list keeps its own scrollbar and the window is virtualized. */
const LIST_HEIGHT = 340

const OUTCOMES = [
  { value: 'ok', label: 'Applied' },
  { value: 'partial', label: 'Partly applied' },
  { value: 'error', label: 'Failed' }
]

const TRANSPORTS = [
  { value: 'http', label: 'HTTP' },
  { value: 'stdio', label: 'stdio' }
]

export default function ActivityPanel() {
  const [q, setQ] = useState('')
  const [outcome, setOutcome] = useState(null)
  const [tool, setTool] = useState(null)
  const [transport, setTransport] = useState(null)
  const [sessionId, setSessionId] = useState(null)
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState(null)
  const [nonce, setNonce] = useState(0)
  const scrollRef = useRef(null)

  const debouncedQ = useDebounced(q, 180)

  const query = useMemo(() => ({
    limit: PAGE_SIZE,
    offset,
    q: debouncedQ || undefined,
    outcome: outcome || undefined,
    tool: tool || undefined,
    transport: transport || undefined,
    session_id: sessionId || undefined
  }), [offset, debouncedQ, outcome, tool, transport, sessionId])

  /* An external client writes to this log while the app is open, so the epoch
     is a manual refresh rather than the vault's own mutation counter. */
  const list = useResource(
    'mcp:audit:' + nonce + ':' + JSON.stringify(query),
    (signal) => api.mcpAudit(query, signal)
  )

  const data = list.data
  const items = useMemo(() => (data && data.items) || [], [data])
  const page = (data && data.page) || null
  const summary = (data && data.summary) || null
  const filtered = Boolean(debouncedQ || outcome || tool || transport || sessionId)

  const setFilter = useCallback((setter) => (value) => {
    setter(value)
    setOffset(0)
    setSelected(null)
  }, [])

  const clearFilters = useCallback(() => {
    setQ('')
    setOutcome(null)
    setTool(null)
    setTransport(null)
    setSessionId(null)
    setOffset(0)
    setSelected(null)
  }, [])

  const refresh = useCallback(() => {
    setNonce((n) => n + 1)
  }, [])

  const { sections, measureRef } = useVirtualGrid({
    scrollRef, items, groups: null, groupKeyOf: null, mode: 'list'
  })

  const total = page ? page.total : 0
  const from = total ? offset + 1 : 0
  const to = page ? offset + page.returned : 0

  /* ------------------------------------------------------------- states */
  if (list.error) {
    return (
      <EmptyState
        tone="error"
        icon={AlertTriangle}
        title="The activity log could not be read"
        text={list.error.message}
        actions={<Button variant="primary" onClick={refresh}>Try again</Button>}
      />
    )
  }

  const emptyLog = !list.loading && page && page.total === 0 && !filtered

  return (
    <>
      <div className="gp-u-row gp-u-between gp-u-gap-4 gp-u-mb-5">
        <p className="gp-u-fs-11 gp-u-meta gp-u-minw0">
          Every change an external MCP client made to this vault, oldest kept forever.
          Nothing here can be edited or deleted, including by this app.
        </p>
        <Button size="sm" variant="ghost" icon={RefreshCw} label="Refresh"
          onClick={refresh} />
      </div>

      {list.loading && !data ? (
        <SkeletonRows rows={6} />
      ) : emptyLog ? (
        <EmptyState
          icon={Activity}
          title="No external client has changed anything yet"
          text={'This is the audit trail of the MCP server. When an MCP client ' +
            'connected to this vault renames, moves, deletes, tags or reindexes ' +
            'something, one line lands here with the time, the tool, the exact ' +
            'arguments it was given, the items it touched and whether it worked. ' +
            'Entries are appended and never removed.'}
        />
      ) : (
        <>
          <ActivitySummary
            summary={summary}
            activeOutcome={outcome}
            activeTool={tool}
            onFilterOutcome={setFilter(setOutcome)}
            onFilterTool={setFilter(setTool)}
          />

          {/* --------------------------------------------------------- filters */}
          <div className="gp-group-head gp-u-mt-6">
            <span className="gp-group-head__label">The log</span>
            <span className="gp-group-head__count">{fmtCount(total)}</span>
            <span className="gp-group-head__rule" />
          </div>

          <div className="gp-u-row gp-u-gap-4 gp-u-wrap">
            <div className="gp-search gp-u-grow">
              <Search className="gp-search__icon" aria-hidden="true" />
              <input
                className="gp-search__input"
                type="search"
                value={q}
                aria-label="Filter the activity log by tool name or item"
                placeholder="Filter by tool name or item, e.g. model:41"
                autoComplete="off"
                spellCheck="false"
                onChange={(e) => { setQ(e.target.value); setOffset(0); setSelected(null) }}
              />
            </div>
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-shrink0">
              {/* A facet nothing ever produced is noise, so a zero count hides
                  the chip unless it is the filter currently applied. */}
              {OUTCOMES.filter((o) => outcome === o.value || !summary ||
                summary.by_outcome[o.value] > 0).map((o) => (
                  <Chip
                    key={o.value}
                    label={o.label}
                    count={outcome || !summary ? undefined : summary.by_outcome[o.value]}
                    selected={outcome === o.value}
                    tone={o.value === 'error' ? 'danger' : undefined}
                    onClick={() => setFilter(setOutcome)(
                      outcome === o.value ? null : o.value)}
                  />
              ))}
              <span className="gp-divider gp-divider--v" />
              {TRANSPORTS.map((t) => (
                <Chip
                  key={t.value}
                  label={t.label}
                  selected={transport === t.value}
                  title={t.value === 'stdio'
                    ? 'A client that launched the server as a local process'
                    : 'A client that connected over the loopback HTTP transport'}
                  onClick={() => setFilter(setTransport)(
                    transport === t.value ? null : t.value)}
                />
              ))}
            </div>
          </div>

          {filtered ? (
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
              {tool ? (
                <Chip label={'tool: ' + tool} mono small
                  onRemove={() => setFilter(setTool)(null)} />
              ) : null}
              {sessionId ? (
                <Chip label={'session: ' + sessionId.slice(0, 8)} mono small
                  onRemove={() => setFilter(setSessionId)(null)} />
              ) : null}
              <Button size="sm" variant="ghost" icon={X} label="Clear filters"
                onClick={clearFilters} />
            </div>
          ) : null}

          {/* ------------------------------------------- detail, on demand */}
          <ActivityDetail
            entry={selected}
            onClose={() => setSelected(null)}
            onFilterSession={(id) => { setFilter(setSessionId)(id) }}
          />

          {/* ------------------------------------------------------- the list */}
          {!items.length ? (
            <div className="gp-u-mt-5">
              <EmptyState
                small
                icon={Search}
                title="Nothing matches"
                text="No recorded call matches the current filters."
                actions={<Button onClick={clearFilters}>Clear the filters</Button>}
              />
            </div>
          ) : (
            <div
              className="gp-list gp-u-hairline gp-u-r-1 gp-u-scroll-y gp-u-mt-5"
              ref={scrollRef}
              /* The virtualised row is a grid whose track width comes from
                 --gp-grid-size, which the shell sets for the asset grid. This
                 list is one row per item wherever it is mounted, so it pins the
                 variable itself instead of inheriting a tile width. */
              style={{ maxHeight: LIST_HEIGHT, '--gp-grid-size': '100%' }}
              tabIndex={-1}
            >
              <div className="gp-vgrid">
                {sections.map((section) => (
                  <div className="gp-vgrid__spacer" key={section.key}
                    style={{ height: section.height }}
                  >
                    {section.rows.map((row) => (
                      <div
                        key={section.key + ':' + row.index}
                        className="gp-vgrid__row"
                        ref={measureRef}
                        style={{ top: row.top }}
                      >
                        {row.items.map((entry) => (
                          <ActivityRow
                            key={entry.id}
                            entry={entry}
                            selected={selected && selected.id === entry.id}
                            onSelect={(e) => setSelected(
                              selected && selected.id === e.id ? null : e)}
                          />
                        ))}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* --------------------------------------------------------- paging */}
          {page && page.total > PAGE_SIZE ? (
            <div className="gp-u-row gp-u-between gp-u-gap-4 gp-u-mt-4">
              <span className="gp-u-fs-10 gp-u-meta gp-u-num">
                {fmtCount(from)}-{fmtCount(to)} of {fmtCount(total)}, newest first
              </span>
              <span className="gp-u-row gp-u-gap-3">
                <Button size="sm" variant="ghost" icon={ChevronLeft} label="Newer"
                  disabled={offset === 0}
                  onClick={() => { setOffset(Math.max(0, offset - PAGE_SIZE)); setSelected(null) }} />
                <Button size="sm" variant="ghost" icon={ChevronRight} label="Older"
                  disabled={!page.has_more}
                  onClick={() => { setOffset(offset + PAGE_SIZE); setSelected(null) }} />
              </span>
            </div>
          ) : null}
        </>
      )}
    </>
  )
}
