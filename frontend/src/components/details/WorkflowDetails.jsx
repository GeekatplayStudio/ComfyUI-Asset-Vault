import React, { useCallback, useState } from 'react'
import {
  AlertTriangle, FolderOpen, Download, Maximize2, Image as ImageIcon, ExternalLink
} from 'lucide-react'
import api, { thumbnailUrl, downloadUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, DetailsFallback } from './MetaRow.jsx'
import DependencyList from './DependencyList.jsx'
import OpenInComfyUIDialog from '../modals/OpenInComfyUIDialog.jsx'
import { bytes, dateTime, count as fmtCount } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * WorkflowDetails - what a workflow does, what it needs, and whether it will
 * run right now. 159 of the 211 indexed workflows are missing something, so the
 * dependency report is the primary content, not a footnote.
 */
export default function WorkflowDetails({ id, onOpenUid, onLightbox }) {
  const { state, toastError } = useVault()
  const epoch = state.dataEpoch
  const [openInComfy, setOpenInComfy] = useState(false)

  const detail = useResource('workflow:' + id, (s) => api.workflow(id, s), { epoch })
  const deps = useResource('workflow-deps:' + id, (s) => api.workflowDependencies(id, s), { epoch })
  const wf = detail.data

  const onReveal = useCallback(async () => {
    try {
      await api.reveal('workflow:' + id)
    } catch (err) {
      toastError(err, 'Could not open the folder')
    }
  }, [id, toastError])

  if (detail.loading && !wf) {
    return (
      <DetailsFallback eyebrow="Workflow" title="Loading workflow">
        <SkeletonMeta rows={9} />
      </DetailsFallback>
    )
  }
  if (detail.error) {
    return (
      <DetailsFallback eyebrow="Workflow" title="Unavailable">
        <EmptyState tone="error" small icon={AlertTriangle}
          title="Could not load this workflow" text={detail.error.message}
          actions={<Button onClick={detail.refresh}>Retry</Button>} />
      </DetailsFallback>
    )
  }
  if (!wf) return null

  const counts = wf.counts || {}
  const missing = (counts.missing_nodes || 0) + (counts.missing_models || 0)
  const derived = wf.description_source === 'derived' || wf.description_source === 'ollama'

  return (
    <>
      <div className={'gp-details__header' + (derived ? ' gp-details__header--inferred' : '')}>
        <div className="gp-details__eyebrow">{wf.folder || 'workflow root'}</div>
        <h2 className="gp-details__title">{wf.name}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          {wf.base_model && wf.base_model !== 'Unknown'
            ? <Badge tone="base">{wf.base_model}</Badge> : null}
          {wf.modality ? <Badge tone="media">{wf.modality}</Badge> : null}
          <Badge tone="mono">{fmtCount(counts.nodes)} nodes</Badge>
          {missing
            ? <Badge tone="dep-missing">{missing} missing</Badge>
            : <Badge tone="dep-satisfied">runnable</Badge>}
        </div>
      </div>

      <div className="gp-details__body">
        <button type="button" className="gp-details__hero"
          onClick={() => onLightbox(wf.uid)}
          aria-label="Open a full size preview" title="Open a full size preview">
          <img src={thumbnailUrl(wf.uid, 640)} width="640" height="400"
            loading="lazy" decoding="async" alt="" />
        </button>

        <div className="gp-u-row gp-u-gap-3 gp-u-mb-6 gp-u-wrap">
          <Button size="sm" icon={Maximize2} label="Preview"
            onClick={() => onLightbox(wf.uid)} />
          <a className="gp-btn gp-btn--sm" href={downloadUrl(wf.uid)}>
            <Download className="gp-btn__icon" aria-hidden="true" />
            <span className="gp-btn__label">Download JSON</span>
          </a>
          <Button size="sm" variant="ghost" icon={FolderOpen} label="Reveal"
            onClick={onReveal} />
          {/* Opening it where it is meant to run. The dialog asks before it
              starts anything - this button only ever opens a dialog. */}
          <Button size="sm" variant="primary" icon={ExternalLink}
            label="Open in ComfyUI"
            title="Open this workflow in ComfyUI, starting ComfyUI first if you confirm it"
            onClick={() => setOpenInComfy(true)} />
        </div>

        {missing ? (
          <div className="gp-callout gp-callout--warn gp-u-mb-6">
            <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">This workflow will not run as it stands</div>
              {counts.missing_nodes || 0} node class(es) and {counts.missing_models || 0} model
              file(s) it references were not found. The list below names each one, and where a
              package is known it links to its repository.
            </div>
          </div>
        ) : null}

        {wf.description ? (
          <Section title="What it does">
            <p className={'gp-u-fs-12 ' + (derived ? 'gp-u-ai' : 'gp-u-muted')}>
              {derived
                ? <span className="gp-inferred"
                  title={'Summary derived locally from the graph (' + wf.description_source + ')'}>
                  {wf.description}
                </span>
                : wf.description}
            </p>
          </Section>
        ) : null}

        {wf.capability_tags && wf.capability_tags.length ? (
          <Section title="Capabilities">
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap">
              {wf.capability_tags.map((t) => (
                <Badge key={t} tone="neutral"
                  title="Derived from the node classes present in the graph">
                  <span className="gp-inferred gp-inferred--nomark">{t}</span>
                </Badge>
              ))}
            </div>
          </Section>
        ) : null}

        {wf.positive_prompt || wf.negative_prompt || wf.prompt_summary ? (
          <Section title="Prompt">
            {wf.positive_prompt || wf.prompt_summary ? (
              <div className="gp-prompt">{wf.positive_prompt || wf.prompt_summary}</div>
            ) : null}
            {wf.negative_prompt ? (
              <div className="gp-prompt gp-prompt--negative gp-u-mt-4">{wf.negative_prompt}</div>
            ) : null}
          </Section>
        ) : null}

        <Section title="Dependencies">
          {deps.loading && !deps.data ? <SkeletonMeta rows={6} /> : null}
          {deps.error ? (
            <EmptyState small tone="error" icon={AlertTriangle}
              title="Dependency report unavailable" text={deps.error.message}
              actions={<Button onClick={deps.refresh}>Retry</Button>} />
          ) : null}
          <DependencyList deps={deps.data} onOpenUid={onOpenUid} />
        </Section>

        {wf.node_breakdown && wf.node_breakdown.length ? (
          <Section title={'Node breakdown (' + wf.node_breakdown.length + ' classes)'}>
            <table className="gp-table gp-table--compact">
              <thead>
                <tr>
                  <th>Class</th>
                  <th className="gp-table__num">Count</th>
                  <th>Package</th>
                </tr>
              </thead>
              <tbody>
                {wf.node_breakdown.map((n, i) => (
                  <tr key={n.class_type + ':' + i}>
                    <td>
                      {n.resolved && n.uid ? (
                        <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm"
                          onClick={() => onOpenUid(n.uid)}>
                          <span className="gp-btn__label">{n.class_type}</span>
                        </button>
                      ) : n.class_type}
                    </td>
                    <td className="gp-table__num">{n.count}</td>
                    <td>
                      {n.package
                        ? n.package.name
                        : <span className="gp-u-danger">unresolved</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        ) : null}

        {wf.outputs_recent && wf.outputs_recent.length ? (
          <Section title="Outputs from this workflow">
            <div className="gp-grid">
              {wf.outputs_recent.slice(0, 8).map((o) => (
                <button key={o.uid} type="button" className="gp-card"
                  onClick={() => onOpenUid(o.uid)} title={o.filename}>
                  <span className="gp-card__thumb">
                    <img className="gp-card__media"
                      src={o.thumbnail_url || thumbnailUrl(o.uid, 160)}
                      width={160} height={160} loading="lazy" decoding="async" alt="" />
                  </span>
                </button>
              ))}
            </div>
          </Section>
        ) : (
          <Section title="Outputs from this workflow">
            <EmptyState small icon={ImageIcon} title="No outputs traced back here"
              text="No indexed output carries this workflow's signature." />
          </Section>
        )}

        <Section title="File">
          <div className="gp-meta">
            <MetaRow label="format" value={wf.format} />
            <MetaRow label="source" value={wf.source} />
            <MetaRow label="nodes" value={fmtCount(counts.nodes)} num />
            <MetaRow label="links" value={fmtCount(counts.links)} num />
            <MetaRow label="groups" value={fmtCount(counts.groups)} num />
            <MetaRow label="subgraphs" value={wf.has_subgraphs ? 'yes' : 'no'} />
            <MetaRow label="unresolved inputs" value={wf.unresolved_inputs} num
              tone={wf.unresolved_inputs ? 'danger' : undefined} />
            <MetaRow label="size" value={bytes(wf.size)} num />
            <MetaRow label="modified" value={dateTime(wf.modified_at)} />
            <MetaRow label="path" value={wf.abs_path || wf.rel_path} wrap />
          </div>
        </Section>
      </div>

      {openInComfy ? (
        <OpenInComfyUIDialog uid={wf.uid} name={wf.name}
          onClose={() => setOpenInComfy(false)} />
      ) : null}
    </>
  )
}
