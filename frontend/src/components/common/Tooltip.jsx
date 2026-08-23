import React, { useState, useRef, useCallback } from 'react'

/*
 * Tooltip - a portal-free fixed-position bubble. It carries no interactive
 * content (the design system gives it pointer-events: none), so it is only used
 * to explain inferred values and detection signals.
 */
export default function Tooltip(props) {
  const { content, title, tone, children, className } = props
  const [pos, setPos] = useState(null)
  const ref = useRef(null)

  const show = useCallback(() => {
    const node = ref.current
    if (!node) return
    const rect = node.getBoundingClientRect()
    setPos({ left: Math.round(rect.left), top: Math.round(rect.bottom + 6) })
  }, [])

  const hide = useCallback(() => setPos(null), [])

  const classes = ['gp-tooltip']
  if (tone === 'ai') classes.push('gp-tooltip--ai')
  if (tone === 'danger') classes.push('gp-tooltip--danger')

  return (
    <>
      <span
        ref={ref}
        className={className}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
      >
        {children}
      </span>
      {pos ? (
        <span className={classes.join(' ')} style={{ left: pos.left, top: pos.top }} role="tooltip">
          {title ? <span className="gp-tooltip__title">{title}</span> : null}
          {content}
        </span>
      ) : null}
    </>
  )
}
