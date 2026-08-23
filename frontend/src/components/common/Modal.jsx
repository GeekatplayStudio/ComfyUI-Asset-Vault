import React, { useEffect, useRef, useCallback } from 'react'
import { X } from 'lucide-react'

/*
 * Modal - .gp-overlay + .gp-modal. Focus is trapped while open and restored to
 * whatever had it before, which is what Esc-to-close depends on.
 */
export default function Modal(props) {
  const {
    title, subtitle, onClose, children, footer, footerLeft,
    size, tone, labelledBy, closeLabel
  } = props
  const modalRef = useRef(null)
  const restoreRef = useRef(null)

  useEffect(() => {
    restoreRef.current = document.activeElement
    const node = modalRef.current
    if (node) {
      const focusable = node.querySelector(
        'input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )
      if (focusable) focusable.focus()
      else node.focus()
    }
    return () => {
      const prev = restoreRef.current
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) prev.focus()
    }
  }, [])

  const onKeyDown = useCallback((event) => {
    if (event.key === 'Escape') {
      event.stopPropagation()
      if (onClose) onClose()
      return
    }
    if (event.key !== 'Tab') return
    const node = modalRef.current
    if (!node) return
    const items = Array.from(node.querySelectorAll(
      'input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    )).filter((el) => el.offsetParent !== null || el === document.activeElement)
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }, [onClose])

  const classes = ['gp-modal']
  if (size === 'sm') classes.push('gp-modal--sm')
  if (size === 'lg') classes.push('gp-modal--lg')
  if (tone === 'ai') classes.push('gp-modal--ai')
  if (tone === 'danger') classes.push('gp-modal--danger')

  return (
    <div
      className="gp-overlay"
      onMouseDown={(e) => { if (e.target === e.currentTarget && onClose) onClose() }}
    >
      <div
        ref={modalRef}
        className={classes.join(' ')}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : title}
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="gp-modal__header">
          <div className="gp-modal__titles">
            <h2 className="gp-modal__title">{title}</h2>
            {subtitle ? <div className="gp-modal__sub">{subtitle}</div> : null}
          </div>
          {onClose ? (
            <button
              type="button"
              className="gp-modal__close"
              aria-label={closeLabel || 'Close'}
              onClick={onClose}
            >
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <div className="gp-modal__body">{children}</div>
        {footer || footerLeft ? (
          <div className="gp-modal__footer">
            {footerLeft ? <div className="gp-modal__footer-left">{footerLeft}</div> : null}
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  )
}
