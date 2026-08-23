import React, { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import {
  AlertTriangle, Terminal, CheckCircle2, XCircle, ShieldAlert
} from 'lucide-react'
import api, { streamUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useEventSource from '../../hooks/useEventSource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import Select from '../common/Select.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { duration, count as fmtCount, humanise } from '../../services/format.js'

/*
 * UpdateDialog - the only place in this product that starts a process.
 *
 * The promise the dialog makes is "this exact file will be executed", and the
 * contract makes that promise structurally true: confirm_path must match the
 * path /update/plan resolved, or the server refuses with a 422. So the path is
 * shown verbatim, in monospace, as the subject of the sentence - not tucked in
 * a tooltip - and the confirm button stays disabled until the owner ticks the
 * box that repeats what is about to happen.
 *
 * It refuses outright while ComfyUI is accepting connections.
 */

const VISIBLE_LINES = 400

export default function UpdateDialog({ plan: initialPlan, info, onClose }) {
  const { toast, toastError } = useVault()
  const [updaterId, setUpdaterId] = useState(initialPlan.updater)
  const [acknowledged, setAcknowledged] = useState(false)
  const [stage, setStage] = useState('confirm')   // confirm | running | done
  const [lines, setLines] = useState([])
  const [result, setResult] = useState(null)
  const [starting, setStarting] = useState(false)
  const logRef = useRef(null)

  /* Switching updater re-plans, so confirm_path always belongs to the mechanism
     actually shown. Never derived in the client. */
  const planned = useResource(
    updaterId === initialPlan.updater ? null : 'comfy:plan:' + updaterId,
    (s) => api.comfyUpdatePlan(updaterId, s)
  )
  const plan = (updaterId === initialPlan.updater ? initialPlan : planned.data) || initialPlan

  const options = useMemo(() => {
    const all = [
      { id: initialPlan.updater, label: initialPlan.label },
      ...((initialPlan.alternatives || []).map((a) => ({ id: a.id, label: a.label })))
    ]
    const seen = new Set()
    return all.filter((o) => (seen.has(o.id) ? false : (seen.add(o.id), true)))
      .map((o) => ({ value: o.id, label: o.label }))
  }, [initialPlan])

  const running = plan.running || {}
  const blocked = !plan.can_run
  const ready = plan.confirm_path && acknowledged && !blocked && !planned.loading

  /* ------------------------------------------------------------- the stream */
  const events = useMemo(() => ['open', 'output', 'done', 'error', 'heartbeat'], [])
  const onEvent = useCallback((name, payload) => {
    if (name === 'output') {
      setLines((prev) => {
        const next = [...prev, payload]
        return next.length > VISIBLE_LINES ? next.slice(next.length - VISIBLE_LINES) : next
      })
    } else if (name === 'done') {
      setResult(payload)
      setStage('done')
    } else if (name === 'error') {
      setResult((prev) => prev || { status: 'failed', error: payload && payload.message })
      setStage('done')
    } else if (name === 'poll' && payload) {
      if (payload.status && payload.status !== 'running' && payload.status !== 'idle') {
        setResult(payload)
        setStage('done')
      }
    }
  }, [])

  useEventSource(streamUrl('comfyui/update'), {
    enabled: stage === 'running',
    events,
    onEvent,
    poll: () => api.comfyUpdateStatus()
  })

  // The modal body is the scroller; keep the newest line in view without
  // reaching for a height of our own.
  useEffect(() => {
    const node = logRef.current
    if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' })
  }, [lines.length, stage])

  const start = useCallback(async () => {
    setStarting(true)
    try {
      await api.comfyUpdateRun({
        updater: plan.updater,
        confirm_path: plan.confirm_path
      })
      setLines([])
      setResult(null)
      setStage('running')
    } catch (err) {
      if (err.status === 409) {
        toast({
          tone: 'warn',
          title: 'ComfyUI is running',
          message: 'Close ComfyUI before updating it, then try again.'
        })
      } else {
        toastError(err, 'The updater did not start')
      }
    } finally {
      setStarting(false)
    }
  }, [plan, toast, toastError])

  /* --------------------------------------------------------------- footers */
  let footer = null
  if (stage === 'confirm') {
    footer = (
      <>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="danger" onClick={start} loading={starting} disabled={!ready || starting}>
          Run this file
        </Button>
      </>
    )
  } else if (stage === 'running') {
    footer = <Button variant="ghost" onClick={onClose}>Hide - it keeps running</Button>
  } else {
    footer = <Button variant="primary" onClick={onClose}>Close</Button>
  }

  const exitOk = result && result.exit_code === 0

  return (
    <Modal
      title={stage === 'confirm'
        ? 'Update ComfyUI'
        : (stage === 'running' ? 'Updating ComfyUI' : 'Updater finished')}
      subtitle={stage === 'confirm'
        ? 'This modifies your ComfyUI installation, not the vault'
        : plan.path}
      size="lg"
      tone="danger"
      onClose={stage === 'running' ? undefined : onClose}
      footer={footer}
    >
      {stage === 'confirm' ? (
        <>
          {/* The blast radius, named before anything else. */}
          <div className="gp-callout gp-callout--danger">
            <span className="gp-callout__icon"><ShieldAlert aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">
                This runs a program that changes ComfyUI {info.version} in place
              </div>
              The vault does not update ComfyUI itself. It executes ComfyUI's own updater,
              exactly as shipped, and shows you its output. Nothing here touches your models,
              outputs or the vault database.
            </div>
          </div>

          <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
            The exact file that will be executed
          </div>
          <code className="gp-code gp-u-break-all">{plan.confirm_path}</code>

          <div className="gp-meta gp-u-mt-5">
            <MetaRow label="Mechanism" value={plan.label} />
            <MetaRow label="Working folder" value={plan.working_dir} wrap />
            <MetaRow label="Command" value={(plan.command || []).join(' ')} wrap />
            <MetaRow
              label="ComfyUI is"
              value={running.running ? 'accepting connections' : 'not responding on any port'}
              inferred={running.confidence === 'inferred'}
              inferredTitle={'Decided by a ' + (running.method || 'loopback tcp probe') +
                '. ' + (running.note || '')}
              tone={running.running ? 'danger' : undefined}
            />
          </div>

          {options.length > 1 ? (
            <div className="gp-formgrid gp-u-mt-6">
              <label className="gp-formgrid__label" htmlFor="updater-choice">Updater</label>
              <Select
                id="updater-choice"
                value={updaterId}
                onChange={setUpdaterId}
                ariaLabel="Which updater to run"
                options={options}
              />
            </div>
          ) : null}

          {(plan.warnings || []).length ? (
            <div className="gp-callout gp-callout--warn gp-u-mt-5">
              <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
              <div className="gp-callout__body">
                {plan.warnings.length === 1
                  ? plan.warnings[0]
                  : (
                    <ul className="gp-confirm__list">
                      {plan.warnings.map((w, i) => <li key={'warn:' + i}>{w}</li>)}
                    </ul>
                  )}
              </div>
            </div>
          ) : null}

          {blocked ? (
            <div className="gp-callout gp-callout--danger gp-u-mt-5">
              <span className="gp-callout__icon"><XCircle aria-hidden="true" /></span>
              <div className="gp-callout__body">
                <div className="gp-callout__title">
                  Refused - {humanise(plan.blocked_reason || 'unavailable')}
                </div>
                {plan.blocked_reason === 'comfyui_running'
                  ? 'Close ComfyUI first. Updating it while it is running corrupts the install.'
                  : 'The updater path could not be resolved on disk.'}
              </div>
            </div>
          ) : (
            <label className="gp-check gp-u-mt-6">
              <input
                className="gp-check__input"
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span className="gp-check__box">
                <svg viewBox="0 0 12 12" aria-hidden="true">
                  <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor"
                    strokeWidth="1.8" />
                </svg>
              </span>
              <span className="gp-check__label">
                I want to run <strong>{plan.confirm_path}</strong> and update my ComfyUI install
              </span>
            </label>
          )}
        </>
      ) : (
        <>
          <div className="gp-u-row gp-u-gap-4 gp-u-mb-5">
            {stage === 'running' ? (
              <>
                <span className="gp-spinner gp-spinner--sm" aria-hidden="true" />
                <span className="gp-u-fs-11 gp-u-meta">
                  Running - {fmtCount(lines.length)} line(s) so far
                </span>
              </>
            ) : (
              <>
                <Badge tone={exitOk ? 'ok' : 'danger'} large>
                  {exitOk
                    ? <CheckCircle2 aria-hidden="true" />
                    : <XCircle aria-hidden="true" />}
                  exit code {result && result.exit_code !== null && result.exit_code !== undefined
                    ? result.exit_code : 'unknown'}
                </Badge>
                <span className="gp-u-fs-11 gp-u-meta">
                  {humanise((result && result.status) || 'finished')}
                  {result && result.duration_ms ? ' in ' + duration(result.duration_ms) : ''}
                </span>
              </>
            )}
          </div>

          {/* Outcome first, output below it: the exit code is the answer, and the
              log is the evidence the reader scrolls to. */}
          {stage === 'done' && result ? (
            <>
              <div className={'gp-callout gp-u-mb-5 gp-callout--' + (exitOk ? 'ok' : 'danger')}>
                <span className="gp-callout__icon">
                  {exitOk ? <CheckCircle2 aria-hidden="true" /> : <XCircle aria-hidden="true" />}
                </span>
                <div className="gp-callout__body">
                  <div className="gp-callout__title">
                    {exitOk ? 'The updater finished cleanly' : 'The updater reported a failure'}
                  </div>
                  {result.note || (result.restart_required
                    ? 'Restart ComfyUI, then re-scan the vault so node packages and workflows are re-read.'
                    : 'Read the output below before restarting ComfyUI.')}
                </div>
              </div>
              <div className="gp-meta gp-u-mb-5">
                <MetaRow label="Exit code"
                  value={result.exit_code === null || result.exit_code === undefined
                    ? null : String(result.exit_code)}
                  num tone={exitOk ? 'ok' : 'danger'} />
                <MetaRow label="Lines" value={fmtCount(result.lines)} num />
                <MetaRow label="Version after" value={result.version_after} num />
                {result.error ? (
                  <MetaRow label="Error" value={result.error} tone="danger" wrap />
                ) : null}
              </div>
            </>
          ) : null}

          <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mb-4">
            <Terminal size={11} aria-hidden="true" /> Updater output
          </div>
          <code className="gp-code">
            {lines.length
              ? lines.map((l) => (
                <div key={'line:' + l.n}>{l.line}</div>
              ))
              : <span className="gp-u-dim">waiting for output...</span>}
            <span ref={logRef} />
          </code>
        </>
      )}
    </Modal>
  )
}
