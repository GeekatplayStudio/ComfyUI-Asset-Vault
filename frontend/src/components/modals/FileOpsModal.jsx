import React, { useState, useMemo, useCallback } from 'react'
import { AlertTriangle, Info } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Select from '../common/Select.jsx'
import ConfirmDialog from '../common/ConfirmDialog.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes, fileStem } from '../../services/format.js'

/*
 * FileOpsModal - rename, move and delete.
 *
 * Deletion is trash-backed by default and the toast carries an Undo that calls
 * /fileops/trash/restore with the ids the server just handed back. Permanent
 * deletion demands an explicit confirmation and states the blast radius first.
 */

export function RenameDialog({ item, onClose }) {
  const { toast, toastError, invalidate } = useVault()
  const [name, setName] = useState(fileStem(item.filename || item.name || ''))
  const [keepExt, setKeepExt] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await api.rename({
        uid: item.uid,
        new_name: name,
        keep_extension: keepExt,
        rename_sidecars: true
      })
      toast({
        tone: 'ok',
        title: 'Renamed',
        message: res.new_path
      })
      invalidate()
      onClose()
    } catch (err) {
      setError(err)
      if (!err.fieldErrors || !err.fieldErrors.length) toastError(err, 'Rename failed')
    } finally {
      setBusy(false)
    }
  }

  const fieldError = error ? (error.fieldError('new_name') || error.message) : null

  return (
    <Modal
      title="Rename"
      subtitle={item.filename || item.name}
      size="sm"
      onClose={onClose}
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={submit} loading={busy}
            disabled={busy || !name.trim()}>Rename</Button>
        </>
      )}
    >
      <form onSubmit={submit}>
        <div className="gp-field">
          <label className="gp-field__label" htmlFor="rename-input">
            New name <span className="gp-field__req">*</span>
          </label>
          <input
            id="rename-input"
            className={'gp-input gp-input--mono' + (fieldError ? ' gp-input--invalid' : '')}
            value={name}
            aria-invalid={fieldError ? 'true' : undefined}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
          {fieldError ? (
            <span className="gp-field__error">
              <AlertTriangle aria-hidden="true" /> {fieldError}
            </span>
          ) : (
            <span className="gp-field__hint">
              The extension is kept. Sidecar files with the same stem are renamed with it.
            </span>
          )}
        </div>
        <label className="gp-check gp-u-mt-5">
          <input className="gp-check__input" type="checkbox" checked={keepExt}
            onChange={(e) => setKeepExt(e.target.checked)} />
          <span className="gp-check__box">
            <svg viewBox="0 0 12 12" aria-hidden="true">
              <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
            </svg>
          </span>
          <span className="gp-check__label">Keep the original extension</span>
        </label>
      </form>
    </Modal>
  )
}

export function MoveDialog({ items, scope, onClose }) {
  const { state, toast, toastError, invalidate } = useVault()
  const roots = (state.config && state.config.roots) || []
  const [rootId, setRootId] = useState(String(items[0] && items[0].root_id ? items[0].root_id : (roots[0] && roots[0].id) || 1))
  const [folder, setFolder] = useState('')
  const [conflict, setConflict] = useState('fail')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const folderOptions = useResource(
    scope === 'models' ? 'move-folders:models' : null,
    (s) => api.modelGroups({ group: 'folder' }, s)
  )

  const suggestions = useMemo(() => {
    const nodes = (folderOptions.data && folderOptions.data.nodes) || []
    const out = []
    const walk = (list) => {
      for (const n of list) {
        out.push(n.key)
        if (n.children) walk(n.children)
      }
    }
    walk(nodes)
    return out
  }, [folderOptions.data])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.move({
        uids: items.map((i) => i.uid),
        target_root_id: Number(rootId),
        target_folder: folder,
        create_missing: true,
        on_conflict: conflict
      })
      const failed = (res.results || []).filter((r) => !r.ok)
      toast({
        tone: failed.length ? 'warn' : 'ok',
        title: failed.length ? 'Moved with problems' : 'Moved',
        message: res.moved + ' moved, ' + res.skipped + ' skipped, ' + res.failed + ' failed'
      })
      invalidate()
      onClose()
    } catch (err) {
      setError(err)
      toastError(err, 'Move failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={'Move ' + items.length + ' asset' + (items.length === 1 ? '' : 's')}
      subtitle="The destination must sit inside a configured root."
      onClose={onClose}
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={submit} loading={busy} disabled={busy}>Move</Button>
        </>
      )}
    >
      <div className="gp-formgrid">
        <label className="gp-formgrid__label" htmlFor="move-root">Root</label>
        <Select
          id="move-root"
          value={rootId}
          onChange={setRootId}
          ariaLabel="Destination root"
          options={roots.map((r) => ({ value: String(r.id), label: r.label + ' - ' + r.path }))}
        />

        <label className="gp-formgrid__label" htmlFor="move-folder">Folder</label>
        <div className="gp-field">
          <input id="move-folder" className="gp-input gp-input--mono" value={folder}
            list="move-folder-list"
            placeholder="checkpoints\flux"
            onChange={(e) => setFolder(e.target.value)} />
          <datalist id="move-folder-list">
            {suggestions.map((s) => <option key={s} value={s} />)}
          </datalist>
          <span className="gp-field__hint">
            Relative to the root. Missing folders are created.
          </span>
        </div>

        <label className="gp-formgrid__label" htmlFor="move-conflict">If it exists</label>
        <Select
          id="move-conflict"
          value={conflict}
          onChange={setConflict}
          ariaLabel="Conflict behaviour"
          options={[
            { value: 'fail', label: 'Stop and report' },
            { value: 'skip', label: 'Skip that file' },
            { value: 'rename', label: 'Add a number to the name' }
          ]}
        />
      </div>

      <div className="gp-callout gp-callout--info gp-u-mt-6">
        <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
        <div className="gp-callout__body">
          {items.length} file{items.length === 1 ? '' : 's'},{' '}
          {bytes(items.reduce((a, i) => a + (i.size || 0), 0))} in total. A move across drives is
          copied, verified and then removed, with progress on the index stream.
        </div>
      </div>

      {error ? (
        <div className="gp-callout gp-callout--danger gp-u-mt-5">
          <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
          <div className="gp-callout__body">{error.message}</div>
        </div>
      ) : null}
    </Modal>
  )
}

