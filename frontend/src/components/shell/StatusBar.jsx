import React from 'react'
import { ChevronLeft, ChevronRight, PanelLeft, PanelRight } from 'lucide-react'
import Select from '../common/Select.jsx'
import Button from '../common/Button.jsx'
import { PAGE_SIZES } from '../../state/actions.js'
import { count as fmtCount, bytes as fmtBytes, shortDuration } from '../../services/format.js'

/*
 * StatusBar - total count, selection count, paging and the live job indicator.
 * The dot is never the only signal: it always sits next to a text label.
 */
export default function StatusBar(props) {
  const {
    page, elapsed, mode, selectionCount, view, patch,
    indexStatus, hashStatus, stats, railOpen, detailsOpen, onToggleRail, onToggleDetails
  } = props

  const total = page && page.total !== undefined ? page.total : null
  const offset = (view && view.offset) || 0
  const limit = (view && view.limit) || 100
  const returned = page ? page.returned : 0
  const hasMore = page ? page.has_more : false
  const first = returned ? offset + 1 : 0
  const last = offset + returned

  const scanning = indexStatus && indexStatus.active
  const hashing = hashStatus && hashStatus.active

  let dot = 'gp-statusbar__dot--ok'
  let statusText = 'Idle'
  if (scanning) {
    const job = indexStatus.job || {}
    dot = 'gp-statusbar__dot--busy'
    statusText = 'Indexing ' + (job.phase || '') +
      (job.items_total ? ' ' + fmtCount(job.items_done) + '/' + fmtCount(job.items_total) : '')
  } else if (hashing) {
    const b = hashStatus.bytes || {}
    dot = 'gp-statusbar__dot--busy'
    statusText = 'Hashing ' + (b.percent !== undefined ? b.percent.toFixed(1) + '%' : '')
  } else if (stats && stats.integrity_issues) {
    dot = 'gp-statusbar__dot--warn'
    statusText = fmtCount(stats.integrity_issues) + ' integrity issue' +
      (stats.integrity_issues === 1 ? '' : 's')
  }

  return (
    <footer className="gp-statusbar">
      <div className="gp-statusbar__group">
        <Button
          size="sm"
          variant="ghost"
          iconOnly
          icon={PanelLeft}
          aria-label={railOpen ? 'Hide the groups rail' : 'Show the groups rail'}
          title={railOpen ? 'Hide the groups rail' : 'Show the groups rail'}
          aria-pressed={railOpen}
          onClick={onToggleRail}
        />
        <span className={'gp-statusbar__dot ' + dot} aria-hidden="true" />
        <span>{statusText}</span>
      </div>

      <span className="gp-statusbar__sep" />

      <div className="gp-statusbar__group">
        <span className="gp-statusbar__num">{total === null ? '-' : fmtCount(total)}</span>
        <span>items</span>
        {returned ? (
          <>
            <span className="gp-statusbar__sep" />
            <span className="gp-statusbar__num">{fmtCount(first)}-{fmtCount(last)}</span>
            <span>shown</span>
          </>
        ) : null}
        {selectionCount ? (
          <>
            <span className="gp-statusbar__sep" />
            <span className="gp-statusbar__num">{fmtCount(selectionCount)}</span>
            <span>selected</span>
          </>
        ) : null}
        {elapsed !== null && elapsed !== undefined ? (
          <>
            <span className="gp-statusbar__sep" />
            <span className="gp-statusbar__num">{shortDuration(elapsed)}</span>
          </>
        ) : null}
        {mode ? <span className="gp-u-meta gp-u-fs-10">{mode}</span> : null}
      </div>

      <span className="gp-statusbar__spacer" />

      {stats ? (
        <div className="gp-statusbar__group">
          <span className="gp-u-meta">Vault</span>
          <span className="gp-statusbar__num">{fmtBytes(stats.models_bytes)}</span>
          <span className="gp-u-meta">models</span>
          <span className="gp-statusbar__sep" />
          <span className="gp-statusbar__num">{fmtBytes(stats.outputs_bytes)}</span>
          <span className="gp-u-meta">outputs</span>
        </div>
      ) : null}

      <span className="gp-statusbar__sep" />

      <div className="gp-statusbar__group">
        <span className="gp-perpage">
          <span className="gp-perpage__label">Per page</span>
          <Select
            bare
            value={String(limit)}
            ariaLabel="Items per page"
            options={PAGE_SIZES.map((n) => ({ value: String(n), label: String(n) }))}
            onChange={(v) => patch({ limit: Number(v), offset: 0 })}
          />
        </span>
        <Button
          size="sm"
          variant="ghost"
          iconOnly
          icon={ChevronLeft}
          aria-label="Previous page"
          title="Previous page"
          disabled={offset === 0}
          onClick={() => patch({ offset: Math.max(0, offset - limit) }, { keepOffset: true })}
        />
        <Button
          size="sm"
          variant="ghost"
          iconOnly
          icon={ChevronRight}
          aria-label="Next page"
          title="Next page"
          disabled={!hasMore}
          onClick={() => patch({ offset: offset + limit }, { keepOffset: true })}
        />
        <Button
          size="sm"
          variant="ghost"
          iconOnly
          icon={PanelRight}
          aria-label={detailsOpen ? 'Hide the details panel' : 'Show the details panel'}
          title={detailsOpen ? 'Hide the details panel' : 'Show the details panel'}
          aria-pressed={detailsOpen}
          onClick={onToggleDetails}
        />
      </div>
    </footer>
  )
}
