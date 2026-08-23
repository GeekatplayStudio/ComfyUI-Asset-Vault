import React, { useState, useCallback, useEffect, useMemo } from 'react'
import { CheckCircle2, XCircle, RefreshCw, AlertTriangle } from 'lucide-react'
import api, { streamUrl } from '../../services/api.js'
import useEventSource from '../../hooks/useEventSource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import ProgressBar from '../common/ProgressBar.jsx'
import Badge from '../common/Badge.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { count as fmtCount, duration, humanise } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * IndexProgress - live scan progress.
 *
 * The stream is the primary transport; after two stream errors useEventSource
 * falls back to polling /index/status every 2 s and the modal keeps working.
 */
export default function IndexProgress({ onClose, autoStart, mode }) {
  const { dispatch, invalidate, toast, toastError, state } = useVault()
  const [live, setLive] = useState({ phase: null, done: 0, total: 0, eta: null, rate: null })
  const [finished, setFinished] = useState(null)
  const [errors, setErrors] = useState([])
  const [starting, setStarting] = useState(false)
  const [jobId, setJobId] = useState(null)

  const start = useCallback(async (scanMode) => {
    setStarting(true)
    setFinished(null)
    setErrors([])
    try {
      const res = await api.indexStart({
        mode: scanMode || 'incremental',
        phases: null,
        root_ids: null,
        force: scanMode === 'full',
        enrich_online: false
      })
      setJobId(res.job_id)
      toast({ tone: 'ok', title: 'Scan started', message: humanise(res.mode) + ' scan' })
    } catch (err) {
      if (err.code === 'JOB_ALREADY_RUNNING') {
        setJobId(err.details && err.details.job_id)
      } else {
        toastError(err, 'Could not start the scan')
      }
    } finally {
      setStarting(false)
    }
  }, [toast, toastError])

  useEffect(() => {
    if (autoStart) start(mode)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onEvent = useCallback((name, payload) => {
    if (name === 'poll') {
      const job = payload.job
      dispatch({ type: 'set-index-status', status: payload })
      if (job) {
        setLive({
          phase: job.phase,
          done: job.items_done,
          total: job.items_total,
          eta: job.eta_ms,
          rate: job.rate_per_sec,
          current: job.current
        })
      } else if (payload.last_completed && !payload.active) {
        setFinished(payload.last_completed)
      }
      return
    }
    if (name === 'phase') {
      setLive((p) => ({ ...p, phase: payload.phase, label: payload.label,
        index: payload.index, of: payload.of, done: 0, total: 0 }))
    } else if (name === 'progress') {
      setLive((p) => ({
        ...p,
        phase: payload.phase || p.phase,
        done: payload.done,
        total: payload.total,
        rate: payload.rate,
        eta: payload.eta_ms,
        current: payload.current
      }))
    } else if (name === 'error') {
      setErrors((prev) => [payload, ...prev].slice(0, 20))
    } else if (name === 'done') {
      setFinished(payload)
      invalidate()
    }
  }, [dispatch, invalidate])

  const events = useMemo(() => ['open', 'phase', 'progress', 'item', 'error', 'done', 'heartbeat'], [])
  const transport = useEventSource(streamUrl('index'), {
    enabled: true,
    events,
    onEvent,
    poll: () => api.indexStatus()
  })

  const cancel = useCallback(async () => {
    try {
      await api.indexCancel(jobId)
      toast({ tone: 'warn', title: 'Cancelling the scan' })
    } catch (err) {
      toastError(err, 'Could not cancel')
    }
  }, [jobId, toast, toastError])

  const percent = live.total ? (live.done / live.total) * 100 : 0
  const running = !finished && (starting || live.phase || (state.indexStatus && state.indexStatus.active))

  return (
    <Modal
      title="Indexing"
      subtitle={transport === 'poll'
        ? 'Live stream unavailable - polling every 2 seconds'
        : 'Reading the ComfyUI installation'}
      onClose={onClose}
      footer={(
        <>
          {running ? (
            <Button variant="dangerGhost" onClick={cancel}>Cancel scan</Button>
          ) : (
            <>
              <Button icon={RefreshCw} onClick={() => start('incremental')} loading={starting}>
                Incremental
              </Button>
              <Button icon={RefreshCw} onClick={() => start('full')} loading={starting}>
                Full rescan
              </Button>
            </>
          )}
          <Button variant="primary" onClick={onClose}>Close</Button>
        </>
      )}
    >
      {finished ? (
        <>
          <div className={'gp-callout gp-callout--' +
            (finished.status === 'completed' || !finished.status ? 'ok' : 'warn')}
          >
            <span className="gp-callout__icon">
              {finished.status === 'completed' || !finished.status
                ? <CheckCircle2 aria-hidden="true" />
                : <XCircle aria-hidden="true" />}
            </span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">
                Scan {finished.status || 'completed'}
              </div>
              Finished in {duration(finished.duration_ms)} with{' '}
              {fmtCount(finished.errors || 0)} error(s).
            </div>
          </div>
          {finished.stats ? (
            <div className="gp-meta gp-u-mt-5">
              {Object.entries(finished.stats).map(([phase, value]) => (
                <MetaRow
                  key={phase}
                  label={phase}
                  value={value && typeof value === 'object'
                    ? Object.entries(value)
                      .filter(([k]) => k !== 'elapsed_ms')
                      .map(([k, v]) => k + ' ' + (typeof v === 'object' ? JSON.stringify(v) : v))
                      .join(', ')
                    : String(value)}
                  wrap
                />
              ))}
            </div>
          ) : null}
        </>
      ) : !running ? (
        <div className="gp-callout gp-callout--info">
          <span className="gp-callout__icon"><RefreshCw aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">No scan is running</div>
            An <strong>incremental</strong> scan only reads files whose size or timestamp changed,
            and finishes in a second or two. A <strong>full rescan</strong> re-parses every header
            and takes around half a minute on this library. Neither one hashes anything.
          </div>
        </div>
      ) : (
        <>
          <ProgressBar
            percent={percent}
            indeterminate={!live.total}
            label={live.label || humanise(live.phase) || 'Preparing'}
            value={live.total
              ? fmtCount(live.done) + ' / ' + fmtCount(live.total)
              : (starting ? 'starting' : 'working')}
            sub={[
              live.rate ? live.rate.toFixed(0) + ' items/s' : null,
              live.eta ? 'about ' + duration(live.eta) + ' left' : null,
              live.index ? 'phase ' + live.index + ' of ' + live.of : null
            ].filter(Boolean).join('  /  ')}
          />
          {live.current ? (
            <p className="gp-u-fs-10 gp-u-meta gp-u-mt-4 gp-u-break-all">{live.current}</p>
          ) : null}
        </>
      )}

      {errors.length ? (
        <div className="gp-u-mt-6">
          <div className="gp-details__section-head">
            <span>Errors</span>
            <Badge tone="danger">{errors.length}</Badge>
          </div>
          <ul className="gp-confirm__list">
            {errors.map((e, i) => (
              <li key={(e.code || 'err') + ':' + i}>
                <AlertTriangle size={9} aria-hidden="true" /> {e.code}: {e.message || e.path}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Modal>
  )
}
