import React from 'react'
import { count as fmtCount, bytes as fmtBytes } from '../../services/format.js'

/**
 * GroupHeader - the rule-and-label divider between groups. The count is the
 * group's total across the whole filtered set, not just the loaded page.
 */
export default function GroupHeader({ label, count, bytes, shown }) {
  return (
    <div className="gp-group-head">
      <span className="gp-group-head__label">{label || 'Ungrouped'}</span>
      {count !== undefined && count !== null ? (
        <span className="gp-group-head__count">
          {shown !== undefined && shown !== null && shown < count
            ? fmtCount(shown) + ' of ' + fmtCount(count)
            : fmtCount(count)}
        </span>
      ) : null}
      {bytes ? <span className="gp-group-head__bytes">{fmtBytes(bytes)}</span> : null}
      <span className="gp-group-head__rule" />
    </div>
  )
}
