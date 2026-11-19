import React, { useCallback, useState } from 'react'
import {
  RefreshCw, Plus, Trash2, ExternalLink, Globe, CheckCircle2, XCircle, Clock,
  Radar, Github, Star, WifiOff, Settings
} from 'lucide-react'
import api from '../../services/api.js'
import { useVault } from '../../state/VaultContext.jsx'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import Toggle from '../common/Toggle.jsx'
import Select from '../common/Select.jsx'
import ProgressBar from '../common/ProgressBar.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { ago, count as fmtCount, dateTime } from '../../services/format.js'
import { SectionHead, compactCount, isoDate } from './parts.jsx'

/*
 * SourcesPanel - where model information comes from, and when it last arrived.
 *
 * The catalog refreshes itself when it is more than a day old, but only while
 * outbound lookups are allowed - the same switch every other online feature in
 * the vault obeys. With that switch off the advisor keeps working from the
 * dataset shipped with the app, and says so here rather than failing quietly.
 */

const KIND_OPTIONS = [
  { value: '', label: 'Detect the type' },
  { value: 'image', label: 'Image' }, { value: 'video', label: 'Video' },
  { value: 'llm', label: 'Language model' }, { value: 'lora', label: 'LoRA' },
  { value: 'audio', label: 'Audio' }, { value: '3d', label: '3D' },
  { value: 'controlnet', label: 'ControlNet' }, { value: 'upscale', label: 'Upscaler' }
]

function UpdateCard({ update, settings, onStart, onCancel, onToggleAuto, onOpenSettings, busy }) {
  const running = update.running
  const pct = running && update.total ? (update.done / update.total) * 100 : null
  const last = update.last_run
  return (
    <section className="gp-inst-panel gp-updatecard">
      <div className="gp-updatecard__head">
        <div className="gp-u-minw0">
          <div className="gp-inst-hero__eyebrow">
            <span className={'gp-inst-live' + (running ? '' : ' gp-inst-live--idle')}
              aria-hidden="true" />
            Model catalog
          </div>
          <h3 className="gp-updatecard__title">
            {fmtCount(update.entries)} models
            <span className="gp-updatecard__age">
              {update.catalog_generated_at
                ? (update.catalog_origin === 'seed' ? 'shipped ' : 'refreshed ') +
                  ago(update.catalog_generated_at)
                : 'no data yet'}
            </span>
          </h3>
        </div>
        <div className="gp-updatecard__actions">
          {running ? (
            <Button icon={XCircle} label="Stop" onClick={onCancel} />
          ) : (
            <Button variant="primary" icon={RefreshCw} label="Update now"
              loading={busy} disabled={!update.online_enabled} onClick={onStart}
              title={update.online_enabled
                ? 'Ask every source for the current models'
                : 'Outbound lookups are switched off in Settings'} />
          )}
        </div>
      </div>

      {running ? (
        <ProgressBar percent={pct} indeterminate={pct === null}
          label={update.phase_label || 'Working'}
          value={update.total ? update.done + ' / ' + update.total : null} />
      ) : null}

      {!update.online_enabled ? (
        <div className="gp-callout gp-callout--info">
          <WifiOff className="gp-callout__icon" aria-hidden="true" />
          <div className="gp-callout__body">
            <div className="gp-callout__title">Working from the shipped dataset</div>
            Outbound lookups are switched off, so the catalog cannot refresh
            itself. Every recommendation still works; it reflects the models
            known when this dataset was built.
            <div className="gp-u-mt-4">
              <Button size="sm" icon={Settings} label="Open Settings"
                onClick={onOpenSettings} />
            </div>
          </div>
        </div>
      ) : null}

      {update.error ? (
        <p className="gp-u-fs-11 gp-u-danger">Last refresh stopped: {update.error}</p>
      ) : null}

      <div className="gp-updatecard__foot">
        <Toggle id="advisor-auto-update" label="Refresh on its own when over a day old"
          checked={Boolean(settings && settings.auto_update)}
          disabled={!settings} onChange={onToggleAuto}
          title="Runs shortly after the app starts, only while outbound lookups are allowed" />
        <span className="gp-u-fs-11 gp-u-meta gp-u-auto-l">
          {update.stale ? 'Due for a refresh' : 'Up to date'}
          {last && last.finished_at ? ' - last run ' + dateTime(last.finished_at) +
            (last.trigger === 'auto' ? ' (automatic)' : '') : ''}
          {last && last.result && last.result.new ? ' - ' + last.result.new + ' new' : ''}
        </span>
      </div>
    </section>
  )
}

