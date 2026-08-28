import React, { useState, useEffect, useCallback } from 'react'
import {
  CheckCircle2, XCircle, AlertTriangle, ChevronRight, ChevronLeft, Sparkles, Rocket
} from 'lucide-react'
import api, { isAbort } from '../../services/api.js'
import useDebounced from '../../hooks/useDebounced.js'
import Button from '../common/Button.jsx'
import Toggle from '../common/Toggle.jsx'
import Badge from '../common/Badge.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes, count as fmtCount } from '../../services/format.js'

/*
 * FirstLaunchWizard - three steps: point at ComfyUI, choose what may run,
 * then start the first scan.
 *
 * The scan is never awaited inside the wizard request; the wizard hands off to
 * the live progress view the moment the server accepts it.
 */

// A placeholder only, never a guess about this machine: the field starts
// empty and the example matches the platform serving the page.
const EXAMPLE_PATH = navigator.platform && navigator.platform.startsWith('Win')
  ? 'C:\\ComfyUI' : '/home/you/ComfyUI'

function BrandLockup() {
  return (
    <div className="gp-brand gp-u-mb-6">
      <span className="gp-brand__mark">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="3.25" y="3.25" width="17.5" height="17.5" rx="3"
            stroke="currentColor" strokeWidth="1.4" opacity=".55" />
          <rect x="6.6" y="6.8" width="2" height="10.4" rx="1" fill="currentColor" />
          <rect x="10.8" y="7.1" width="7" height="1.6" rx=".8" fill="currentColor" opacity=".9" />
          <rect x="10.8" y="11.2" width="5.2" height="1.6" rx=".8" fill="currentColor" opacity=".6" />
          <rect x="10.8" y="15.3" width="3.4" height="1.6" rx=".8" fill="currentColor" opacity=".38" />
        </svg>
      </span>
      <span className="gp-brand__text">
        <span className="gp-brand__word">GEEKATPLAY</span>
        <span className="gp-brand__sub">ASSET VAULT</span>
      </span>
    </div>
  )
}

