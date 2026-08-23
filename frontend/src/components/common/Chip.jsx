import React from 'react'
import { X } from 'lucide-react'
import { count as fmtCount } from '../../services/format.js'

/*
 * Chip - facet values and active filter pills. A facet always carries a count;
 * a zero-count facet is never rendered (the caller filters those out).
 */
export default function Chip(props) {
  const {
    label, count, selected, tone, mono, small, onRemove, onClick, title, ...rest
  } = props
  const classes = ['gp-chip']
  if (selected) classes.push('gp-chip--selected')
  if (tone) classes.push('gp-chip--' + tone)
  if (mono) classes.push('gp-chip--mono')
  if (small) classes.push('gp-chip--sm')

  const body = (
    <>
      <span>{label}</span>
      {count !== undefined && count !== null
        ? <span className="gp-chip__count">{fmtCount(count)}</span>
        : null}
      {onRemove ? (
        <span
          className="gp-chip__remove"
          role="button"
          tabIndex={-1}
          aria-label={'Remove filter ' + label}
          onClick={(e) => { e.stopPropagation(); onRemove() }}
        >
          <X size={10} aria-hidden="true" />
        </span>
      ) : null}
    </>
  )

  if (!onClick && !onRemove) {
    return <span className={classes.join(' ')} title={title} {...rest}>{body}</span>
  }
  return (
    <button
      type="button"
      className={classes.join(' ')}
      aria-pressed={selected ? 'true' : undefined}
      onClick={onClick}
      title={title}
      {...rest}
    >
      {body}
    </button>
  )
}