export default function SourcesPanel(props) {
  const { data, loading, error, settings, onChanged, onOpenSettings } = props
  const { toast, toastError } = useVault()
  const [repo, setRepo] = useState('')
  const [kind, setKind] = useState('')
  const [busy, setBusy] = useState(false)

  const start = useCallback(() => {
    setBusy(true)
    api.advisorUpdate({})
      .then(() => onChanged())
      .catch((err) => toastError(err, 'The refresh could not start'))
      .finally(() => setBusy(false))
  }, [onChanged, toastError])

  const cancel = useCallback(() => {
    api.advisorUpdateCancel().then(() => onChanged())
      .catch((err) => toastError(err, 'The refresh could not be stopped'))
  }, [onChanged, toastError])

  const toggleAuto = useCallback((value) => {
    api.advisorPatchSettings({ auto_update: value }).then(() => onChanged())
      .catch((err) => toastError(err, 'The setting could not be saved'))
  }, [onChanged, toastError])

  const addPin = useCallback((e) => {
    e.preventDefault()
    const body = { repo: repo.trim() }
    if (kind) body.kind = kind
    api.advisorAddPin(body)
      .then((pin) => {
        setRepo('')
        toast({ tone: 'ok', title: 'Pinned', message: pin.repo +
          ' is read on every refresh from now on.' })
        onChanged()
      })
      .catch((err) => toastError(err, 'That repository could not be pinned'))
  }, [repo, kind, toast, toastError, onChanged])

  const removePin = useCallback((name) => {
    api.advisorRemovePin(name).then(() => onChanged())
      .catch((err) => toastError(err, 'The pin could not be removed'))
  }, [onChanged, toastError])

  if (error) {
    return <EmptyState tone="error" title="The source list could not be loaded"
      text={error.message} />
  }
  if (loading && !data) return <SkeletonRows rows={12} />
  if (!data) return null

  return (
    <div className="gp-adv">
      <UpdateCard update={data.update} settings={settings} busy={busy}
        onStart={start} onCancel={cancel} onToggleAuto={toggleAuto}
        onOpenSettings={onOpenSettings} />

      <section className="gp-inst-panel gp-inst-panel--flat">
        <SectionHead label="Sources" count={data.sources.length}
          sub={'source list revision ' + data.revision + ' (' + data.origin + ')'} />
        <div className="gp-sources">
          {data.sources.map((s) => (
            <div key={s.id} className="gp-source">
              <div className="gp-source__head">
                {s.id === 'github'
                  ? <Github className="gp-inst-stat__icon" aria-hidden="true" />
                  : <Globe className="gp-inst-stat__icon" aria-hidden="true" />}
                <a className="gp-source__name" href={s.url} target="_blank"
                  rel="noopener noreferrer">{s.name}</a>
                <Badge mono>{s.trust}</Badge>
                <span className="gp-u-auto-l" />
                {s.last_ok === true ? (
                  <CheckCircle2 className="gp-source__state gp-u-ok" aria-label="Last refresh succeeded" />
                ) : (s.last_ok === false ? (
                  <XCircle className="gp-source__state gp-u-danger" aria-label="Last refresh failed" />
                ) : (
                  <Clock className="gp-source__state gp-u-meta" aria-label="Not asked yet" />
                ))}
              </div>
              <p className="gp-source__text">{s.provides}</p>
              <div className="gp-source__foot">
                <span className="gp-u-num gp-u-fg gp-u-fw-600">{fmtCount(s.entries)}</span>
                <span>{s.id === 'github' ? 'leads below' : 'models'}</span>
                <span className="gp-u-auto-l" />
                <span title={s.last_at ? dateTime(s.last_at) : undefined}>
                  {s.last_at ? 'asked ' + ago(s.last_at) : 'from the shipped dataset'}
                </span>
              </div>
              {s.last_error
                ? <p className="gp-u-fs-11 gp-u-danger">{s.last_error}</p> : null}
            </div>
          ))}
        </div>
        <p className="gp-inst-note">
          The advisor only ever reads these hosts: {data.allowed_hosts.join(', ')}.
          Requests carry no account, no key and nothing about this machine or
          your library.
        </p>
      </section>

      <div className="gp-inst-row gp-inst-row--two">
        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="How it finds models" />
          <div className="gp-meta">
            <div className="gp-meta__row"><span className="gp-meta__key">Pinned repositories</span>
              <span className="gp-meta__leader" /><span className="gp-meta__val gp-meta__val--num">{data.pins}</span></div>
            <div className="gp-meta__row"><span className="gp-meta__key">Your pins</span>
              <span className="gp-meta__leader" /><span className="gp-meta__val gp-meta__val--num">{data.user_pins.length}</span></div>
            <div className="gp-meta__row"><span className="gp-meta__key">Discovery queries</span>
              <span className="gp-meta__leader" /><span className="gp-meta__val gp-meta__val--num">
                {Object.values(data.queries || {}).reduce((a, n) => a + n, 0)}</span></div>
            <div className="gp-meta__row"><span className="gp-meta__key">Recognised publishers</span>
              <span className="gp-meta__leader" /><span className="gp-meta__val gp-meta__val--num">
                {(data.publishers.official || 0) + (data.publishers.trusted || 0)}</span></div>
            <div className="gp-meta__row"><span className="gp-meta__key">Learned by following links</span>
              <span className="gp-meta__leader" /><span className="gp-meta__val gp-meta__val--num">{data.learned_total}</span></div>
          </div>
          <p className="gp-inst-note">
            <Radar className="gp-inst-note__icon" aria-hidden="true" />
            Each refresh reads the pins, asks every source what is most used and
            what is trending per model type, and follows a repackaged model
            back to the original it was made from.
          </p>
        </section>

        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="Pin a repository" sub="Hugging Face, owner/name" />
          <form className="gp-pinform" onSubmit={addPin}>
            <input className="gp-input gp-input--mono" value={repo}
              aria-label="Hugging Face repository, owner slash name"
              placeholder="owner/name" maxLength={200}
              onChange={(e) => setRepo(e.target.value)} />
            <Select value={kind} onChange={setKind} options={KIND_OPTIONS}
              ariaLabel="Model type" />
            <Button type="submit" icon={Plus} label="Pin"
              disabled={!/^[^/\s]+\/[^/\s]+$/.test(repo.trim()
                .replace('https://huggingface.co/', ''))} />
          </form>
          {data.user_pins.length ? (
            <ul className="gp-pins">
              {data.user_pins.map((p) => (
                <li key={p.repo} className="gp-pins__row">
                  <span className="gp-u-num gp-u-truncate">{p.repo}</span>
                  {p.kind ? <Badge mono>{p.kind}</Badge> : null}
                  <span className="gp-u-auto-l" />
                  <Button variant="ghost" size="sm" iconOnly icon={Trash2}
                    aria-label={'Remove the pin for ' + p.repo} title="Remove this pin"
                    onClick={() => removePin(p.repo)} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="gp-inst-note">
              A pinned repository is read on every refresh, so a model you care
              about is always in the catalog with its current files.
            </p>
          )}
        </section>
      </div>

      {(data.leads || []).length ? (
        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="New on GitHub" count={data.leads.length}
            sub="projects worth a look - not catalog entries" />
          <ul className="gp-leads">
            {data.leads.slice(0, 18).map((lead) => (
              <li key={lead.url} className="gp-lead">
                <a className="gp-lead__name" href={lead.url} target="_blank"
                  rel="noopener noreferrer">
                  {lead.name}<ExternalLink size={10} aria-hidden="true" />
                </a>
                <span className="gp-lead__stars">
                  <Star size={10} aria-hidden="true" />{compactCount(lead.stars) || 0}
                </span>
                <span className="gp-lead__text">{lead.description}</span>
                <span className="gp-lead__date">{isoDate(lead.updated_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}