export default function FirstLaunchWizard({ onDone, onCancel, onSkip, initialPath }) {
  const { toast, toastError, refreshConfig } = useVault()
  const [step, setStep] = useState(0)
  const [path, setPath] = useState(initialPath || '')
  const [validation, setValidation] = useState(null)
  const [checking, setChecking] = useState(false)
  const [online, setOnline] = useState(false)
  const [autoReindex, setAutoReindex] = useState(true)
  const [smart, setSmart] = useState(false)
  const [starting, setStarting] = useState(false)
  const debounced = useDebounced(path, 400)

  useEffect(() => {
    if (!debounced || debounced.trim().length < 2) { setValidation(null); return undefined }
    const controller = new AbortController()
    let alive = true
    setChecking(true)
    api.validatePath(debounced.trim(), controller.signal)
      .then((res) => { if (alive) { setValidation(res); setChecking(false) } })
      .catch((err) => {
        if (!isAbort(err) && alive) {
          setValidation({ valid: false, reason: err.message })
          setChecking(false)
        }
      })
    return () => { alive = false; controller.abort() }
  }, [debounced])

  const complete = useCallback(async (startScan) => {
    setStarting(true)
    try {
      const res = await api.completeWizard({
        comfyui_path: path.trim(),
        online_enabled: online,
        auto_reindex: autoReindex,
        smart_search_enabled: smart,
        ollama_enabled: false,
        ollama_url: 'http://localhost:11434',
        ollama_model: 'llama3',
        start_scan: Boolean(startScan)
      })
      await refreshConfig()
      toast({
        tone: 'ok',
        title: 'Vault configured',
        message: res.scan_started ? 'The first scan is running.' : 'Run a scan when you are ready.'
      })
      onDone(res)
    } catch (err) {
      toastError(err, 'Setup could not be completed')
    } finally {
      setStarting(false)
    }
  }, [path, online, autoReindex, smart, refreshConfig, toast, toastError, onDone])

  const preview = (validation && validation.preview) || {}
  const signals = (validation && validation.signals) || {}
  const usable = validation && validation.valid

  return (
    <div className="gp-centered">
      <div className="gp-centered__inner">
        <div className="gp-modal">
          <div className="gp-modal__header">
            <div className="gp-modal__titles">
              <h1 className="gp-modal__title">
                {step === 0 ? 'Where is ComfyUI?'
                  : (step === 1 ? 'What may the vault do?' : 'Ready to index')}
              </h1>
              <div className="gp-modal__sub">Step {step + 1} of 3</div>
            </div>
          </div>

          <div className="gp-modal__body">
            <BrandLockup />

            {step === 0 ? (
              <>
                <div className="gp-field">
                  <label className="gp-field__label" htmlFor="wizard-path">
                    Installation folder <span className="gp-field__req">*</span>
                  </label>
                  <input
                    id="wizard-path"
                    className="gp-input gp-input--mono"
                    value={path}
                    spellCheck="false"
                    autoFocus
                    onChange={(e) => setPath(e.target.value)}
                    placeholder={EXAMPLE_PATH}
                  />
                  <span className="gp-field__hint">
                    The folder holding models, custom_nodes, output and input. Nothing is written
                    to it; the vault only reads.
                  </span>
                </div>

                {checking && !validation ? (
                  <p className="gp-u-fs-11 gp-u-meta gp-u-mt-4">Looking at that folder...</p>
                ) : null}

                {validation ? (
                  <div className={'gp-callout gp-u-mt-5 gp-callout--' +
                    (usable && validation.is_comfyui_root ? 'ok' : (usable ? 'warn' : 'danger'))}
                  >
                    <span className="gp-callout__icon">
                      {usable && validation.is_comfyui_root
                        ? <CheckCircle2 aria-hidden="true" />
                        : (usable
                          ? <AlertTriangle aria-hidden="true" />
                          : <XCircle aria-hidden="true" />)}
                    </span>
                    <div className="gp-callout__body">
                      <div className="gp-callout__title">
                        {usable && validation.is_comfyui_root
                          ? 'Found a ComfyUI installation'
                          : (usable ? 'Folder exists, but no ComfyUI signature'
                            : (validation.reason || 'Cannot use that folder'))}
                      </div>
                      {validation.normalized ? (
                        <div className="gp-u-fs-11 gp-u-break-all">{validation.normalized}</div>
                      ) : null}
                      {signals.comfyui_version ? (
                        <div className="gp-u-mt-4">
                          <Badge tone="brand">ComfyUI {signals.comfyui_version}</Badge>
                        </div>
                      ) : null}
                      {usable ? (
                        <div className="gp-meta gp-u-mt-5">
                          <MetaRow label="model files" value={fmtCount(preview.model_files)} num />
                          <MetaRow label="models size" value={bytes(preview.model_bytes)} num />
                          <MetaRow label="node packages"
                            value={fmtCount(preview.custom_node_packages)} num />
                          <MetaRow label="workflows" value={fmtCount(preview.workflows)} num />
                          <MetaRow label="outputs" value={fmtCount(preview.outputs)} num />
                          <MetaRow label="inputs" value={fmtCount(preview.inputs)} num />
                        </div>
                      ) : null}
                      {(validation.warnings || []).length ? (
                        <ul className="gp-confirm__list gp-u-mt-4">
                          {validation.warnings.map((w, i) => <li key={'w:' + i}>{w}</li>)}
                        </ul>
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </>
            ) : null}

            {step === 1 ? (
              <>
                <div className="gp-formgrid">
                  <span className="gp-formgrid__label">Rescan</span>
                  <Toggle checked={autoReindex} label="Reindex on startup when files changed"
                    onChange={setAutoReindex} />
                  <span className="gp-formgrid__label">Network</span>
                  <Toggle checked={online} label="Allow Civitai lookups for hashed files"
                    onChange={setOnline} />
                  <span className="gp-formgrid__label">Smart search</span>
                  <Toggle ai checked={smart} label="Enable meaning-based search later"
                    onChange={setSmart} />
                </div>
                <div className="gp-callout gp-callout--info gp-u-mt-6">
                  <span className="gp-callout__icon"><Sparkles aria-hidden="true" /></span>
                  <div className="gp-callout__body">
                    Everything works offline. Network access is only ever used to identify a file
                    you have already hashed, and smart search downloads a 23 MB model the first
                    time you turn it on. Hashing is a separate job you start yourself - it is
                    never part of a scan.
                  </div>
                </div>
              </>
            ) : null}

            {step === 2 ? (
              <>
                <div className="gp-meta">
                  <MetaRow label="ComfyUI" value={validation && validation.normalized} wrap />
                  <MetaRow label="model files" value={fmtCount(preview.model_files)} num />
                  <MetaRow label="node packages" value={fmtCount(preview.custom_node_packages)} num />
                  <MetaRow label="workflows" value={fmtCount(preview.workflows)} num />
                  <MetaRow label="outputs" value={fmtCount(preview.outputs)} num />
                  <MetaRow label="network" value={online ? 'allowed' : 'off'} />
                  <MetaRow label="smart search" value={smart ? 'on' : 'off'} />
                </div>
                <div className="gp-callout gp-callout--ok gp-u-mt-6">
                  <span className="gp-callout__icon"><Rocket aria-hidden="true" /></span>
                  <div className="gp-callout__body">
                    <div className="gp-callout__title">The first scan reads headers only</div>
                    It parses metadata, never file bodies. A typical library is indexed in
                    seconds to minutes, but a very large one — or one on a slow or network
                    drive — can take considerably longer on the first pass. It runs in the
                    background: you can keep using the app the whole time, and later scans
                    only touch what changed.
                  </div>
                </div>
              </>
            ) : null}
          </div>

          <div className="gp-modal__footer">
            {onCancel || onSkip ? (
              <div className="gp-modal__footer-left">
                {onCancel ? (
                  <Button variant="ghost" onClick={onCancel}>Cancel</Button>
                ) : (
                  <Button variant="ghost" onClick={onSkip}
                    title="Open the app without a ComfyUI folder. You can set it any time in Settings -> Location.">
                    Set up later
                  </Button>
                )}
              </div>
            ) : null}
            {step > 0 ? (
              <Button icon={ChevronLeft} label="Back" onClick={() => setStep(step - 1)} />
            ) : null}
            {step < 2 ? (
              <Button
                variant="primary"
                icon={ChevronRight}
                label="Continue"
                disabled={step === 0 && !usable}
                title={step === 0 && !usable ? 'Enter a folder that exists first' : undefined}
                onClick={() => setStep(step + 1)}
              />
            ) : (
              <>
                <Button label="Finish without scanning" disabled={starting}
                  onClick={() => complete(false)} />
                <Button variant="primary" icon={Rocket} label="Finish and scan"
                  loading={starting} disabled={starting} onClick={() => complete(true)} />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
