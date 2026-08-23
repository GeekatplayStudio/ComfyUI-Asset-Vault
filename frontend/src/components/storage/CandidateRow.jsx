import React from 'react'
import { Star, ShieldQuestion } from 'lucide-react'
import Badge from '../common/Badge.jsx'
import { ReasonBadge } from './ConfidenceMark.jsx'
import { bytes as fmtBytes, count as fmtCount, humanise } from '../../services/format.js'

/*
 * CandidateRow - one reclaim candidate as a .gp-row.
 *
 * Reading order matches the decision the owner is making: is it ticked, what
 * is it, why is it here, how sure are we, how big, how old.
 *
 * A row belonging to a role that is 100% unused gets the violet spine and its
 * own caution badge. The amber "0 references" badge stays exactly as it was -
 * that fact is still measured. What the violet says is that the fact may not
 * mean what it looks like.
 */

const MAX_REASONS = 2

/* These reasons already have a column of their own, so repeating them as a
   badge says the same thing twice and turns the table into noise. They stay in
   the score tooltip, the filter chips and the "+n" overflow. */
const HAS_ITS_OWN_COLUMN = { large: true, stale: true }

export default function CandidateRow(props) {
  const { item, checked, focused, wholeRoleGap, onSelect, onToggle } = props

  const reasons = item.reasons || []
  const substantive = reasons.filter((r) => !HAS_ITS_OWN_COLUMN[r.code])
  // Never leave "why it is listed" blank: if size or age is the only reason,
  // that reason is the answer.
  const ranked = substantive.length ? substantive : reasons
  const shown = ranked.slice(0, MAX_REASONS)
  const hidden = reasons.length - shown.length

  const classes = ['gp-row', 'gp-focus-inset']
  if (focused) classes.push('gp-row--selected')
  else if (wholeRoleGap) classes.push('gp-row--inferred')

  const gapTitle = wholeRoleGap
    ? 'Every model of this role shows zero references. An entire role at 100% is more ' +
      'likely a gap in what the indexer can read than genuine disuse - verify before deleting.'
    : undefined

  return (
    <div
      className={classes.join(' ')}
      role="option"
      aria-selected={focused ? 'true' : 'false'}
      tabIndex={-1}
      onClick={(event) => onSelect(item, event)}
    >
      <label
        className="gp-check gp-u-shrink0"
        onClick={(event) => event.stopPropagation()}
        title={checked ? 'Remove from the cleanup selection' : 'Add to the cleanup selection'}
      >
        <input
          className="gp-check__input"
          type="checkbox"
          checked={checked}
          aria-label={'Select ' + (item.filename || item.name)}
          onChange={() => onToggle(item)}
        />
        <span className="gp-check__box">
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          </svg>
        </span>
      </label>

      {/* One flexible cell only, so every column to its right lines up on the
          same axis from row to row. */}
      <span className="gp-row__name" title={item.abs_path}>
        {item.filename || item.name}
        <span className="gp-u-meta gp-u-fs-11">
          {'  '}{item.category || item.folder || humanise(item.kind)}
        </span>
      </span>

      <span className="gp-u-row gp-u-gap-2 gp-u-shrink0">
        {wholeRoleGap ? (
          <Badge tone="ai" title={gapTitle}>
            <ShieldQuestion aria-hidden="true" />
            <span className="gp-inferred" title={gapTitle}>whole role unreferenced</span>
          </Badge>
        ) : null}
        {item.protected ? (
          <Badge tone="warn" title="Favourite or rated 4+, so it scores -40 and is flagged, never hidden">
            <Star aria-hidden="true" />
            kept
          </Badge>
        ) : null}
        {shown.map((r) => <ReasonBadge key={r.code} reason={r} />)}
        {!shown.length && !wholeRoleGap && !item.protected ? (
          // The server flagged nothing: the row is here because the current
          // sort ranks everything, not because anything is wrong with it.
          <span className="gp-u-dim gp-u-fs-11"
            title={'Nothing is flagged against this file. It scores ' +
              item.reclaim_score + ' on size and age alone.'}
          >
            no flags
          </span>
        ) : null}
        {hidden > 0 ? (
          <Badge tone="neutral"
            title={reasons.filter((r) => !shown.includes(r))
              .map((r) => r.label).join(' / ')}>
            +{hidden}
          </Badge>
        ) : null}
      </span>

      <span className="gp-row__cell gp-row__cell--num gp-u-num"
        title={'Reclaim score ' + item.reclaim_score + ' of 100, from ' +
          reasons.map((r) => r.code + ' +' + r.weight).join(', ')}
      >
        {fmtCount(item.reclaim_score)}
      </span>
      <span className="gp-row__cell gp-row__cell--num gp-u-num gp-u-fg">
        {fmtBytes(item.size)}
      </span>
      <span className="gp-row__cell gp-row__cell--num gp-u-num"
        title={item.modified_at ? new Date(item.modified_at).toLocaleString() : undefined}
      >
        {fmtCount(item.age_days)}d
      </span>
    </div>
  )
}
