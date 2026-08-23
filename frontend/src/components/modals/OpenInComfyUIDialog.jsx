import React, { useState, useCallback, useMemo, useEffect } from 'react'
import {
  ExternalLink, ShieldAlert, AlertTriangle, CheckCircle2, XCircle, Info, Copy
} from 'lucide-react'
import api, { streamUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useEventSource from '../../hooks/useEventSource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Select from '../common/Select.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { duration } from '../../services/format.js'

/*
 * OpenInComfyUIDialog - open one workflow inside ComfyUI.
 *
 * The plan is fetched and shown before anything happens, and the two things
 * that are not "open a URL" each carry their own confirmation:
 *
 *   - starting ComfyUI names the resolved absolute path of the launcher, the
 *     same promise the updater dialog makes;
 *   - copying the file into the ComfyUI installation names the destination.
 *
 * Neither is implied by pressing Open. It also refuses to pretend about deep
 * links: this ComfyUI frontend cannot open a user workflow from a URL, so for
 * those the dialog says so and tells the owner which file to pick.
 */

function Check({ checked, onChange, children, id }) {
  return (
    <label className="gp-check gp-u-mt-5" htmlFor={id}>
      <input
        id={id}
        className="gp-check__input"
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="gp-check__box">
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
        </svg>
      </span>
      <span className="gp-check__label">{children}</span>
    </label>
  )
}

export default function OpenInComfyUIDialog({ uid, name, onClose }) {
  const { toast, toastError } = useVault()
  const [launcherId, setLauncherId] = useState(null)
  const [ackStart, setAckStart] = useState(false)
  const [wantCopy, setWantCopy] = useState(false)
  const [ackCopy, setAckCopy] = useState(false)
  const [stage, setStage] = useState('plan')      // plan | starting | ready | failed
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [failure, setFailure] = useState(null)
  const [popupBlocked, setPopupBlocked] = useState(false)

  const planned = useResource(
    'comfy:open:' + uid + ':' + (launcherId || 'default'),
    (s) => api.comfyOpenWorkflowPlan(uid, launcherId || undefined, s)
  )
  const plan = planned.data

  const launcherOptions = useMemo(() => {
    if (!plan || !plan.launcher) return []
    const all = [plan.launcher, ...(plan.launcher_alternatives || [])]
    const seen = new Set()
    return all
      .filter((l) => l && l.id && (seen.has(l.id) ? false : (seen.add(l.id), true)))
      .map((l) => ({ value: l.id, label: l.label + (l.port ? ' - port ' + l.port : '') }))
  }, [plan])

  /* Opening the tab is the last thing that happens, never the first. */
  const openTab = useCallback((url) => {
    const opened = window.open(url, '_blank', 'noopener,noreferrer')
    setPopupBlocked(!opened)
    return Boolean(opened)
  }, [])

  const onEvent = useCallback((event, payload) => {
    if (event === 'waiting' && payload && payload.elapsed_ms) {
      setElapsed(payload.elapsed_ms)
    } else if (event === 'ready' || (event === 'done' && payload && payload.ready)) {
      setStage('ready')
      setElapsed((payload && payload.elapsed_ms) || 0)
      if (payload && payload.url) openTab(payload.url)
    } else if (event === 'error' || (event === 'done' && payload && !payload.ready)) {
      setStage('failed')
      setFailure((payload && (payload.error || payload.message)) || 'ComfyUI did not start.')
    } else if (event === 'poll' && payload) {
      if (payload.status === 'ready') {
        setStage('ready')
        if (payload.url) openTab(payload.url)
      } else if (payload.status === 'failed') {
        setStage('failed')
        setFailure(payload.error || 'ComfyUI did not start.')
      } else if (payload.elapsed_ms) {
        setElapsed(payload.elapsed_ms)
      }
    }
  }, [openTab])

  const events = useMemo(() => ['open', 'phase', 'waiting', 'ready', 'error', 'done'], [])
  useEventSource(streamUrl('comfyui/launch'), {
    enabled: stage === 'starting',
    events,
    onEvent,
    poll: () => api.comfyLaunchStatus()
  })

  /* A ticking "still waiting" figure, so a long cold start never looks stuck. */
  useEffect(() => {
    if (stage !== 'starting') return undefined
    const started = Date.now()
    const timer = window.setInterval(() => setElapsed(Date.now() - started), 1000)
    return () => window.clearInterval(timer)
  }, [stage])

  const copyPlan = (plan && plan.copy) || {}
  const running = (plan && plan.running) || {}
  const needsStart = Boolean(plan && plan.needs_start)
  const copyReady = !wantCopy || (copyPlan.possible && ackCopy)
  const startReady = !needsStart || (plan && plan.launcher && ackStart)
  const ready = Boolean(plan && plan.can_open && startReady && copyReady && !busy)

  const submit = useCallback(async () => {
    if (!plan) return
    setBusy(true)
    setFailure(null)
    try {
      const result = await api.comfyOpenWorkflow({
        uid,
        launcher: (plan.launcher && plan.launcher.id) || null,
        start: needsStart,
        confirm_launcher_path: needsStart ? plan.launcher_confirm_path : null,
        copy_to_user_workflows: wantCopy,
        confirm_copy_destination: wantCopy ? copyPlan.destination : null
      })
      if (result.copied) {
        toast({
          tone: 'ok',
          title: 'Copied into your ComfyUI workflows folder',
          message: result.copy_destination
        })
      }
      if (result.started) {
        setStage('starting')
        setElapsed(0)
      } else {
        setStage('ready')
        openTab(result.url)
      }
    } catch (err) {
      toastError(err, 'Could not open this workflow in ComfyUI')
      setFailure(err.message)
    } finally {
      setBusy(false)
    }
  }, [plan, uid, needsStart, wantCopy, copyPlan.destination, toast, toastError, openTab])

  /* ---------------------------------------------------------------- footer */
  let footer = null
  if (stage === 'plan') {
    footer = (
      <>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button
          variant={needsStart ? 'danger' : 'primary'}
          onClick={submit}
          loading={busy}
          disabled={!ready}
        >
          {needsStart ? 'Start ComfyUI and open' : 'Open in ComfyUI'}
        </Button>
      </>
    )
  } else if (stage === 'starting') {
    footer = <Button variant="ghost" onClick={onClose}>Hide - it keeps starting</Button>
  } else {
    footer = <Button variant="primary" onClick={onClose}>Close</Button>
  }

  const deep = (plan && plan.deep_link) || {}

  return (
    <Modal
      title="Open in ComfyUI"
      subtitle={name || (plan && plan.name) || uid}
      size="lg"
      tone={needsStart ? 'danger' : undefined}
      onClose={onClose}
      footer={footer}
    >
      {planned.loading && !plan ? <SkeletonMeta rows={7} /> : null}

      {planned.error ? (
        <div className="gp-callout gp-callout--danger">
          <span className="gp-callout__icon"><XCircle aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">The plan could not be built</div>
            {planned.error.message}
          </div>
        </div>
      ) : null}

      {plan && stage === 'plan' ? (
        <>
          {/* What ComfyUI will actually do with this file, stated first. */}
          <div className={'gp-callout gp-callout--' + (deep.supported ? 'ok' : 'info')}>
            <span className="gp-callout__icon">
              {deep.supported
                ? <CheckCircle2 aria-hidden="true" />
                : <Info aria-hidden="true" />}
            </span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">
                {deep.supported
                  ? 'ComfyUI will load this graph itself'
                  : 'ComfyUI cannot be told to open this file from a link'}
              </div>
              {deep.explanation}
            </div>
          </div>

          <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
            The address that will be opened
          </div>
          <code className="gp-code gp-u-break-all">{plan.url}</code>

          <div className="gp-meta gp-u-mt-5">
            <MetaRow label="Workflow" value={plan.name} wrap />
            <MetaRow label="Origin" value={plan.origin_label} />
            <MetaRow label="File" value={plan.abs_path} wrap />
            <MetaRow
              label="ComfyUI is"
              value={running.running
                ? 'accepting connections on port ' + plan.port
                : 'not responding on any port'}
              inferred={running.confidence === 'inferred'}
              inferredTitle={'Decided by a ' + (running.method || 'loopback tcp probe') +
                '. ' + (running.note || '')}
              tone={running.running ? 'ok' : undefined}
            />
            <MetaRow label="Port" value={String(plan.port)} num
              title={plan.port_reason} />
          </div>

          {!deep.supported && plan.manual_hint ? (
            <div className="gp-callout gp-callout--warn gp-u-mt-5">
              <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
              <div className="gp-callout__body">{plan.manual_hint}</div>
            </div>
          ) : null}

          {/* Gate 1 - a write into the ComfyUI installation. */}
          {copyPlan.needed ? (
            <>
              <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
                Optional - put it in ComfyUI's Workflows sidebar
              </div>
              <Check id="open-comfy-copy" checked={wantCopy}
                onChange={(v) => { setWantCopy(v); if (!v) setAckCopy(false) }}>
                <Copy size={12} aria-hidden="true" /> Also copy this file into my
                ComfyUI workflows folder
              </Check>
              {wantCopy ? (
                <>
                  <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-5 gp-u-mb-4">
                    The exact file that will be written
                  </div>
                  <code className="gp-code gp-u-break-all">{copyPlan.destination}</code>
                  {copyPlan.note ? (
                    <div className="gp-u-fs-11 gp-u-meta gp-u-mt-4">{copyPlan.note}</div>
                  ) : null}
                  {copyPlan.possible ? (
                    <Check id="open-comfy-copy-ack" checked={ackCopy} onChange={setAckCopy}>
                      I want to write <strong>{copyPlan.destination}</strong> into my
                      ComfyUI installation
                    </Check>
                  ) : (
                    <div className="gp-callout gp-callout--danger gp-u-mt-5">
                      <span className="gp-callout__icon"><XCircle aria-hidden="true" /></span>
                      <div className="gp-callout__body">
                        {copyPlan.exists
                          ? 'A file with that name is already there. Nothing is overwritten.'
                          : 'That copy is not possible for this workflow.'}
                      </div>
                    </div>
                  )}
                </>
              ) : null}
            </>
          ) : null}

          {/* Gate 2 - starting a program. */}
          {needsStart ? (
            plan.launcher ? (
              <>
                <div className="gp-callout gp-callout--danger gp-u-mt-6">
                  <span className="gp-callout__icon"><ShieldAlert aria-hidden="true" /></span>
                  <div className="gp-callout__body">
                    <div className="gp-callout__title">
                      ComfyUI is not running. Opening it means starting a program.
                    </div>
                    The vault runs ComfyUI's own launcher script, exactly as it is on
                    disk, in its own console window. It keeps running after you close
                    the vault - that window is how you stop it.
                  </div>
                </div>

                <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
                  The exact file that will be executed
                </div>
                <code className="gp-code gp-u-break-all">
                  {plan.launcher_confirm_path}
                </code>

                <div className="gp-meta gp-u-mt-5">
                  <MetaRow label="Working folder" value={plan.launcher.working_dir} wrap />
                  <MetaRow label="Command"
                    value={(plan.launcher.command || []).join(' ')} wrap />
                </div>

                {launcherOptions.length > 1 ? (
                  <div className="gp-formgrid gp-u-mt-6">
                    <label className="gp-formgrid__label" htmlFor="comfy-launcher">
                      Launcher
                    </label>
                    <Select
                      id="comfy-launcher"
                      value={plan.launcher.id}
                      onChange={(v) => { setLauncherId(v); setAckStart(false) }}
                      ariaLabel="Which launcher to run"
                      options={launcherOptions}
                    />
                  </div>
                ) : null}

                <Check id="open-comfy-start" checked={ackStart} onChange={setAckStart}>
                  I want to run <strong>{plan.launcher_confirm_path}</strong> and start
                  ComfyUI
                </Check>
              </>
            ) : (
              <div className="gp-callout gp-callout--danger gp-u-mt-6">
                <span className="gp-callout__icon"><XCircle aria-hidden="true" /></span>
                <div className="gp-callout__body">
                  <div className="gp-callout__title">
                    No way to start ComfyUI was found for this installation
                  </div>
                  {plan.launcher_error || 'Start ComfyUI yourself, then open this again.'}
                </div>
              </div>
            )
          ) : null}

          <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
            What happens, in order
          </div>
          <ul className="gp-confirm__list">
            {(plan.steps || []).map((step, i) => <li key={'step:' + i}>{step}</li>)}
          </ul>
        </>
      ) : null}

      {stage === 'starting' ? (
        <>
          <div className="gp-u-row gp-u-gap-4 gp-u-mb-5">
            <span className="gp-spinner gp-spinner--sm" aria-hidden="true" />
            <span className="gp-u-fs-11 gp-u-meta">
              Waiting for ComfyUI to accept connections - {duration(elapsed)} so far
            </span>
          </div>
          <div className="gp-callout gp-callout--info">
            <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
            <div className="gp-callout__body">
              A cold start loads every installed node package, so this can take a
              few minutes. ComfyUI's own console window shows exactly where it is.
              The tab opens by itself as soon as the port answers.
            </div>
          </div>
        </>
      ) : null}

      {stage === 'ready' ? (
        <>
          <div className="gp-callout gp-callout--ok">
            <span className="gp-callout__icon"><CheckCircle2 aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">ComfyUI is answering</div>
              {plan && plan.deep_link && plan.deep_link.supported
                ? 'The graph is loading in the new tab.'
                : (plan && plan.manual_hint) || 'Pick the workflow from the sidebar.'}
            </div>
          </div>
          {popupBlocked ? (
            <div className="gp-callout gp-callout--warn gp-u-mt-5">
              <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
              <div className="gp-callout__body">
                <div className="gp-callout__title">The browser blocked the new tab</div>
                <a className="gp-btn gp-btn--sm gp-u-mt-4"
                  href={(plan && plan.url) || '#'} target="_blank" rel="noreferrer">
                  <ExternalLink className="gp-btn__icon" aria-hidden="true" />
                  <span className="gp-btn__label">Open ComfyUI</span>
                </a>
              </div>
            </div>
          ) : null}
        </>
      ) : null}

      {stage === 'failed' ? (
        <div className="gp-callout gp-callout--danger">
          <span className="gp-callout__icon"><XCircle aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">ComfyUI did not come up</div>
            {failure}
          </div>
        </div>
      ) : null}
    </Modal>
  )
}
