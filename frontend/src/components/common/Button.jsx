import React from 'react'

/*
 * Button - .gp-btn and its documented modifiers.
 * `disabled` is expected to come from the API's `actions` block, which the
 * contract declares authoritative; the client never re-derives those rules.
 */

const VARIANTS = {
  default: '',
  primary: 'gp-btn--primary',
  ai: 'gp-btn--ai',
  ghost: 'gp-btn--ghost',
  danger: 'gp-btn--danger',
  dangerGhost: 'gp-btn--danger-ghost'
}

export default function Button(props) {
  const {
    variant = 'default', size, icon: Icon, label, children, count,
    iconOnly, loading, className, type = 'button', ...rest
  } = props

  const classes = ['gp-btn']
  if (VARIANTS[variant]) classes.push(VARIANTS[variant])
  if (size === 'sm') classes.push('gp-btn--sm')
  if (size === 'lg') classes.push('gp-btn--lg')
  if (size === 'block') classes.push('gp-btn--block')
  if (iconOnly) classes.push('gp-btn--icon')
  if (loading) classes.push('is-loading')
  if (className) classes.push(className)

  const text = label !== undefined ? label : children

  return (
    <button type={type} className={classes.join(' ')} {...rest}>
      {Icon ? <Icon className="gp-btn__icon" aria-hidden="true" /> : null}
      {!iconOnly && text !== undefined && text !== null
        ? <span className="gp-btn__label">{text}</span>
        : null}
      {count !== undefined && count !== null
        ? <span className="gp-btn__count">{count}</span>
        : null}
    </button>
  )
}
