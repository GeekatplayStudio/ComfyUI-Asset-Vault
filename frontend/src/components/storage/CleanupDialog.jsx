import React, { useCallback, useMemo } from 'react'
import { ShieldQuestion, Star } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import ConfirmDialog from '../common/ConfirmDialog.jsx'
import Badge from '../common/Badge.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes as fmtBytes, count as fmtCount, humanise } from '../../services/format.js'
import { BATCH_CAP } from './CandidatesPanel.jsx'

/*
 * CleanupDialog - the confirmation in front of /storage/cleanup.
 *
 * The byte total is read back from /storage/estimate rather than summed in the
 * client, because that is the number the contract says the dialog must show:
 * priced from the index, before anything is touched.
 *
 * Trash-backed is the default and its toast carries the same Undo the delete
 * button elsewhere in the app uses. Permanent deletion sends confirm:true and
 * says plainly that nothing comes back.
 */
export default function CleanupDialog(props) {
  const { uids, mode, names, wholeRoleUids, onClose, onCompleted } = props
  const { toast, toastError, invalidate } = useVault()
  const permanent = mode === 'permanent'

  const estimate = useResource(
    uids.length ? 'storage:estimate:' + uids.join(',') : null,
    (s) => api.storageEstimate(uids, s)
  )

  const est = estimate.data
  const flagged = useMemo(
    () => uids.filter((u) => wholeRoleUids && wholeRoleUids.has(u)).length,
    [uids, wholeRoleUids]
  )

  const run = useCallback(async () => {
    try {
      const res = await api.storageCleanup({
        uids,
        mode: permanent ? 'permanent' : 'trash',
        confirm: true
      })
      const trashIds = res.trash_ids || []
      toast({
        tone: permanent ? 'warn' : 'ok',
        title: permanent ? 'Deleted permanently' : 'Moved to trash',
        message: fmtCount(res.deleted) + ' of ' + fmtCount(res.requested) + ' file(s), ' +
          fmtBytes(res.freed_bytes) + ' freed' +
          (res.failed ? ' - ' + fmtCount(res.failed) + ' failed' : ''),
        action: !permanent && trashIds.length ? {
          label: 'Undo',
          run: async () => {
            try {
              await api.trashRestore({ ids: trashIds, on_conflict: 'rename' })
              toast({ tone: 'ok', title: 'Restored from trash' })
              invalidate()
            } catch (err) {
              toastError(err, 'Restore failed')
            }
          }
        } : null
      })
      if (onCompleted) onCompleted(uids)
      invalidate()
      onClose()
    } catch (err) {
      toastError(err, 'Cleanup failed')
    }
  }, [uids, permanent, toast, toastError, invalidate, onClose, onCompleted])

  const total = est ? est.bytes : null
  const sizeText = total === null ? 'Pricing the selection' : fmtBytes(total)

  return (
    <ConfirmDialog
      danger={permanent}
      title={permanent
        ? 'Delete ' + fmtCount(uids.length) + ' file(s) permanently'
        : 'Move ' + fmtCount(uids.length) + ' file(s) to the trash'}
      confirmLabel={permanent ? 'Delete permanently' : 'Move to trash'}
      busyLabel="Working"
      onCancel={onClose}
      onConfirm={run}
      text={permanent
        ? sizeText + ' will be removed from disk. Nothing is copied to the trash first and ' +
          'nothing can be restored. Models this large can take hours to download again.'
        : sizeText + ' moves to the vault trash. Everything stays restorable until you empty it, ' +
          'and the notification that follows carries an Undo.'}
      items={names}
      extra={(
        <>
          {est ? (
            <div className="gp-meta gp-u-mt-5">
              <div className="gp-meta__row">
                <span className="gp-meta__key">Priced from the index</span>
                <span className="gp-meta__leader" />
                <span className="gp-meta__val gp-meta__val--num">{fmtBytes(est.bytes)}</span>
              </div>
              <div className="gp-meta__row">
                <span className="gp-meta__key">Resolved</span>
                <span className="gp-meta__leader" />
                <span className="gp-meta__val gp-meta__val--num">
                  {fmtCount(est.resolved)} of {fmtCount(est.requested)}
                  {Object.entries(est.by_kind || {}).length
                    ? ' - ' + Object.entries(est.by_kind)
                      .map(([k, v]) => fmtCount(v) + ' ' + humanise(k).toLowerCase())
                      .join(', ')
                    : ''}
                </span>
              </div>
              {est.unknown_uids && est.unknown_uids.length ? (
                <div className="gp-meta__row">
                  <span className="gp-meta__key">Not found</span>
                  <span className="gp-meta__leader" />
                  <span className="gp-meta__val gp-meta__val--danger gp-meta__val--num">
                    {fmtCount(est.unknown_uids.length)}
                  </span>
                </div>
              ) : null}
            </div>
          ) : null}

          {est && est.protected_count ? (
            <div className="gp-callout gp-callout--warn gp-u-mt-5">
              <span className="gp-callout__icon"><Star aria-hidden="true" /></span>
              <div className="gp-callout__body">
                <div className="gp-callout__title">
                  {fmtCount(est.protected_count)} of these you marked as keepers
                </div>
                They are favourites or rated 4 and above. Nothing stops you, but they were
                singled out for a reason.
              </div>
            </div>
          ) : null}

          {flagged ? (
            <div className="gp-callout gp-callout--ai gp-u-mt-5">
              <span className="gp-callout__icon"><ShieldQuestion aria-hidden="true" /></span>
              <div className="gp-callout__body">
                <div className="gp-callout__title">
                  <span className="gp-inferred"
                    title="Every model of these roles shows zero references">
                    {fmtCount(flagged)} come from a role that is 100% unreferenced
                  </span>
                </div>
                When an entire role reports zero references, the usual cause is that the
                indexer cannot read that role out of a workflow graph - not that you stopped
                using it. Verify these before they go.
              </div>
            </div>
          ) : null}

          {uids.length > BATCH_CAP ? (
            <div className="gp-callout gp-callout--danger gp-u-mt-5">
              <span className="gp-callout__icon"><Badge tone="danger">!</Badge></span>
              <div className="gp-callout__body">
                One cleanup action covers at most {BATCH_CAP} files. Narrow the selection.
              </div>
            </div>
          ) : null}
        </>
      )}
    />
  )
}
