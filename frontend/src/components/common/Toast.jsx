import React, { useEffect } from 'react'
import { X, CheckCircle2, AlertTriangle, XCircle, Info, Sparkles } from 'lucide-react'
import Button from './Button.jsx'

const ICONS = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  danger: XCircle,
  info: Info,
  ai: Sparkles
}

function Toast({ toast, onDismiss }) {
  const Icon = ICONS[toast.tone] || Info
  const sticky = toast.sticky || toast.tone === 'danger'

  useEffect(() => {
    if (sticky) return undefined
    const timer = setTimeout(() => onDismiss(toast.id), toast.duration || 6000)
    return () => clearTimeout(timer)
  }, [toast.id, toast.duration, sticky, onDismiss])

  return (
    <div
      className={'gp-toast' + (toast.tone ? ' gp-toast--' + toast.tone : '')}
      role={toast.tone === 'danger' ? 'alert' : 'status'}
    >
      <Icon className="gp-toast__icon" aria-hidden="true" />
      <div className="gp-toast__body">
        <div className="gp-toast__title">{toast.title}</div>
        {toast.message ? <div className="gp-toast__msg">{toast.message}</div> : null}
        {toast.detail ? <div className="gp-toast__msg gp-u-dim">{toast.detail}</div> : null}
        {toast.action ? (
          <div className="gp-toast__actions">
            <Button
              size="sm"
              onClick={() => { toast.action.run(); onDismiss(toast.id) }}
            >
              {toast.action.label}
            </Button>
          </div>
        ) : null}
      </div>
      <button
        type="button"
        className="gp-toast__close"
        aria-label="Dismiss notification"
        onClick={() => onDismiss(toast.id)}
      >
        <X size={13} aria-hidden="true" />
      </button>
    </div>
  )
}

export default function Toaster({ toasts, onDismiss }) {
  if (!toasts.length) return null
  return (
    <div className="gp-toaster" aria-live="polite">
      {toasts.map((toast) => (
        <Toast key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  )
}