export function DeleteDialog({ items, onClose, onCompleted }) {
  const { state, toast, toastError, invalidate } = useVault()
  const defaultMode = (state.config && state.config.trash_mode) || 'trash'
  const [permanent, setPermanent] = useState(defaultMode === 'permanent')

  const totalBytes = items.reduce((a, i) => a + (i.size || 0), 0)

  const run = useCallback(async () => {
    try {
      const res = await api.remove({
        uids: items.map((i) => i.uid),
        mode: permanent ? 'permanent' : 'trash',
        confirm: true
      })
      const trashIds = res.trash_ids || []
      toast({
        tone: permanent ? 'warn' : 'ok',
        title: permanent ? 'Deleted permanently' : 'Moved to trash',
        message: res.deleted + ' item(s), ' + bytes(res.freed_bytes) + ' freed',
        action: !permanent && trashIds.length ? {
          label: 'Undo',
          run: async () => {
            try {
              await api.trashRestore({ ids: trashIds, on_conflict: 'rename' })
              toast({ tone: 'ok', title: 'Restored' })
              invalidate()
            } catch (err) {
              toastError(err, 'Restore failed')
            }
          }
        } : null
      })
      // Drop the deleted rows from the selection before anything can ask the
      // API for a uid that no longer exists.
      if (onCompleted) onCompleted(items.map((i) => i.uid))
      invalidate()
      onClose()
    } catch (err) {
      toastError(err, 'Delete failed')
    }
  }, [items, permanent, toast, toastError, invalidate, onClose, onCompleted])

  return (
    <ConfirmDialog
      danger={permanent}
      title={permanent ? 'Delete permanently' : 'Move to trash'}
      confirmLabel={permanent ? 'Delete permanently' : 'Move to trash'}
      busyLabel="Working"
      onCancel={onClose}
      onConfirm={run}
      text={permanent
        ? 'These ' + items.length + ' file(s), ' + bytes(totalBytes) +
          ' in total, will be removed from disk. This cannot be undone.'
        : 'These ' + items.length + ' file(s), ' + bytes(totalBytes) +
          ' in total, move to the vault trash and can be restored from Settings.'}
      items={items.map((i) => i.filename || i.name || i.uid)}
      extra={(
        <label className="gp-check gp-u-mt-5">
          <input className="gp-check__input" type="checkbox" checked={permanent}
            onChange={(e) => setPermanent(e.target.checked)} />
          <span className="gp-check__box">
            <svg viewBox="0 0 12 12" aria-hidden="true">
              <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
            </svg>
          </span>
          <span className="gp-check__label">Delete permanently instead of using the trash</span>
        </label>
      )}
    />
  )
}

/** Dispatcher used by the shell so one modal slot covers all three operations. */
export default function FileOpsModal({ op, items, scope, onClose, onCompleted }) {
  if (!items || !items.length) return null
  if (op === 'rename') return <RenameDialog item={items[0]} onClose={onClose} />
  if (op === 'move') return <MoveDialog items={items} scope={scope} onClose={onClose} />
  if (op === 'delete') {
    return <DeleteDialog items={items} onClose={onClose} onCompleted={onCompleted} />
  }
  return null
}
