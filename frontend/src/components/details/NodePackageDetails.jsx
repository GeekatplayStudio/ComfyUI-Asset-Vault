import React, { useCallback, useState } from 'react'
import {
  ExternalLink, FolderOpen, RefreshCw, AlertTriangle, Package, ShieldAlert
} from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge, { ConfidenceBadge } from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, DetailsFallback } from './MetaRow.jsx'
import { bytes, dateTime, count as fmtCount, humanise } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * NodePackageDetails - what a custom node package installs, where it came from,
 * how its classes were extracted, and whether a newer commit exists.
 */
export default function NodePackageDetails({ id, onOpenUid, onFilterPackage }) {
  const { state, toast, toastError } = useVault()
  const epoch = state.dataEpoch
  const [checking, setChecking] = useState(false)

  const detail = useResource('node_package:' + id, (s) => api.nodePackage(id, s), { epoch })
  const pkg = detail.data

  const checkUpdate = useCallback(async () => {
    setChecking(true)
    try {
      const res = await api.checkPackageUpdate(id)
      if (res && res.state === 'suspect_remote') {
        toast({
          tone: 'warn',
          title: 'Remote does not match the folder',
          message: res.reason || 'The declared repository does not look like this package.'
        })
      } else {
        toast({ tone: 'ok', title: 'Update check queued' })
      }
      detail.refresh()
    } catch (err) {
      toastError(err, 'Update check unavailable')
    } finally {
      setChecking(false)
    }
  }, [id, detail, toast, toastError])

  const onReveal = useCallback(async () => {
    try {
      await api.reveal('node_package:' + id)
    } catch (err) {
      toastError(err, 'Could not open the folder')
    }
  }, [id, toastError])

  if (detail.loading && !pkg) {
    return (
      <DetailsFallback eyebrow="Node package" title="Loading package">
        <SkeletonMeta rows={8} />
      </DetailsFallback>
    )
  }
  if (detail.error) {
    return (
      <DetailsFallback eyebrow="Node package" title="Unavailable">
        <EmptyState tone="error" small icon={AlertTriangle}
          title="Could not load this package" text={detail.error.message}
          actions={<Button onClick={detail.refresh}>Retry</Button>} />
      </DetailsFallback>
    )
  }
  if (!pkg) return null

  const extraction = pkg.extraction || {}
  const repo = pkg.repo || {}
  const update = pkg.update || {}
  const inferred = extraction.confidence === 'inferred'

  return (
    <>
      <div className={'gp-details__header' + (inferred ? ' gp-details__header--inferred' : '')}>
        <div className="gp-details__eyebrow">
          {pkg.is_official ? 'Official ComfyUI' : 'Custom node package'}
        </div>
        <h2 className="gp-details__title">{pkg.display_name || pkg.folder_name}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          <Badge tone="mono">{fmtCount(pkg.class_count)} classes</Badge>
          {pkg.author ? <Badge tone="role">{pkg.author}</Badge> : null}
          <ConfidenceBadge confidence={extraction.confidence}
            title={'Class extraction confidence - strategies ' +
              ((extraction.strategies || []).join(', ') || 'none')} />
          {pkg.enabled ? null : <Badge tone="warn">disabled</Badge>}
          {update.has_update ? <Badge tone="info">update</Badge> : null}
        </div>
      </div>

      <div className="gp-details__body">
        <div className="gp-u-row gp-u-gap-3 gp-u-mb-6 gp-u-wrap">
          <Button size="sm" icon={Package}
            label={'Show ' + fmtCount(pkg.class_count) + ' classes'}
            onClick={() => onFilterPackage(pkg)} />
          <Button size="sm" variant="ghost" icon={RefreshCw} label="Check update"
            loading={checking} onClick={checkUpdate} />
          <Button size="sm" variant="ghost" icon={FolderOpen} label="Reveal"
            onClick={onReveal} />
        </div>

        {repo.suspect ? (
          <div className="gp-callout gp-callout--warn gp-u-mb-6">
            <span className="gp-callout__icon"><ShieldAlert aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">Remote does not match the folder</div>
              The git remote recorded for this package points somewhere unrelated. Update checks
              for it are unreliable.
            </div>
          </div>
        ) : null}

        {pkg.description ? (
          <Section title="Description">
            <p className="gp-u-fs-12 gp-u-muted">{pkg.description}</p>
          </Section>
        ) : null}

        <Section title="Package">
          <div className="gp-meta">
            <MetaRow label="folder" value={pkg.folder_name} />
            <MetaRow label="author" value={pkg.author} />
            <MetaRow label="publisher" value={pkg.publisher_id} />
            <MetaRow label="version" value={pkg.version} />
            <MetaRow label="license" value={pkg.license} />
            <MetaRow label="classes" value={fmtCount(pkg.class_count)} num />
            <MetaRow label="files" value={fmtCount(pkg.file_count)} num />
            <MetaRow label="size" value={bytes(pkg.size)} num />
            <MetaRow label="web directory" value={pkg.has_web_directory ? 'yes' : 'no'} />
            <MetaRow label="used by" value={fmtCount((pkg.counts && pkg.counts.workflows) || 0) + ' workflows'} num />
            <MetaRow label="path" value={pkg.abs_path} wrap />
          </div>
        </Section>

        {pkg.comfyui_version || pkg.source_breakdown ? (
          <Section title="ComfyUI core">
            <div className="gp-meta">
              <MetaRow label="version" value={pkg.comfyui_version} />
              {Object.entries(pkg.source_breakdown || {}).map(([k, v]) => (
                <MetaRow key={k} label={k} value={fmtCount(v)} num />
              ))}
            </div>
          </Section>
        ) : null}

        <Section title="Repository">
          <div className="gp-meta">
            <MetaRow label="branch" value={repo.branch} />
            <MetaRow label="commit" value={repo.commit} />
            <MetaRow label="committed" value={dateTime(repo.commit_at)} />
            <MetaRow label="update state" value={humanise(update.state)} />
            <MetaRow label="commits behind" value={update.commits_behind} num />
            <MetaRow label="checked" value={dateTime(update.checked_at)}
              empty="Never checked. Online checks are off by default." />
          </div>
          {repo.url ? (
            <a className="gp-btn gp-btn--sm gp-u-mt-4" href={repo.url}
              target="_blank" rel="noreferrer noopener">
              <ExternalLink className="gp-btn__icon" aria-hidden="true" />
              <span className="gp-btn__label">Open repository</span>
            </a>
          ) : null}
        </Section>

        <Section title="Class extraction">
          <div className="gp-meta">
            <MetaRow label="status" value={extraction.status} />
            <MetaRow label="strategies" value={(extraction.strategies || []).join(', ')} />
            <MetaRow label="confidence" value={extraction.confidence}
              inferred={inferred}
              inferredTitle="Classes were recovered by reading the package source, not from a declared map" />
            <MetaRow label="notes" value={extraction.notes} wrap />
          </div>
        </Section>

        {pkg.python_deps && pkg.python_deps.length ? (
          <Section title={'Python requirements (' + pkg.python_deps.length + ')'}>
            <ul className="gp-confirm__list">
              {pkg.python_deps.map((d, i) => <li key={d + ':' + i}>{d}</li>)}
            </ul>
            <p className="gp-u-fs-10 gp-u-meta gp-u-mt-4">
              Listed only. This application never installs a package&apos;s requirements for you.
            </p>
          </Section>
        ) : null}

        {pkg.class_categories && pkg.class_categories.length ? (
          <Section title="Class categories">
            <table className="gp-table gp-table--compact">
              <tbody>
                {pkg.class_categories.slice(0, 20).map((c, i) => (
                  <tr key={(c.category || 'none') + ':' + i}>
                    <td>{c.category || '(uncategorised)'}</td>
                    <td className="gp-table__num">{fmtCount(c.count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        ) : null}

        {pkg.top_classes && pkg.top_classes.length ? (
          <Section title="Classes">
            <div className="gp-list">
              {pkg.top_classes.slice(0, 24).map((c) => (
                <button key={c.uid} type="button" className="gp-row gp-focus-inset"
                  onClick={() => onOpenUid(c.uid)} title={c.node_id}>
                  <span className="gp-row__name">{c.display_name || c.node_id}</span>
                  <span className="gp-row__cell gp-row__cell--grow">{c.category}</span>
                </button>
              ))}
            </div>
          </Section>
        ) : null}
      </div>
    </>
  )
}
