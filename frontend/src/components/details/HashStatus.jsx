import React, { useState } from 'react'
import { Hash, Copy, Check } from 'lucide-react'
import api from '../../services/api.js'
import Button from '../common/Button.jsx'
import Badge, { HashBadge } from '../common/Badge.jsx'
import MetaRow from './MetaRow.jsx'
import ProgressBar from '../common/ProgressBar.jsx'
import { bytes, duration } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * HashStatus - the hash state of one model plus the button that starts the job.
 *
 * Hashing never runs during a scan (DECISIONS C1); it is an explicit, scoped,
 * cancellable background job, and the UI states the cost before starting one.
 */
export default function HashStatus({ model, onDone }) {
  const { toast, toastError, state } = useVault()
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const hash = model.hash || {}
  const canHash = model.actions ? model.actions.can_hash : true
  const live = state.hashStatus
  const running = live && live.running
    ? live.running.find((r) => r.uid === model.uid)
    : null

  const start = async () => {
    setBusy(true)
    try {
      const res = await api.hashEnqueue({
        scope: 'ids',
        uids: [model.uid],
        priority: 9,
        skip_hashed: false
      })
      toast({
        tone: 'ok',
        title: 'Hash queued',
        message: bytes(res.bytes_total) + ' to read' +
          (res.eta_ms ? ', about ' + duration(res.eta_ms) : '')
      })
      if (onDone) onDone()
    } catch (err) {
      toastError(err, 'Could not queue the hash')
    } finally {
      setBusy(false)
    }
  }

  const copy = async () => {
    const value = hash.sha256 || hash.autov2
    if (!value || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch (err) {
      toast({ tone: 'warn', title: 'Clipboard unavailable', message: 'Copy the value manually.' })
    }
  }

  return (
    <>
      <div className="gp-u-row gp-u-gap-3 gp-u-mb-4 gp-u-wrap">
        <HashBadge state={hash.state} />
        {hash.autov2 ? <Badge tone="mono">AutoV2 {hash.autov2}</Badge> : null}
      </div>

      {running ? (
        <div className="gp-u-mb-4">
          <ProgressBar
            percent={running.percent}
            label="Reading"
            value={running.percent.toFixed(1) + '%'}
            sub={bytes(running.bytes_done) + ' of ' + bytes(running.size) +
              (running.mbps ? ' at ' + running.mbps.toFixed(0) + ' MB/s' : '')}
          />
        </div>
      ) : null}

      <div className="gp-meta gp-u-mb-4">
        <MetaRow label="state" value={hash.state} />
        <MetaRow label="AutoV2" value={hash.autov2} num
          empty="Not computed yet - start the hash to enable Civitai matching." />
        <MetaRow label="SHA-256" value={hash.sha256} wrap
          empty="Not computed yet." />
      </div>

      <div className="gp-u-row gp-u-gap-3">
        {hash.state !== 'done' ? (
          <Button
            size="sm"
            icon={Hash}
            label={hash.state === 'queued' || hash.state === 'hashing' ? 'Queued' : 'Compute hash'}
            loading={busy}
            disabled={!canHash || hash.state === 'queued' || hash.state === 'hashing'}
            title={canHash
              ? 'Read this file once and store its SHA-256 / AutoV2'
              : 'Hashing is not available for this file'}
            onClick={start}
          />
        ) : (
          <Button
            size="sm"
            icon={copied ? Check : Copy}
            label={copied ? 'Copied' : 'Copy hash'}
            onClick={copy}
          />
        )}
      </div>
    </>
  )
}
