import React, { useMemo } from 'react'
import { Copy, AlertTriangle, Hash } from 'lucide-react'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import Chip from '../common/Chip.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import ConfidenceMark from './ConfidenceMark.jsx'
import { bytes as fmtBytes, count as fmtCount, percent } from '../../services/format.js'

/*
 * DuplicatesPanel - candidate duplicate sets.
 *
 * Only sha256 is ever measured. name+size and "name across roots" are
 * inferences the owner has to confirm, so the method rides on every group and
 * the whole panel says how far hash coverage actually goes. Two files can share
 * a name and a byte count and still differ.
 *
 * The keep suggestion is a suggestion: the checkbox starts on the OTHER members
 * so nothing is pre-selected for deletion that the server picked to keep.
 */

const METHOD_LABEL = {
  sha256: 'Identical contents',
  'name+size': 'Same name and size',
  'name across roots': 'Same name in two roots'
}

export default function DuplicatesPanel(props) {
  const {
    data, loading, error, method, onMethod, selection, onToggle, onRefresh, onHash,
    onCleanup, onClearSelection
  } = props

  const items = (data && data.items) || []
  const meta = (data && data.meta) || {}
  const coverage = meta.hash_coverage || {}
  const methods = meta.methods || ['sha256', 'name+size', 'name across roots']
  const selected = useMemo(() => new Set(selection), [selection])

  const selectedBytes = useMemo(() => {
    let total = 0
    for (const group of items) {
      for (const member of group.items || []) {
        if (selected.has(member.uid)) total += member.size || 0
      }
    }
    return total
  }, [items, selected])

  const hashedPct = coverage.total ? (coverage.hashed / coverage.total) * 100 : 0

  if (error) {
    return (
      <>
        <div className="gp-toolbar">
          <span className="gp-toolbar__label">Duplicates</span>
        </div>
        <div className="gp-facetbar gp-facetbar--empty" />
        <div className="gp-main__body">
          <EmptyState tone="error" icon={AlertTriangle} title="Could not load the duplicates"
            text={error.message}
            actions={<Button variant="primary" onClick={onRefresh}>Try again</Button>} />
        </div>
      </>
    )
  }

  return (
    <>
      {/* .gp-main is a three-row grid: controls, facets, body. */}
      <div>
        <div className="gp-toolbar">
          <div className="gp-toolbar__group">
            <span className="gp-toolbar__label">Duplicate sets</span>
            <span className="gp-u-num gp-u-fw-600">{fmtCount(items.length)}</span>
            <span className="gp-u-fs-11 gp-u-meta">
              holding {fmtBytes(meta.reclaimable_bytes)} of recoverable space
            </span>
          </div>
          <div className="gp-toolbar__spacer" />
          <div className="gp-toolbar__group">
            <span className="gp-toolbar__label gp-u-num">
              {fmtCount(coverage.hashed)} of {fmtCount(coverage.total)} models hashed
            </span>
          </div>
        </div>

        {selection.length ? (
          <div className="gp-toolbar gp-u-bg-raised" role="region"
            aria-label="Duplicate cleanup selection">
            <div className="gp-toolbar__group">
              <span className="gp-u-num gp-u-fw-600 gp-u-fs-15">
                {fmtBytes(selectedBytes)}
              </span>
              <span className="gp-u-fs-11 gp-u-meta">
                would be reclaimed from {fmtCount(selection.length)} cop
                {selection.length === 1 ? 'y' : 'ies'}
              </span>
            </div>
            <div className="gp-toolbar__group">
              <Button size="sm" variant="ghost" label="Clear selection"
                onClick={onClearSelection} />
            </div>
            <div className="gp-toolbar__spacer" />
            <span className="gp-divider gp-divider--v" />
            <div className="gp-toolbar__group">
              <Button size="sm" variant="dangerGhost" label="Move to trash"
                count={fmtCount(selection.length)}
                title={'Move ' + selection.length + ' copy/copies to the vault trash'}
                onClick={onCleanup} />
            </div>
          </div>
        ) : null}
      </div>

      <div className="gp-facetbar">
        <span className="gp-toolbar__label">Matched by</span>
        {methods.map((m) => (
          <Chip
            key={m}
            label={METHOD_LABEL[m] || m}
            selected={method === m}
            tone={m === 'sha256' ? undefined : 'inferred'}
            title={m === 'sha256'
              ? 'Byte-for-byte identical. Needs both files hashed.'
              : 'A candidate match, not proof. Hash the group to be certain.'}
            onClick={() => onMethod(m)}
          />
        ))}
      </div>

      <div className="gp-main__body">
        {/* Hash coverage is the honesty check: without it nothing here is exact. */}
        <div className={'gp-callout gp-u-mb-6 gp-callout--' +
          (method === 'sha256' ? 'info' : 'ai')}
        >
          <span className="gp-callout__icon">
            {method === 'sha256' ? <Hash aria-hidden="true" /> : <Copy aria-hidden="true" />}
          </span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">
              {method === 'sha256'
                ? 'Exact matching, limited by hash coverage'
                : (
                  <span className="gp-inferred"
                    title="Matched on name and byte count, not on contents">
                    These are candidates, not confirmed duplicates
                  </span>
                )}
            </div>
            {fmtCount(coverage.hashed)} of {fmtCount(coverage.total)} models are hashed
            ({percent(hashedPct)}). Hashing is opt-in, so until it has run, matching on name
            and size is the best the vault can do - and two files can share a name and a byte
            count and still differ.
            <div className="gp-callout__actions">
              <Button size="sm" icon={Hash} label="Hash these models"
                title="Queue the models in these groups so the match can be proved"
                onClick={onHash} />
            </div>
          </div>
        </div>

        {loading && !items.length ? <SkeletonRows rows={6} /> : null}

        {!loading && !items.length ? (
          <EmptyState
            icon={Copy}
            title="No duplicate sets found"
            text={method === 'sha256'
              ? 'No two hashed files share a checksum. Only ' + fmtCount(coverage.hashed) +
                ' file(s) are hashed so far.'
              : 'No two files share a name and a byte count.'}
          />
        ) : null}

        {items.map((group) => (
          <div className="gp-panel gp-u-mb-5" key={group.key}>
            <div className="gp-u-row gp-u-between gp-u-gap-5 gp-u-mb-5">
              <div className="gp-u-row gp-u-gap-3 gp-u-minw0">
                <span className="gp-u-fw-600 gp-u-truncate">{group.key.split('@')[0]}</span>
                <ConfidenceMark confidence={group.confidence} reason="duplicate"
                  method={group.method} />
                <Badge tone="neutral" mono>{fmtCount(group.count)} copies</Badge>
              </div>
              <span className="gp-u-num gp-u-fs-11 gp-u-meta gp-u-shrink0"
                title="Freed by keeping one copy">
                {fmtBytes(group.reclaimable_bytes)} recoverable
              </span>
            </div>

            <table className="gp-table gp-table--compact gp-u-w-full">
              <thead>
                <tr>
                  <th />
                  <th>Copy</th>
                  <th>Folder</th>
                  <th className="gp-table__num">Size</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(group.items || []).map((member) => {
                  const keep = member.uid === group.suggested_keep_uid
                  return (
                    <tr key={member.uid} aria-selected={selected.has(member.uid)}>
                      <td>
                        <label className="gp-check"
                          title={keep
                            ? 'The largest copy - suggested to keep, but you decide'
                            : 'Select this copy for cleanup'}>
                          <input className="gp-check__input" type="checkbox"
                            checked={selected.has(member.uid)}
                            aria-label={'Select ' + member.name}
                            onChange={() => onToggle(member)} />
                          <span className="gp-check__box">
                            <svg viewBox="0 0 12 12" aria-hidden="true">
                              <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor"
                                strokeWidth="1.8" />
                            </svg>
                          </span>
                        </label>
                      </td>
                      <td className="gp-u-fg">
                        {member.name}
                        {member.protected ? <Badge tone="warn">kept</Badge> : null}
                      </td>
                      <td className="gp-u-fs-11 gp-u-meta gp-u-break-all"
                        title={member.abs_path}>
                        {member.category || member.abs_path}
                      </td>
                      <td className="gp-table__num gp-u-num">{fmtBytes(member.size)}</td>
                      <td className="gp-table__num">
                        {keep ? (
                          <Badge tone="ok" title="The server's suggestion, not a decision">
                            suggested keep
                          </Badge>
                        ) : null}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </>
  )
}
