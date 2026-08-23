import React from 'react'

/*
 * MetaRow - signature element #4: key ... value with a dotted leader, in the
 * tabular monospace face.
 *
 * An unresolved value renders as an em dash carrying the reason in its title -
 * exactly what provenance.{field}.reason exists for. It never prints a raw
 * link tuple and never throws.
 */
export default function MetaRow(props) {
  const {
    label, value, num, tone, inferred, inferredTitle, wrap, title, empty
  } = props

  const missing = value === null || value === undefined || value === ''
  const classes = ['gp-meta__val']
  if (num) classes.push('gp-meta__val--num')
  if (missing) classes.push('gp-meta__val--empty')
  else if (inferred) classes.push('gp-meta__val--inferred')
  else if (tone) classes.push('gp-meta__val--' + tone)

  return (
    <div className={'gp-meta__row' + (wrap ? ' gp-meta__row--wrap' : '')}>
      <span className="gp-meta__key">{label}</span>
      <span className="gp-meta__leader" />
      <span className={classes.join(' ')} title={missing ? (empty || 'Not recorded') : title}>
        {missing ? '—' : (
          inferred
            ? <span className="gp-inferred" title={inferredTitle || 'Inferred value'}>{value}</span>
            : value
        )}
      </span>
    </div>
  )
}

/** A titled block inside the details body. */
export function Section({ title, aside, children }) {
  return (
    <section className="gp-details__section">
      <div className="gp-details__section-head">
        <span>{title}</span>
        {aside}
      </div>
      {children}
    </section>
  )
}

/** Provenance-aware row: renders the reason when the backend could not resolve. */
export function ProvenanceRow({ label, value, provenance, num }) {
  const p = provenance || null
  const unresolved = p && p.resolved === false
  const reasonText = p
    ? (p.reason
      ? 'Unresolved: ' + p.reason
      : 'Origin ' + (p.origin || 'unknown') +
        (p.source_node_id ? ' from node ' + p.source_node_id : ''))
    : null
  return (
    <MetaRow
      label={label}
      value={unresolved ? null : value}
      num={num}
      title={reasonText}
      empty={reasonText || 'Not recorded'}
    />
  )
}

/** Header + body scaffold for the loading and error states, so the panel keeps
    its three-row grid shape while a record is on its way. */
export function DetailsFallback({ eyebrow, title, children }) {
  return (
    <>
      <div className="gp-details__header">
        <div className="gp-details__eyebrow">{eyebrow}</div>
        <h2 className="gp-details__title">{title}</h2>
      </div>
      <div className="gp-details__body">{children}</div>
    </>
  )
}
