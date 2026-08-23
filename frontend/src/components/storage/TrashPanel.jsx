import React, { useState, useCallback, useMemo } from 'react'
import { Trash2, Undo2, AlertTriangle, CheckCircle2 } from 'lucide-react'
import api from '../../services/api.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import ConfirmDialog from '../common/ConfirmDialog.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes as fmtBytes, count as fmtCount, dateTime, humanise } from '../../services/format.js'

/*
 * TrashPanel - what the trash itself is holding (C10.4).
 *
 * The point of a trash-backed default is that it is reversible, and that
 * promise is only real if the owner can see what is in there and get it back.
 * Emptying is the one action here that is not reversible, so it takes the same
 * explicit confirmation as a permanent delete.
 */
export default function TrashPanel({ data, summary, loading, error, onRefresh }) {
  const { toast, toastError, invalidate } = useVault()
  const [selection, setSelection] = useState([])
  const [emptyRequest, setEmptyRequest] = useState(null)
  const [busy, setBusy] = useState(false)

  const items = (data && data.items) || []
  const totals = (data && data.summary) || summary || { count: 0, bytes: 0 }
  const selected = useMemo(() => new Set(selection), [selection])

  const selectedBytes = useMemo(
    () => items.filter((i) => selected.has(i.id)).reduce((a, i) => a + (i.size || 0), 0),
    [items, selected]
  )

  const toggle = useCallback((id) => {
    setSelection((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }, [])

  const restore = useCallback(async (ids) => {
    setBusy(true)
    try {
      const res = await api.trashRestore({ ids, on_conflict: 'rename' })
      toast({
        tone: 'ok',
        title: 'Restored',
        message: fmtCount(res.restored !== undefined ? res.restored : ids.length) +
          ' item(s) put back'
      })
      setSelection([])
      invalidate()
      onRefresh()
    } catch (err) {
      toastError(err, 'Restore failed')
    } finally {
      setBusy(false)
    }
  }, [toast, toastError, invalidate, onRefresh])

  const empty = useCallback(async (ids) => {
    try {
      const res = await api.trashEmpty({ ids: ids || null, confirm: true })
      toast({
        tone: 'warn',
        title: 'Trash emptied',
        message: fmtCount(res.removed !== undefined ? res.removed : (ids ? ids.length : 0)) +
          ' item(s) gone, ' + fmtBytes(res.freed_bytes) + ' freed'
      })
      setSelection([])
      setEmptyRequest(null)
      invalidate()
      onRefresh()
    } catch (err) {
      toastError(err, 'Could not empty the trash')
    }
  }, [toast, toastError, invalidate, onRefresh])

  if (error) {
    return (
      <>
      <div className="gp-toolbar">
        <span className="gp-toolbar__label">Trash</span>
      </div>
      <div className="gp-facetbar gp-facetbar--empty" />
      <div className="gp-main__body">
        <EmptyState tone="error" icon={AlertTriangle} title="Could not read the trash"
          text={error.message}
          actions={<Button variant="primary" onClick={onRefresh}>Try again</Button>} />
      </div>
      </>
    )
  }

  return (
    <>
      <div className="gp-toolbar">
        <div className="gp-toolbar__group">
          <span className="gp-toolbar__label">Holding</span>
          <span className="gp-u-num gp-u-fw-600">{fmtCount(totals.count)}</span>
          <span className="gp-u-fs-11 gp-u-meta">items</span>
          <span className="gp-u-num gp-u-fw-600">{fmtBytes(totals.bytes)}</span>
          {summary && summary.retention_days ? (
            <span className="gp-u-fs-11 gp-u-meta">
              purged after {fmtCount(summary.retention_days)} days
            </span>
          ) : null}
        </div>

        <div className="gp-toolbar__group">
          <Button size="sm" icon={Undo2} label="Restore"
            count={selection.length ? fmtCount(selection.length) : undefined}
            disabled={!selection.length || busy}
            title={selection.length
              ? 'Put ' + selection.length + ' item(s) back where they came from'
              : 'Select items to restore them'}
            onClick={() => restore(selection)} />
        </div>

        <div className="gp-toolbar__spacer" />
        <span className="gp-divider gp-divider--v" />
        <div className="gp-toolbar__group">
          <Button size="sm" variant="danger" icon={Trash2}
            label={selection.length ? 'Delete selected for good' : 'Empty the trash'}
            disabled={!items.length}
            title={selection.length
              ? 'Permanently remove ' + selection.length + ' item(s), ' + fmtBytes(selectedBytes)
              : 'Permanently remove everything in the trash'}
            onClick={() => setEmptyRequest({ ids: selection.length ? selection : null })} />
        </div>
      </div>

      <div className="gp-facetbar">
        <span className="gp-toolbar__label">Trash folders</span>
        {((summary && summary.directories) || []).map((d) => (
          <span className="gp-chip gp-chip--mono gp-chip--sm" key={d} title={d}>{d}</span>
        ))}
      </div>

      <div className="gp-main__body">
        {loading && !items.length ? <SkeletonRows rows={6} /> : null}

        {!loading && !items.length ? (
          <EmptyState
            icon={CheckCircle2}
            title="The trash is empty"
            text={'Deleting from the vault puts files here first and keeps them for ' +
              fmtCount((summary && summary.retention_days) || 30) +
              ' days, so a mistake is a click away from being undone.'}
          />
        ) : null}

        {items.length ? (
          <table className="gp-table gp-u-w-full">
            <thead>
              <tr>
                <th />
                <th>File</th>
                <th>Came from</th>
                <th>Deleted</th>
                <th className="gp-table__num">Size</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} aria-selected={selected.has(item.id)}>
                  <td>
                    <label className="gp-check" title="Select this item">
                      <input className="gp-check__input" type="checkbox"
                        checked={selected.has(item.id)}
                        aria-label={'Select ' + (item.name || item.original_path)}
                        onChange={() => toggle(item.id)} />
                      <span className="gp-check__box">
                        <svg viewBox="0 0 12 12" aria-hidden="true">
                          <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor"
                            strokeWidth="1.8" />
                        </svg>
                      </span>
                    </label>
                  </td>
                  <td className="gp-u-fg">
                    {item.name || item.filename}
                    {item.kind ? <Badge tone="neutral">{humanise(item.kind)}</Badge> : null}
                  </td>
                  <td className="gp-u-fs-11 gp-u-meta gp-u-break-all">
                    {item.original_path || item.rel_path}
                  </td>
                  <td className="gp-u-fs-11 gp-u-num">{dateTime(item.deleted_at)}</td>
                  <td className="gp-table__num gp-u-num">{fmtBytes(item.size)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>

      {emptyRequest ? (
        <ConfirmDialog
          danger
          title={emptyRequest.ids
            ? 'Delete ' + fmtCount(emptyRequest.ids.length) + ' item(s) for good'
            : 'Empty the trash'}
          confirmLabel={emptyRequest.ids ? 'Delete for good' : 'Empty the trash'}
          busyLabel="Working"
          onCancel={() => setEmptyRequest(null)}
          onConfirm={() => empty(emptyRequest.ids)}
          text={emptyRequest.ids
            ? fmtBytes(selectedBytes) + ' will be removed from disk. This is the last copy: ' +
              'once the trash lets go, nothing can restore it.'
            : fmtBytes(totals.bytes) + ' across ' + fmtCount(totals.count) + ' item(s) will be ' +
              'removed from disk. This is the last copy: once the trash lets go, nothing can ' +
              'restore it.'}
          items={(emptyRequest.ids
            ? items.filter((i) => emptyRequest.ids.includes(i.id))
            : items).map((i) => i.name || i.filename || i.original_path)}
        />
      ) : null}
    </>
  )
}
