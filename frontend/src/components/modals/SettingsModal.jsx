import React, { useState, useEffect, useCallback, useMemo, Suspense, lazy } from 'react'
import {
  CheckCircle2, XCircle, AlertTriangle, FolderSearch, RefreshCw, Sparkles,
  Trash2, HardDrive, Info, Wand2
} from 'lucide-react'
import api, { isAbort } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useDebounced from '../../hooks/useDebounced.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Select from '../common/Select.jsx'
import Toggle from '../common/Toggle.jsx'
import Badge from '../common/Badge.jsx'
import ProgressBar from '../common/ProgressBar.jsx'
import MetaRow from '../details/MetaRow.jsx'
import EmptyState from '../common/EmptyState.jsx'
import ErrorBoundary from '../common/ErrorBoundary.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes, count as fmtCount, humanise, dateTime } from '../../services/format.js'

/*
 * SettingsModal.
 *
 * The Location tab is the one that matters most (REQUIREMENTS C7): the owner
 * must be able to point the vault at a different ComfyUI install, see live
 * validation of what was found there before saving, and reindex immediately
 * afterwards - with the fate of the existing rows stated plainly.
 */

/* The activity log is a secondary panel with its own list, virtualiser and
   filters, and it is only ever reached by clicking one tab. Splitting it keeps
   the first-paint chunk inside the budget vite.config.js sets. */
const ActivityPanel = lazy(() => import('../activity/ActivityPanel.jsx'))

const TABS = [
  { id: 'location', label: 'Location' },
  { id: 'search', label: 'Search' },
  { id: 'jobs', label: 'Jobs' },
  { id: 'storage', label: 'Storage' },
  { id: 'activity', label: 'Activity' }
]

/* ------------------------------------------------------------------ location */

function PathValidator({ value, onResult }) {
  const [result, setResult] = useState(null)
  const [checking, setChecking] = useState(false)
  const debounced = useDebounced(value, 400)

  useEffect(() => {
    if (!debounced || debounced.trim().length < 2) {
      setResult(null)
      if (onResult) onResult(null)
      return undefined
    }
    const controller = new AbortController()
    let alive = true
    setChecking(true)
    api.validatePath(debounced.trim(), controller.signal)
      .then((res) => {
        if (!alive) return
        setResult(res)
        setChecking(false)
        if (onResult) onResult(res)
      })
      .catch((err) => {
        if (!isAbort(err) && alive) {
          const bad = { valid: false, reason: err.message }
          setResult(bad)
          setChecking(false)
          if (onResult) onResult(bad)
        }
      })
    return () => { alive = false; controller.abort() }
  }, [debounced, onResult])

  if (checking && !result) {
    return <p className="gp-u-fs-11 gp-u-meta gp-u-mt-4">Checking that folder...</p>
  }
  if (!result) return null

  const signals = result.signals || {}
  const preview = result.preview || {}
  const extra = result.extra_model_paths || {}
  const warnings = (result.warnings || []).filter((w) => w !== result.reason)

  return (
    <div className={'gp-callout gp-u-mt-4 gp-callout--' +
      (result.valid && result.is_comfyui_root ? 'ok' : (result.valid ? 'warn' : 'danger'))}
    >
      <span className="gp-callout__icon">
        {result.valid && result.is_comfyui_root
          ? <CheckCircle2 aria-hidden="true" />
          : (result.valid ? <AlertTriangle aria-hidden="true" /> : <XCircle aria-hidden="true" />)}
      </span>
      <div className="gp-callout__body">
        <div className="gp-callout__title">
          {result.valid && result.is_comfyui_root
            ? 'This looks like a ComfyUI installation'
            : (result.valid
              ? 'The folder exists but does not look like a ComfyUI root'
              : 'Not usable')}
        </div>
        {result.normalized ? (
          <div className="gp-u-fs-11 gp-u-break-all gp-u-mb-4">{result.normalized}</div>
        ) : null}
        {result.reason ? <div className="gp-u-fs-11">{result.reason}</div> : null}

        {result.valid ? (
          <>
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
              {Object.entries(signals)
                .filter(([k]) => k.startsWith('has_'))
                .map(([k, v]) => (
                  <Badge key={k} tone={v ? 'ok' : 'neutral'}>
                    {v ? '' : 'no '}{k.replace('has_', '').replace(/_/g, ' ')}
                  </Badge>
                ))}
              {signals.comfyui_version
                ? <Badge tone="brand">ComfyUI {signals.comfyui_version}</Badge>
                : null}
            </div>

            <div className="gp-meta gp-u-mt-5">
              <MetaRow label="model files" value={fmtCount(preview.model_files)} num />
              <MetaRow label="model bytes" value={bytes(preview.model_bytes)} num />
              <MetaRow label="node packages" value={fmtCount(preview.custom_node_packages)} num />
              <MetaRow label="workflows" value={fmtCount(preview.workflows)} num />
              <MetaRow label="outputs" value={fmtCount(preview.outputs)} num />
              <MetaRow label="inputs" value={fmtCount(preview.inputs)} num />
              <MetaRow label="extra model paths"
                value={extra.present ? 'loaded' : (extra.held_present ? 'present but held' : null)}
                empty="No extra_model_paths.yaml" />
            </div>
          </>
        ) : null}

        {warnings.length ? (
          <ul className="gp-confirm__list gp-u-mt-4">
            {warnings.map((w, i) => <li key={'warn:' + i}>{w}</li>)}
          </ul>
        ) : null}
      </div>
    </div>
  )
}

