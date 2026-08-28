import React, { useState, useCallback, useMemo, useEffect } from 'react'
import { Hash, AlertTriangle, Clock } from 'lucide-react'
import api, { streamUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useEventSource from '../../hooks/useEventSource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Select from '../common/Select.jsx'
import Slider from '../common/Slider.jsx'
import ProgressBar from '../common/ProgressBar.jsx'
import Badge from '../common/Badge.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { bytes, duration, count as fmtCount, humanise } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * HashDialog - start, watch and cancel the hashing queue.
 *
 * Hashing reads every byte of every selected file, so the cost is stated up
 * front: the full library here is roughly 1.5 TB, which is about a 2.8 hour job.
 */
export default function HashDialog({ onClose, presetUids }) {
  const { state, dispatch, toast, toastError, invalidate } = useVault()
  const [scope, setScope] = useState(presetUids && presetUids.length ? 'ids' : 'unhashed')
  const [category, setCategory] = useState('checkpoints')
  const [concurrency, setConcurrency] = useState(
    (state.config && state.config.hash_concurrency) || 2
  )
  const [throttle, setThrottle] = useState((state.config && state.config.hash_throttle_mbps) || 0)
  const [busy, setBusy] = useState(false)

  const facets = useResource('hash-facets', (s) => api.modelFacets({}, s), { epoch: state.dataEpoch })
  const status = state.hashStatus

  const events = useMemo(() => ['hash_progress', 'hash_item', 'done', 'heartbeat'], [])
  // Coalesce bursts of progress events into at most one in-flight status fetch.
  const statusFetchRef = React.useRef(false)
  const refreshStatus = useCallback(() => {
    if (statusFetchRef.current) return
    statusFetchRef.current = true
    api.hashStatus()
      .then((s) => dispatch({ type: 'set-hash-status', status: s }))
      .catch(() => {})
      .finally(() => { statusFetchRef.current = false })
  }, [dispatch])
  useEventSource(streamUrl('hash'), {
    enabled: true,
    events,
    onEvent: (name, payload) => {
      if (name === 'poll') dispatch({ type: 'set-hash-status', status: payload })
      else if (name === 'done') {
        refreshStatus()
        invalidate()
      } else if (name === 'hash_progress' || name === 'hash_item') {
        refreshStatus()
      }
    },
    poll: () => api.hashStatus()
  })

  useEffect(() => {
    api.hashStatus().then((s) => dispatch({ type: 'set-hash-status', status: s })).catch(() => {})
  }, [dispatch])

  const categories = useMemo(() => {
    const list = (facets.data && facets.data.category) || []
    return list.map((c) => ({ value: c.value, label: c.label + ' (' + c.count + ')' }))
  }, [facets.data])

  const unhashed = useMemo(() => {
    const list = (facets.data && facets.data.hash_state) || []
    const hit = list.find((h) => h.value === 'unhashed')
    return hit ? hit.count : null
  }, [facets.data])

  const totalBytes = facets.data && facets.data.size ? facets.data.size.total : null

  const enqueue = useCallback(async () => {
    setBusy(true)
    try {
      const body = {
        scope,
        category: scope === 'category' ? category : null,
        folder: null,
        root_id: null,
        uids: scope === 'ids' ? presetUids : null,
        priority: 5,
        skip_hashed: true
      }
      const res = await api.hashEnqueue(body)
      toast({
        tone: 'ok',
        title: 'Hashing queued',
        message: fmtCount(res.queued) + ' file(s), ' + bytes(res.bytes_total) +
          (res.eta_ms ? ', about ' + duration(res.eta_ms) : '')
      })
      const s = await api.hashStatus()
      dispatch({ type: 'set-hash-status', status: s })
    } catch (err) {
      toastError(err, 'Could not queue hashing')
    } finally {
      setBusy(false)
    }
  }, [scope, category, presetUids, toast, toastError, dispatch])

  const cancelAll = useCallback(async () => {
    try {
      await api.hashCancel({ batch_id: null, uids: null })
      toast({ tone: 'warn', title: 'Hash queue cancelled' })
      const s = await api.hashStatus()
      dispatch({ type: 'set-hash-status', status: s })
    } catch (err) {
      toastError(err, 'Could not cancel')
    }
  }, [toast, toastError, dispatch])

  const applySettings = useCallback(async () => {
    try {
      await api.hashSettings({ concurrency, throttle_mbps: throttle })
      toast({ tone: 'ok', title: 'Hash settings applied' })
    } catch (err) {
      toastError(err, 'Could not apply the settings')
    }
  }, [concurrency, throttle, toast, toastError])

  const active = status && status.active
  const q = (status && status.queue) || {}
  const b = (status && status.bytes) || {}

  return (
    <Modal
      title="Hash the vault"
      subtitle="Computes SHA-256 / AutoV2 so Civitai can identify your files."
      onClose={onClose}
      footer={(
        <>
          {active
            ? <Button variant="dangerGhost" onClick={cancelAll}>Cancel queue</Button>
            : null}
          <Button variant="ghost" onClick={onClose}>Close</Button>
          <Button variant="primary" icon={Hash} onClick={enqueue} loading={busy}
            disabled={busy}>Start hashing</Button>
        </>
      )}
    >
      <div className="gp-callout gp-callout--warn gp-u-mb-6">
        <span className="gp-callout__icon"><Clock aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <div className="gp-callout__title">This reads every byte</div>
          {totalBytes ? bytes(totalBytes) + ' of models are indexed. ' : ''}
          A full pass over roughly 1.5 TB takes about 2.8 hours at typical drive speed. The queue
          is cancellable and survives a restart, so you can stop and resume it whenever you like.
        </div>
      </div>

      {active ? (
        <div className="gp-u-mb-6">
          <ProgressBar
            percent={b.percent || 0}
            label="Hashing"
            value={(b.percent || 0).toFixed(1) + '%'}
            sub={bytes(b.done) + ' of ' + bytes(b.total) +
              (status.throughput_mbps ? ' at ' + status.throughput_mbps.toFixed(0) + ' MB/s' : '') +
              (status.eta_ms ? ', about ' + duration(status.eta_ms) + ' left' : '')}
          />
          {(status.running || []).map((r) => (
            <p key={r.uid} className="gp-u-fs-10 gp-u-meta gp-u-mt-4 gp-u-break-all">
              {r.filename} - {r.percent.toFixed(0)}%
            </p>
          ))}
        </div>
      ) : null}

      <div className="gp-formgrid">
        <label className="gp-formgrid__label" htmlFor="hash-scope">Scope</label>
        <Select
          id="hash-scope"
          value={scope}
          onChange={setScope}
          ariaLabel="Hash scope"
          options={[
            { value: 'unhashed', label: 'Everything not hashed yet' + (unhashed ? ' (' + unhashed + ')' : '') },
            { value: 'all', label: 'Every model file' },
            { value: 'category', label: 'One category' },
            ...(presetUids && presetUids.length
              ? [{ value: 'ids', label: 'The current selection (' + presetUids.length + ')' }]
              : [])
          ]}
        />

        {scope === 'category' ? (
          <>
            <label className="gp-formgrid__label" htmlFor="hash-category">Category</label>
            <Select id="hash-category" value={category} onChange={setCategory}
              ariaLabel="Category" options={categories} />
          </>
        ) : null}

        <label className="gp-formgrid__label" htmlFor="hash-concurrency">Parallel reads</label>
        <div className="gp-field">
          <Slider id="hash-concurrency" value={concurrency} min={1} max={8}
            ariaLabel="Parallel reads" valueLabel={String(concurrency)}
            onChange={setConcurrency} />
          <span className="gp-field__hint">
            More parallel reads only help on an SSD; on a spinning disk they slow it down.
          </span>
        </div>

        <label className="gp-formgrid__label" htmlFor="hash-throttle">Throttle</label>
        <div className="gp-field">
          <Slider id="hash-throttle" value={throttle} min={0} max={800} step={25}
            ariaLabel="Throughput limit"
            valueLabel={throttle ? throttle + ' MB/s' : 'off'}
            onChange={setThrottle} />
          <span className="gp-field__hint">
            Cap the read rate so ComfyUI stays responsive while hashing runs.
          </span>
        </div>
      </div>

      <div className="gp-u-mt-5">
        <Button size="sm" variant="ghost" label="Apply read settings" onClick={applySettings} />
      </div>

      {status ? (
        <div className="gp-u-mt-6">
          <div className="gp-details__section-head"><span>Queue</span></div>
          <div className="gp-meta">
            <MetaRow label="queued" value={fmtCount(q.queued || 0)} num />
            <MetaRow label="running" value={fmtCount(q.running || 0)} num />
            <MetaRow label="done" value={fmtCount(q.done || 0)} num />
            <MetaRow label="failed" value={fmtCount(q.failed || 0)} num
              tone={q.failed ? 'danger' : undefined} />
          </div>
        </div>
      ) : null}

      {status && status.recent_failures && status.recent_failures.length ? (
        <div className="gp-u-mt-5">
          <div className="gp-details__section-head">
            <span>Recent failures</span>
            <Badge tone="danger">{status.recent_failures.length}</Badge>
          </div>
          <ul className="gp-confirm__list">
            {status.recent_failures.map((f, i) => (
              <li key={f.uid + ':' + i}>
                <AlertTriangle size={9} aria-hidden="true" /> {f.filename} - {humanise(f.code)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Modal>
  )
}
