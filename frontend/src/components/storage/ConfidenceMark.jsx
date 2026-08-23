import React from 'react'
import Badge from '../common/Badge.jsx'

/*
 * ConfidenceMark / ReasonBadge - the amber-violet convention for §18.
 *
 * Every reclaim group and every candidate reason carries
 * confidence: "measured" | "inferred". They are never merged into one badge:
 *
 *   measured  amber   the index holds the fact - "0 references", a byte count,
 *                     a timestamp. Nothing was guessed.
 *   inferred  violet  a judgement - "the name and size match, so this is
 *                     probably a duplicate". Carries the "~" marker and a
 *                     mandatory title naming what the guess rests on.
 *
 * The marker is suppressed inside the badge whose own text is the word
 * "inferred" (DESIGN_SYSTEM §2, .gp-inferred--nomark); everywhere the reason
 * text itself is shown, the tilde stays.
 */

const WHY = {
  unused: 'Counted from the index: no workflow references it and no output was generated with it.',
  duplicate: 'Two files share a name and a byte count. That is a candidate, not proof - hash the group to be certain.',
  superseded: 'A file with a similar name and a higher version number sits beside it.',
  stale: 'Read from the filesystem timestamp.',
  large: 'Read from the file size.',
  integrity: 'The header could not be parsed as a model.',
  orphan_output: 'The model this output names is no longer in the index.',
  non_media: 'The extension is neither an image nor a video.',
  no_provenance: 'No workflow graph was embedded in the file.',
  protected: 'You marked this a favourite or rated it 4 or more.'
}

export function reasonTitle(reason) {
  if (!reason) return undefined
  const base = WHY[reason.code] || null
  if (reason.confidence === 'inferred') {
    return 'Inferred' + (reason.method ? ' by ' + reason.method : '') + '. ' +
      (base || 'A judgement, not a measurement.')
  }
  return base || 'Measured from the index.'
}

/** One reason on a candidate row. */
export function ReasonBadge({ reason }) {
  if (!reason) return null
  const inferred = reason.confidence === 'inferred'
  const title = reasonTitle(reason)
  return (
    <Badge tone={inferred ? 'ai' : 'brand'} title={title}>
      {inferred
        ? <span className="gp-inferred" title={title}>{reason.label}</span>
        : reason.label}
    </Badge>
  )
}

/** The confidence of a whole reclaim group or duplicate set. */
export default function ConfidenceMark({ confidence, reason, method, group }) {
  if (!confidence) return null
  const inferred = confidence === 'inferred'
  const title = inferred
    ? 'Inferred' + (method ? ' by ' + method : '') + '. ' +
      (WHY[reason] || 'A judgement the vault made; confirm it before deleting.')
    : 'Measured. ' + (WHY[reason] || 'Read straight out of the index.')

  return (
    <span className="gp-u-row gp-u-gap-3">
      <Badge tone={inferred ? 'ai' : 'brand'} title={title}>
        {inferred
          ? <span className="gp-inferred gp-inferred--nomark">inferred</span>
          : 'measured'}
      </Badge>
      {group && group.exact_count === 0 && inferred ? (
        <span className="gp-u-fs-10 gp-u-meta">no exact match yet</span>
      ) : null}
    </span>
  )
}