function LocationTab({ config, onSaved, onReindex, onWizard }) {
  const { toast, toastError, refreshConfig } = useVault()
  const [path, setPath] = useState(config.comfyui_path || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [savedRootsChanged, setSavedRootsChanged] = useState(false)
  const [verdict, setVerdict] = useState(null)

  const dirty = path.trim() !== (config.comfyui_path || '')
  const rejected = Boolean(verdict && verdict.valid === false)
  const blockedReason = rejected
    ? 'That folder cannot be used: ' + (verdict.reason || 'it is not reachable')
    : (!dirty ? 'The path has not changed' : undefined)

  const save = useCallback(async () => {
    setSaving(true)
    setError(null)
    try {
      const next = await api.updateConfig({ comfyui_path: path.trim() })
      await refreshConfig()
      setSavedRootsChanged(Boolean(next.roots_changed))
      toast({
        tone: 'ok',
        title: 'ComfyUI path saved',
        message: next.comfyui_path
      })
      if (onSaved) onSaved(next)
    } catch (err) {
      setError(err)
      toastError(err, 'Could not save the path')
    } finally {
      setSaving(false)
    }
  }, [path, refreshConfig, toast, toastError, onSaved])

  return (
    <>
      <div className="gp-field">
        <label className="gp-field__label" htmlFor="comfy-path">
          ComfyUI installation folder <span className="gp-field__req">*</span>
        </label>
        <input
          id="comfy-path"
          className={'gp-input gp-input--mono' + (error ? ' gp-input--invalid' : '')}
          value={path}
          spellCheck="false"
          aria-invalid={error ? 'true' : undefined}
          onChange={(e) => setPath(e.target.value)}
          placeholder="O:\ComfyUI"
        />
        {error ? (
          <span className="gp-field__error">
            <AlertTriangle aria-hidden="true" /> {error.fieldError('comfyui_path') || error.message}
          </span>
        ) : (
          <span className="gp-field__hint">
            The folder that contains models, custom_nodes, output and input. Validation runs as
            you type; nothing is saved until you press Save.
          </span>
        )}
      </div>

      <PathValidator value={path} onResult={setVerdict} />

      <div className="gp-callout gp-callout--info gp-u-mt-5">
        <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <div className="gp-callout__title">What happens to what is already indexed</div>
          Existing rows are <strong>kept, not erased</strong>. After a rescan, anything whose file
          is no longer on disk is flagged as missing and hidden from the lists; it is only removed
          for good after 30 days. Rows belonging to a root that is offline are left untouched, so
          unplugging a drive never wipes your library. Extra roots from
          extra_model_paths.yaml survive a change of the primary path.
        </div>
      </div>

      <div className="gp-u-row gp-u-gap-4 gp-u-mt-6 gp-u-wrap">
        <Button variant="primary" icon={FolderSearch} label="Save path"
          disabled={!dirty || rejected || saving} loading={saving}
          title={blockedReason} onClick={save} />
        <Button icon={RefreshCw} label="Save and reindex now"
          disabled={rejected || saving} title={rejected ? blockedReason : undefined}
          onClick={async () => { if (dirty) await save(); onReindex('full') }} />
        <Button variant="ghost" icon={Wand2} label="Run the setup wizard"
          onClick={onWizard} />
      </div>

      {savedRootsChanged ? (
        <div className="gp-callout gp-callout--warn gp-u-mt-5">
          <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">The scan roots changed</div>
            Reindex now so the models, nodes, workflows and outputs lists describe the new
            installation.
            <div className="gp-callout__actions">
              <Button size="sm" variant="primary" icon={RefreshCw} label="Reindex now"
                onClick={() => onReindex('full')} />
            </div>
          </div>
        </div>
      ) : null}

      <div className="gp-details__section-head gp-u-mt-6"><span>Scan roots</span></div>
      <table className="gp-table gp-table--compact">
        <thead>
          <tr><th>Label</th><th>Kind</th><th>Path</th><th className="gp-table__num">State</th></tr>
        </thead>
        <tbody>
          {(config.roots || []).map((root) => (
            <tr key={root.id}>
              <td>{root.label}</td>
              <td>{humanise(root.kind)}</td>
              <td className="gp-u-break-all">{root.path}</td>
              <td className="gp-table__num">
                <Badge tone={root.available ? 'ok' : 'danger'}>
                  {root.available ? 'available' : 'offline'}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

/* -------------------------------------------------------------------- search */

/* Cosine floors for the semantic arm.  Named rather than numeric: the raw
   value means nothing to anyone who has not read the embedding code. */
const STRICTNESS_LEVELS = [
  { value: '0.45', label: 'Strict - only close matches' },
  { value: '0.3', label: 'Balanced (default)' },
  { value: '0.2', label: 'Loose - more, looser matches' },
  { value: '0.1', label: 'Widest - anything the model ranks' }
]

function SearchTab({ config }) {
  const { state, toast, toastError, refreshConfig } = useVault()
  const embeddings = useResource('embeddings-status', (s) => api.embeddingsStatus(s),
    { epoch: state.dataEpoch })
  const ai = useResource('ai-status', (s) => api.aiStatus(s), { epoch: state.dataEpoch })
  const [busy, setBusy] = useState(false)
  const [ollamaUrl, setOllamaUrl] = useState(config.ollama_url || '')
  const [ollamaProbe, setOllamaProbe] = useState(null)

  const emb = embeddings.data

  const patch = useCallback(async (body) => {
    try {
      await api.updateConfig(body)
      await refreshConfig()
      toast({ tone: 'ok', title: 'Setting saved' })
    } catch (err) {
      toastError(err, 'Could not save the setting')
    }
  }, [refreshConfig, toast, toastError])

  const enable = useCallback(async () => {
    setBusy(true)
    try {
      await api.embeddingsEnable('auto')
      toast({
        tone: 'ai',
        title: 'Downloading the embedding model',
        message: 'About 23 MB. Smart search turns on once it lands.'
      })
      embeddings.refresh()
    } catch (err) {
      toastError(err, 'Smart search could not be enabled')
    } finally {
      setBusy(false)
    }
  }, [embeddings, toast, toastError])

  const probe = useCallback(async () => {
    try {
      const res = await api.testOllama(ollamaUrl)
      setOllamaProbe(res)
    } catch (err) {
      toastError(err, 'Could not reach the local model server')
    }
  }, [ollamaUrl, toastError])

  return (
    <>
      <div className="gp-details__section-head"><span>Smart search</span></div>
      {emb ? (
        <>
          <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mb-4">
            <Badge tone={emb.state === 'ready' ? 'ok' : 'neutral'}>{humanise(emb.state)}</Badge>
            <Badge tone="mono">{emb.model_id}</Badge>
            <Badge tone="mono">{emb.dim} dims</Badge>
          </div>
          {emb.download && emb.download.bytes_total ? (
            <ProgressBar percent={emb.download.percent} tone="ai" label="Downloading"
              value={bytes(emb.download.bytes_done) + ' / ' + bytes(emb.download.bytes_total)} />
          ) : null}
          <div className="gp-meta gp-u-mt-4">
            <MetaRow label="reason" value={humanise(emb.reason)} />
            <MetaRow label="embedded" value={fmtCount(emb.index && emb.index.embedded)} num />
            <MetaRow label="pending" value={fmtCount(emb.index && emb.index.pending)} num />
            <MetaRow label="runtime"
              value={emb.onnxruntime && emb.onnxruntime.installed
                ? emb.onnxruntime.version : null}
              empty="The inference runtime is not installed." />
            <MetaRow label="install folder" value={emb.install_dir} wrap />
          </div>
          <div className="gp-callout gp-callout--ai gp-u-mt-5">
            <span className="gp-callout__icon"><Sparkles aria-hidden="true" /></span>
            <div className="gp-callout__body">
              Smart search fuses the keyword index with vector similarity. The model is about
              23 MB and is downloaded only when you ask for it; it can also be placed in the
              install folder by hand for an offline machine. Results found this way are marked
              violet with a leading tilde.
              <div className="gp-callout__actions">
                <Button size="sm" variant="ai" label="Enable smart search"
                  loading={busy} disabled={busy || emb.state === 'ready'} onClick={enable} />
                {emb.state === 'ready' ? (
                  <Button size="sm" variant="ghost" label="Rebuild index"
                    onClick={() => api.embeddingsRebuild({ kinds: null, force: false })} />
                ) : null}
              </div>
            </div>
          </div>

          <div className="gp-formgrid gp-u-mt-5">
            <label className="gp-formgrid__label" htmlFor="smart-min-score">Match strictness</label>
            <div className="gp-field">
              <Select id="smart-min-score"
                value={String(config.smart_search_min_score ?? 0.3)}
                ariaLabel="How close a semantic match must be"
                onChange={(v) => patch({ smart_search_min_score: Number(v) })}
                options={STRICTNESS_LEVELS} />
              <span className="gp-field__hint">
                How similar a result must be before smart search will offer it. Stricter
                returns fewer, closer matches; looser casts a wider net and can surface
                items whose connection to the query is not obvious. Keyword matches are
                never filtered by this.
              </span>
            </div>
          </div>
        </>
      ) : <p className="gp-u-fs-11 gp-u-meta">Reading the embedding status...</p>}

      <div className="gp-details__section-head gp-u-mt-6"><span>Online enrichment</span></div>
      <div className="gp-formgrid">
        <span className="gp-formgrid__label">Civitai</span>
        <Toggle checked={config.civitai_enabled} label="Match hashed files against Civitai"
          onChange={(v) => patch({ civitai_enabled: v })} />
        <span className="gp-formgrid__label">Network</span>
        <Toggle checked={config.online_enabled} label="Allow outbound lookups at all"
          onChange={(v) => patch({ online_enabled: v })} />
      </div>

      <div className="gp-details__section-head gp-u-mt-6"><span>Local summaries</span></div>
      <div className="gp-formgrid">
        <span className="gp-formgrid__label">Enabled</span>
        <Toggle ai checked={config.ollama_enabled} label="Generate summaries with a local model"
          onChange={(v) => patch({ ollama_enabled: v })} />
        <label className="gp-formgrid__label" htmlFor="ollama-url">Server</label>
        <div className="gp-field">
          <input id="ollama-url" className="gp-input gp-input--mono" value={ollamaUrl}
            onChange={(e) => setOllamaUrl(e.target.value)} />
          <div className="gp-u-row gp-u-gap-3 gp-u-mt-4">
            <Button size="sm" label="Test" onClick={probe} />
            <Button size="sm" variant="ghost" label="Save"
              onClick={() => patch({ ollama_url: ollamaUrl })} />
          </div>
          {ollamaProbe ? (
            <span className="gp-field__hint">
              {ollamaProbe.available
                ? 'Reachable in ' + ollamaProbe.latency_ms + ' ms - ' +
                  (ollamaProbe.models || []).join(', ')
                : 'Not reachable: ' + ollamaProbe.reason}
            </span>
          ) : (
            <span className="gp-field__hint">
              Optional and entirely local. Anything written this way is marked as inferred.
              Currently: {(ai.data && ai.data.reason) || 'unknown'}.
            </span>
          )}
        </div>
      </div>
    </>
  )
}

/* ---------------------------------------------------------------------- jobs */

function JobsTab({ config }) {
  const { toast, toastError, refreshConfig } = useVault()
  const patch = useCallback(async (body) => {
    try {
      await api.updateConfig(body)
      await refreshConfig()
      toast({ tone: 'ok', title: 'Setting saved' })
    } catch (err) {
      toastError(err, 'Could not save the setting')
    }
  }, [refreshConfig, toast, toastError])

  return (
    <>
      <div className="gp-details__section-head"><span>Indexing</span></div>
      <div className="gp-formgrid">
        <span className="gp-formgrid__label">Automatic</span>
        <Toggle checked={config.auto_reindex} label="Reindex on startup when files changed"
          onChange={(v) => patch({ auto_reindex: v })} />
        <span className="gp-formgrid__label">Watcher</span>
        <Toggle checked={config.watch_enabled} label="Watch the folders for changes"
          onChange={(v) => patch({ watch_enabled: v })} />
        <span className="gp-formgrid__label">Held YAML</span>
        <Toggle checked={config.read_held_extra_paths}
          label="Also read extra_model_paths.yaml.hold"
          onChange={(v) => patch({ read_held_extra_paths: v })} />
      </div>

      <div className="gp-details__section-head gp-u-mt-6"><span>Deletion</span></div>
      <div className="gp-formgrid">
        <label className="gp-formgrid__label" htmlFor="trash-mode">Default</label>
        <Select id="trash-mode" value={config.trash_mode} ariaLabel="Default delete mode"
          onChange={(v) => patch({ trash_mode: v })}
          options={[
            { value: 'trash', label: 'Move to the vault trash (recoverable)' },
            { value: 'permanent', label: 'Delete permanently' }
          ]} />
        <label className="gp-formgrid__label" htmlFor="trash-days">Keep for</label>
        <div className="gp-field">
          <input id="trash-days" className="gp-input gp-input--num" type="number" min="1" max="365"
            defaultValue={config.trash_retention_days}
            onBlur={(e) => patch({ trash_retention_days: Number(e.target.value) })} />
          <span className="gp-field__hint">Days before trashed files are purged for good.</span>
        </div>
      </div>

      <div className="gp-details__section-head gp-u-mt-6"><span>External agents</span></div>
      <div className="gp-formgrid">
        <span className="gp-formgrid__label">Read only</span>
        <Toggle checked={config.mcp_read_only}
          label="Refuse file operations from connected agents"
          onChange={(v) => patch({ mcp_read_only: v })} />
      </div>
    </>
  )
}

/* ------------------------------------------------------------------- storage */

function StorageTab({ config }) {
  const { state, toast, toastError, invalidate } = useVault()
  const trash = useResource('trash', (s) => api.trash({ limit: 50 }, s), { epoch: state.dataEpoch })
  const [busy, setBusy] = useState(false)

  const gc = useCallback(async () => {
    setBusy(true)
    try {
      const res = await api.thumbsGc(config.thumb_cache_max_mb)
      toast({
        tone: 'ok',
        title: 'Thumbnail cache trimmed',
        message: fmtCount(res.deleted) + ' files, ' + bytes(res.freed_bytes) + ' freed'
      })
    } catch (err) {
      toastError(err, 'Could not trim the cache')
    } finally {
      setBusy(false)
    }
  }, [config.thumb_cache_max_mb, toast, toastError])

  const restore = useCallback(async (id) => {
    try {
      await api.trashRestore({ ids: [id], on_conflict: 'rename' })
      toast({ tone: 'ok', title: 'Restored' })
      trash.refresh()
      invalidate()
    } catch (err) {
      toastError(err, 'Restore failed')
    }
  }, [trash, toast, toastError, invalidate])

  const emptyTrash = useCallback(async () => {
    try {
      const res = await api.trashEmpty({ ids: null, older_than_days: null, confirm: true })
      toast({
        tone: 'warn',
        title: 'Trash emptied',
        message: fmtCount(res.purged) + ' items, ' + bytes(res.freed_bytes) + ' freed'
      })
      trash.refresh()
    } catch (err) {
      toastError(err, 'Could not empty the trash')
    }
  }, [trash, toast, toastError])

  const items = (trash.data && trash.data.items) || []
  const summary = (trash.data && trash.data.summary) || { count: 0, bytes: 0 }

  return (
    <>
      <div className="gp-details__section-head"><span>Thumbnail cache</span></div>
      <div className="gp-meta">
        <MetaRow label="budget" value={config.thumb_cache_max_mb + ' MB'} num />
        <MetaRow label="video frames"
          value={config.thumb_video_ffmpeg ? 'enabled' : null}
          empty="No frame extraction - install ffmpeg on PATH to enable it." />
      </div>
      <div className="gp-u-mt-4">
        <Button size="sm" icon={HardDrive} label="Trim the cache now" loading={busy} onClick={gc} />
      </div>

      <div className="gp-details__section-head gp-u-mt-6">
        <span>Trash</span>
        <Badge tone="neutral">{fmtCount(summary.count)} / {bytes(summary.bytes)}</Badge>
      </div>
      {items.length ? (
        <>
          <table className="gp-table gp-table--compact">
            <thead>
              <tr>
                <th>File</th><th>Kind</th>
                <th className="gp-table__num">Size</th>
                <th className="gp-table__num">Deleted</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id}>
                  <td className="gp-u-break-all">{t.filename}</td>
                  <td>{t.kind}</td>
                  <td className="gp-table__num">{bytes(t.size)}</td>
                  <td className="gp-table__num">{dateTime(t.deleted_at)}</td>
                  <td className="gp-table__num">
                    <Button size="sm" variant="ghost" label="Restore"
                      disabled={!t.restorable} onClick={() => restore(t.id)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="gp-u-mt-5">
            <Button size="sm" variant="dangerGhost" icon={Trash2}
              label="Empty the trash" onClick={emptyTrash} />
          </div>
        </>
      ) : (
        <EmptyState small icon={Trash2} title="The trash is empty"
          text="Deleted files land here first and can be restored until they are purged." />
      )}
    </>
  )
}

/* ------------------------------------------------------------------- wrapper */

export default function SettingsModal({ onClose, onReindex, onWizard, initialTab }) {
  const { state } = useVault()
  const [tab, setTab] = useState(initialTab || 'location')
  const config = state.config

  const body = useMemo(() => {
    if (!config) return <p className="gp-u-fs-11 gp-u-meta">Loading configuration...</p>
    if (tab === 'location') {
      return <LocationTab config={config} onReindex={onReindex} onWizard={onWizard} />
    }
    if (tab === 'search') return <SearchTab config={config} />
    if (tab === 'jobs') return <JobsTab config={config} />
    if (tab === 'activity') {
      // DECISIONS C5 rail 3: Settings -> Activity. Wrapped so a malformed audit
      // row can never take the whole Settings dialog down with it.
      return (
        <ErrorBoundary small title="The activity log could not be rendered">
          <Suspense fallback={<SkeletonRows rows={6} />}>
            <ActivityPanel />
          </Suspense>
        </ErrorBoundary>
      )
    }
    return <StorageTab config={config} />
  }, [tab, config, onReindex, onWizard])

  return (
    <Modal
      title="Settings"
      subtitle="Geekatplay ComfyUI Asset Vault"
      size="lg"
      onClose={onClose}
      footerLeft={(
        <span className="gp-u-fs-10 gp-u-meta">
          {(state.info && state.info.app) || 'Asset Vault'} {(state.info && state.info.version) || ''}
          {' / '}Vladimir Chopine
        </span>
      )}
      footer={<Button variant="primary" onClick={onClose}>Done</Button>}
    >
      <div className="gp-tabs gp-u-mb-6" role="tablist" aria-label="Settings sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={'gp-tab' + (tab === t.id ? ' gp-tab--active' : '')}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {body}
    </Modal>
  )
}
