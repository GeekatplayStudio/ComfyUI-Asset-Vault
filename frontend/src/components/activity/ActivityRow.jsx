import React from 'react'
import Badge from '../common/Badge.jsx'
import { count as fmtCount, dateTime, shortDuration } from '../../services/format.js'

/*
 * ActivityRow - one audited MCP tool call.
 *
 * Three things have to be legible without opening anything (C11 keeps the rest
 * for the detail block above the list):
 *
 *   what kind of call it was   a delete must not look like a tag assignment, so
 *                              destructive calls carry the danger badge and
 *                              plain writes the amber one;
 *   whether it worked          a failure paints the row's spine red through
 *                              .gp-row--error and names the error code;
 *   what it was given          the argument precis, because the arguments are
 *                              the entire reason this log is kept (C5 rail 3).
 *
 * A tool the current catalogue does not know is the one inferred judgement here
 * - what it did cannot be stated - so it takes the violet "~" treatment.
 */

/** `{uids:[2], mode:"permanent", confirm:true}` -> `uids[2] mode=permanent confirm` */
export function argumentPrecis(args) {
  if (!args || typeof args !== 'object') return ''
  const parts = []
  for (const [key, value] of Object.entries(args)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      if (!value.length) continue
      parts.push(key + '[' + value.length + ']')
    } else if (typeof value === 'boolean') {
      if (value) parts.push(key)
    } else if (typeof value === 'object') {
      parts.push(key + '{}')
    } else {
      const text = String(value)
      parts.push(key + '=' + (text.length > 28 ? text.slice(0, 27) + '...' : text))
    }
  }
  return parts.join('  ')
}

export function KindBadge({ entry }) {
  if (entry.kind === 'unknown') {
    return (
      <Badge tone="ai"
        title="This tool is not in the current tool catalogue, so what the call changed cannot be stated from the log alone."
      >
        <span className="gp-inferred">unknown</span>
      </Badge>
    )
  }
  if (entry.kind === 'read') {
    return <Badge tone="neutral" title="A read-only tool: it changed nothing.">read</Badge>
  }
  if (entry.destructive) {
    return (
      <Badge tone="danger"
        title="A destructive tool: it can delete, move or rename files in the library."
      >
        deletes
      </Badge>
    )
  }
  return (
    <Badge tone="brand" title="A writing tool: it changed the library but destroys nothing.">
      writes
    </Badge>
  )
}

export default function ActivityRow({ entry, selected, onSelect }) {
  const failed = entry.outcome === 'error'
  const classes = ['gp-row']
  if (failed) classes.push('gp-row--error')
  if (entry.kind === 'unknown') classes.push('gp-row--inferred')

  const precis = argumentPrecis(entry.arguments)

  return (
    <button
      type="button"
      className={classes.join(' ')}
      aria-selected={selected ? 'true' : undefined}
      title={failed
        ? 'Failed: ' + (entry.error_code || 'error') + '. Nothing was changed.'
        : 'Click for the full arguments this call was given.'}
      onClick={() => onSelect(entry)}
    >
      <span className="gp-row__cell gp-u-num gp-u-fs-10" style={{ minWidth: 104 }}>
        {dateTime(entry.ts)}
      </span>

      <KindBadge entry={entry} />

      {/* Fixed width: the tool name is the identity of the row and must never
          be the column that gives way when the argument precis is long. */}
      <span className="gp-row__name gp-u-truncate gp-u-num gp-u-fs-11"
        style={{ flex: '0 0 172px' }}
      >
        {entry.tool}
      </span>

      <span className="gp-row__cell gp-row__cell--grow gp-u-num gp-u-fs-10 gp-u-meta"
        title={precis}
      >
        {precis}
      </span>

      {failed ? (
        <Badge tone="danger" title={'Refused or failed: ' + (entry.error_code || 'error')}>
          {entry.error_code || 'failed'}
        </Badge>
      ) : entry.outcome === 'partial' ? (
        <Badge tone="warn" title="Some items in this call were applied and some were not.">
          partial
        </Badge>
      ) : (
        <span className="gp-row__cell gp-row__cell--num gp-u-local"
          title={fmtCount(entry.affected) + ' item(s) changed by this call'}
        >
          {entry.affected
            ? fmtCount(entry.affected) + (entry.affected === 1 ? ' item' : ' items')
            : 'ok'}
        </span>
      )}

      <span className="gp-row__cell gp-row__cell--num gp-u-fs-10" style={{ minWidth: 52 }}>
        {shortDuration(entry.elapsed_ms)}
      </span>
    </button>
  )
}
