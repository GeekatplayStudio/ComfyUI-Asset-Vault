import React, { useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import Modal from './Modal.jsx'
import Button from './Button.jsx'

/*
 * ConfirmDialog - names the exact blast radius before anything destructive:
 * how many items, how many bytes, and the affected filenames.
 */
export default function ConfirmDialog(props) {
  const {
    title, text, items, confirmLabel, danger, onConfirm, onCancel, extra, busyLabel
  } = props
  const [busy, setBusy] = useState(false)

  const run = async () => {
    setBusy(true)
    try {
      await onConfirm()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={title}
      size="sm"
      tone={danger ? 'danger' : undefined}
      onClose={busy ? undefined : onCancel}
      footer={(
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={run}
            loading={busy}
            disabled={busy}
          >
            {busy && busyLabel ? busyLabel : confirmLabel || 'Confirm'}
          </Button>
        </>
      )}
    >
      <div className={'gp-confirm' + (danger ? ' gp-confirm--danger' : '')}>
        <span className="gp-confirm__icon"><AlertTriangle aria-hidden="true" /></span>
        <div className="gp-confirm__body">
          <p className="gp-confirm__text">{text}</p>
          {items && items.length ? (
            <ul className="gp-confirm__list">
              {items.slice(0, 200).map((name, i) => (
                <li key={name + ':' + i}>{name}</li>
              ))}
            </ul>
          ) : null}
          {extra}
        </div>
      </div>
    </Modal>
  )
}
