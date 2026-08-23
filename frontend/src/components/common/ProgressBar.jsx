import React from 'react'

/*
 * ProgressBar - the value is written once as a custom property on the container.
 * Indeterminate is only used when the server sends no percentage.
 */
export default function ProgressBar(props) {
  const { percent, tone, thin, indeterminate, label, value, sub } = props
  const classes = ['gp-progress']
  if (tone === 'ai') classes.push('gp-progress--ai')
  if (tone === 'ok') classes.push('gp-progress--ok')
  if (tone === 'danger') classes.push('gp-progress--danger')
  if (thin) classes.push('gp-progress--thin')
  if (indeterminate) classes.push('gp-progress--indeterminate')

  const pct = indeterminate
    ? '40%'
    : Math.max(0, Math.min(100, Number(percent) || 0)).toFixed(1) + '%'

  const bar = (
    <div
      className={classes.join(' ')}
      style={{ '--gp-progress': pct }}
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : Math.round(Number(percent) || 0)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label || 'Progress'}
    >
      <div className="gp-progress__bar" />
    </div>
  )

  if (!label && !value && !sub) return bar

  return (
    <div className="gp-progress-block">
      <div className="gp-progress-block__head">
        <span>{label}</span>
        {value ? <span className="gp-progress-block__num">{value}</span> : null}
      </div>
      {bar}
      {sub ? <div className="gp-progress-block__sub">{sub}</div> : null}
    </div>
  )
}
