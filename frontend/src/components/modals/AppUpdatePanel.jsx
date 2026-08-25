import React, { useCallback, useState } from 'react'
import {
  AlertTriangle, CheckCircle2, Download, ExternalLink, RefreshCw, ShieldQuestion, Trash2
} from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import Toggle from '../common/Toggle.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { bytes, dateTime } from '../../services/format.js'

/*
 * AppUpdatePanel - Settings -> Updates.
 *
 * Downloading and installing are deliberately two moments, and the panel says
 * so: a downloaded release sits inert in backend/data/updates until the next
 * launch, when the launcher applies it with nothing running. That is why there
 * is no "install now" button that restarts the app underneath the user.
 */

/** Release notes arrive as GitHub-flavoured markdown; render the shape only. */
function Notes({ text }) {
  if (!text) return null
  const lines = String(text).split('\n').slice(0, 400)
  return (
    <div className="gp-relnotes">
      {lines.map((raw, i) => {
        const line = raw.trimEnd()
        const heading = /^#{1,6}\s+/.test(line)
        const bullet = /^\s*[-*]\s+/.test(line)
        if (!line.trim()) return <div key={i} className="gp-relnotes__gap" />
        if (heading) {
          return (
            <div key={i} className="gp-relnotes__head">
              {line.replace(/^#{1,6}\s+/, '')}
            </div>
          )
        }
        if (bullet) {
          return (
            <div key={i} className="gp-relnotes__item">
              {line.replace(/^\s*[-*]\s+/, '')}
            </div>
          )
        }
        return <div key={i} className="gp-relnotes__line">{line}</div>
      })}
    </div>
  )
}

export default function AppUpdatePanel({ config }) {
  const { state, toast, toastError, refreshConfig } = useVault()
  const [busy, setBusy] = useState(false)
  const status = useResource('app-update', (s) => api.appUpdate(s), { epoch: state.dataEpoch })
  const data = status.data

  const patch = useCallback(async (body) => {
    try {
      await api.updateConfig(body)
      await refreshConfig()
      toast({ tone: 'ok', title: 'Setting saved' })
      status.refresh()
    } catch (err) {
      toastError(err, 'Could not save the setting')
    }
  }, [refreshConfig, toast, toastError, status])

  const recheck = useCallback(async () => {
    setBusy(true)
    try {
      await api.appUpdateCheck()
      status.refresh()
    } catch (err) {
      toastError(err, 'The update check failed')
    } finally {
      setBusy(false)
    }
  }, [status, toastError])

  const download = useCallback(async () => {
    setBusy(true)
    try {
      const res = await api.appUpdateDownload()
      toast({
        tone: 'ok',
        title: 'Update downloaded',
        message: 'Version ' + ((res.pending && res.pending.version) || '')
          + ' will be applied the next time you start the vault.'
      })
      status.refresh()
    } catch (err) {
      toastError(err, 'The update could not be downloaded')
    } finally {
      setBusy(false)
    }
  }, [status, toast, toastError])

  const discard = useCallback(async () => {
    setBusy(true)
    try {
      await api.appUpdateDiscard()
      toast({ tone: 'ok', title: 'Downloaded update discarded' })
      status.refresh()
    } catch (err) {
      toastError(err, 'Could not discard the download')
    } finally {
      setBusy(false)
    }
  }, [status, toast, toastError])

  if (status.loading && !data) return <SkeletonMeta rows={8} />
  if (!data) {
    return (
      <p className="gp-u-fs-11 gp-u-meta">
        The update status could not be read. {status.error ? status.error.message : ''}
      </p>
    )
  }

  const pending = data.pending
  const offline = data.state === 'offline'
  const disabled = data.state === 'disabled'

  return (
    <>
      <div className="gp-details__section-head"><span>This installation</span></div>
      <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mb-4">
        <Badge tone="mono">installed v{data.current_version}</Badge>
        {data.latest_version
          ? <Badge tone={data.has_update ? 'info' : 'ok'}>
              latest v{data.latest_version}
            </Badge>
          : null}
        {pending
          ? <Badge tone="brand">v{pending.version} ready to install</Badge>
          : null}
      </div>
      <div className="gp-meta">
        <MetaRow label="repository" value={data.repository} />
        <MetaRow label="last checked" value={data.last_check ? dateTime(data.last_check) : null}
          empty="Never." />
        <MetaRow label="published" value={data.published_at ? dateTime(Date.parse(data.published_at)) : null} />
        <MetaRow label="download size" value={data.download_bytes ? bytes(data.download_bytes) : null} />
      </div>

      {/* ------------------------------------------------------- the verdict */}
      {pending ? (
        <div className="gp-callout gp-callout--ok gp-u-mt-5">
          <span className="gp-callout__icon"><CheckCircle2 aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">
              Version {pending.version} is downloaded and waiting
            </div>
            It is not installed yet. Close the vault and start it again — the
            launcher applies the update before the engine starts, so nothing is
            replaced while the app is running. Your library, settings and
            database are untouched, and the previous version is kept so an
            update can be undone.
            <div className="gp-callout__actions">
              <Button size="sm" variant="ghost" icon={Trash2} label="Discard the download"
                loading={busy} disabled={busy} onClick={discard} />
            </div>
          </div>
        </div>
      ) : disabled ? (
        <div className="gp-callout gp-callout--info gp-u-mt-5">
          <span className="gp-callout__icon"><ShieldQuestion aria-hidden="true" /></span>
          <div className="gp-callout__body">
            Update checks are off. Turn them on below to be told when a new
            release is published.
          </div>
        </div>
      ) : offline ? (
        <div className="gp-callout gp-callout--warn gp-u-mt-5">
          <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">Outbound lookups are disabled</div>
            The vault cannot ask GitHub what the newest release is. Turn on
            <strong> Settings → Search → Allow outbound lookups at all</strong>,
            or check the releases page yourself.
          </div>
        </div>
      ) : data.has_update ? (
        <div className="gp-callout gp-callout--info gp-u-mt-5">
          <span className="gp-callout__icon"><Download aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">
              Version {data.latest_version} is available
            </div>
            {data.downloadable === false
              ? 'That release publishes no archive, so it has to be installed by hand from the releases page.'
              : 'Downloading fetches the release archive and checks it against the checksum published with it. Nothing is replaced until you restart.'}
            <div className="gp-callout__actions">
              {data.downloadable === false ? null : (
                <Button size="sm" variant="primary" icon={Download}
                  label="Download this update" loading={busy} disabled={busy}
                  onClick={download} />
              )}
              <a className="gp-btn gp-btn--ghost gp-btn--sm" href={data.releases_url}
                target="_blank" rel="noopener noreferrer">
                <ExternalLink className="gp-btn__icon" aria-hidden="true" />
                <span className="gp-btn__label">Release page</span>
              </a>
            </div>
          </div>
        </div>
      ) : data.state === 'error' ? (
        <div className="gp-callout gp-callout--warn gp-u-mt-5">
          <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">The check did not complete</div>
            {data.reason}
          </div>
        </div>
      ) : (
        <div className="gp-callout gp-callout--ok gp-u-mt-5">
          <span className="gp-callout__icon"><CheckCircle2 aria-hidden="true" /></span>
          <div className="gp-callout__body">
            {data.latest_version
              ? 'This is the newest published release.'
              : (data.reason || 'No newer release was found.')}
          </div>
        </div>
      )}

      <div className="gp-u-mt-4">
        <Button size="sm" variant="ghost" icon={RefreshCw} label="Check again"
          loading={busy || status.loading} disabled={busy || disabled || offline}
          onClick={recheck} />
      </div>

      {/* -------------------------------------------------------- what changed */}
      {(pending && pending.notes) || data.notes ? (
        <>
          <div className="gp-details__section-head gp-u-mt-6">
            <span>What changed in v{(pending && pending.version) || data.latest_version}</span>
          </div>
          <Notes text={(pending && pending.notes) || data.notes} />
        </>
      ) : null}

      {/* ------------------------------------------------------------ settings */}
      <div className="gp-details__section-head gp-u-mt-6"><span>Preferences</span></div>
      <div className="gp-formgrid">
        <span className="gp-formgrid__label">Check</span>
        <Toggle checked={config.app_update_check_enabled}
          label="Tell me when a new release is published"
          onChange={(v) => patch({ app_update_check_enabled: v })} />
        <span className="gp-formgrid__label">Download</span>
        <div className="gp-field">
          <Toggle checked={config.app_update_auto_download}
            label="Download new releases automatically"
            onChange={(v) => patch({ app_update_auto_download: v })} />
          <span className="gp-field__hint">
            Off by default. A downloaded update still installs only when you
            restart the vault — this setting saves the click, it never replaces
            files behind your back.
          </span>
        </div>
      </div>

      <div className="gp-callout gp-callout--info gp-u-mt-5">
        <span className="gp-callout__icon"><ShieldQuestion aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <div className="gp-callout__title">What is checked, and what is not</div>
          Updates come only from the pinned repository above — no setting can
          point this at another project. The archive is checked against the
          SHA-256 published with the release, which proves it arrived intact.
          It is <strong>not</strong> proof of authorship: the checksum travels
          from the same server as the file. This app does not ship signed
          releases, and does not pretend to.
        </div>
      </div>
    </>
  )
}
