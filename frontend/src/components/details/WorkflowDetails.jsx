import React, { useCallback, useState } from 'react'
import {
  AlertTriangle, FolderOpen, Download, Maximize2, Image as ImageIcon, ExternalLink, PackageSearch,
  LayoutTemplate, Lock, BookOpen
} from 'lucide-react'
import api, { thumbnailUrl, downloadUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import Chip from '../common/Chip.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import { TASK_LABELS, RUNS_ON_LABELS } from '../../state/actions.js'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, DetailsFallback } from './MetaRow.jsx'
import DependencyList, { isHttps } from './DependencyList.jsx'
import OpenInComfyUIDialog from '../modals/OpenInComfyUIDialog.jsx'
import ResolveNodesDialog from '../modals/ResolveNodesDialog.jsx'
import { bytes, dateTime, count as fmtCount } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * WorkflowDetails - what a workflow does, what it needs, and whether it will
 * run right now. 159 of the 211 indexed workflows are missing something, so the
 * dependency report is the primary content, not a footnote.
 */
export default function WorkflowDetails({ id, onOpenUid, onLightbox }) {
  const { state, dispatch, toastError } = useVault()
  const epoch = state.dataEpoch
  const [openInComfy, setOpenInComfy] = useState(false)
  const [resolveNodes, setResolveNodes] = useState(false)

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
  const isTemplate = wf.origin === 'official_template'
  const t = wf.template || {}
  const paidApi = (isTemplate && t.open_source === false) || wf.runs_on === 'api'
  /* One click from "what this does" to every other workflow that does it. */
  const showTask = (key) => {
    dispatch({ type: 'set-filter', tab: 'workflows', field: 'task', value: [key] })
    dispatch({ type: 'set-tab', tab: 'workflows' })
  }
  const outputsTotal = wf.counts_outputs || (wf.outputs_recent || []).length

  /* Every output carrying this graph's id - for a template that includes the
     renders of saved copies of it. */
  const showAllOutputs = () => {
    dispatch({ type: 'clear-filters', tab: 'outputs' })
    dispatch({ type: 'set-filter', tab: 'outputs', field: 'from_workflow_graph', value: wf.id })
    dispatch({ type: 'set-tab', tab: 'outputs' })
  }

  return (
    <>
      <div className={'gp-details__header' + (derived ? ' gp-details__header--inferred' : '')}>
        <div className="gp-details__eyebrow">
          {isTemplate
            ? 'ComfyUI template' + (t.category ? ' · ' + t.category : '')
            : (wf.folder || 'workflow root')}
        </div>
        <h2 className="gp-details__title">{isTemplate ? (wf.title || wf.name) : wf.name}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          {isTemplate ? <Badge tone="brand" title="Official ComfyUI template - read-only">template</Badge> : null}
          {paidApi ? <Badge tone="info" title="Runs on a partner API, not on local models">API</Badge> : null}
          {wf.base_model && wf.base_model !== 'Unknown'
            ? <Badge tone="base">{wf.base_model}</Badge> : null}
          {wf.modality ? <Badge tone="media">{wf.modality}</Badge> : null}
          <Badge tone="mono">{fmtCount(counts.nodes)} nodes</Badge>
          {missing
            ? <Badge tone={isTemplate ? 'warn' : 'dep-missing'}>
              {isTemplate && !counts.missing_nodes
                ? 'needs ' + counts.missing_models + ' model' + (counts.missing_models === 1 ? '' : 's')
                : missing + ' missing'}
            </Badge>
            : <Badge tone="dep-satisfied">{isTemplate ? 'ready to run' : 'runnable'}</Badge>}
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
          {counts.missing_nodes ? <Button size="sm" icon={PackageSearch}
            label="Resolve missing nodes" title="Review safe package sources for this workflow"
            onClick={() => setResolveNodes(true)} /> : null}
        </div>

        {isTemplate ? (
          <div className="gp-callout gp-callout--info gp-u-mb-6">
            <span className="gp-callout__icon"><Lock aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">
                {missing ? 'Almost ready: this template needs a few files' : 'Official ComfyUI template'}
              </div>
              {missing
                ? (counts.missing_models || 0) + ' model file(s)' +
                  (counts.missing_nodes ? ' and ' + counts.missing_nodes + ' node class(es)' : '') +
                  ' it uses are not in your library yet. Each one below links to where ComfyUI ' +
                  'downloads it and names the models/ folder it belongs in. '
                : paidApi
                  ? 'Runs on a partner API rather than on local models, so there is nothing to download. '
                  : 'Every model and node it uses is already in your library. '}
              It ships with ComfyUI, so the vault only reads it: open it in ComfyUI and save a
              copy to make one of your own.
            </div>
          </div>
        ) : missing ? (
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

        {wf.based_on_template ? (
          <div className="gp-u-row gp-u-gap-3 gp-u-mb-6 gp-u-fs-11">
            <LayoutTemplate size={12} aria-hidden="true" />
            <span className="gp-u-muted">Based on the ComfyUI template</span>
            <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm"
              onClick={() => onOpenUid(wf.based_on_template.uid)}>
              <span className="gp-btn__label">{wf.based_on_template.title}</span>
            </button>
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

        {isTemplate ? (
          <Section title="Template">
            {t.tags && t.tags.length ? (
              <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mb-4">
                {t.tags.map((tag) => <Badge key={tag} tone="neutral">{tag}</Badge>)}
              </div>
            ) : null}
            <div className="gp-meta">
              <MetaRow label="category" value={[t.category_group, t.category].filter(Boolean).join(' · ')} />
              <MetaRow label="runs on" value={paidApi ? 'partner API (no local models)' : 'your models'} />
              {t.model_families && t.model_families.length
                ? <MetaRow label="models" value={t.model_families.join(', ')} /> : null}
              {t.required_size ? <MetaRow label="model size" value={bytes(t.required_size)} num /> : null}
              {t.min_comfyui ? <MetaRow label="needs ComfyUI" value={t.min_comfyui + ' or newer'} /> : null}
              {t.released_on ? <MetaRow label="published" value={t.released_on} /> : null}
              {t.package_version ? <MetaRow label="package" value={'templates ' + t.package_version} /> : null}
              <MetaRow label="name" value={t.name || wf.name} />
            </div>
            {isHttps(t.tutorial_url) ? (
              <a className="gp-btn gp-btn--sm gp-btn--ghost gp-u-mt-4" href={t.tutorial_url}
                target="_blank" rel="noreferrer noopener">
                <BookOpen className="gp-btn__icon" aria-hidden="true" />
                <span className="gp-btn__label">Tutorial</span>
              </a>
            ) : null}
          </Section>
        ) : null}

        {isTemplate && wf.copies && wf.copies.length ? (
          <Section title={'Your copies (' + wf.copies.length + ')'}>
            <div className="gp-list">
              {wf.copies.map((c) => (
                <button key={c.uid} type="button" className="gp-row gp-focus-inset"
                  onClick={() => onOpenUid(c.uid)} title={c.rel_path}>
                  <span className="gp-row__name">{c.name}</span>
                  <span className="gp-row__cell gp-row__cell--grow gp-u-meta">{c.rel_path}</span>
                </button>
              ))}
            </div>
          </Section>
        ) : null}

        {wf.tasks && wf.tasks.length ? (
          <Section title="What it does">
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap">
              {wf.tasks.map((key, i) => (
                <Chip key={key} small selected={i === 0}
                  label={TASK_LABELS[key] || key}
                  title={(i === 0 ? 'Main job. ' : '') + 'Show every workflow that does this'}
                  onClick={() => showTask(key)} />
              ))}
              <Badge tone={wf.runs_on === 'api' ? 'info' : 'neutral'}
                title={wf.runs_on === 'api'
                  ? 'A paid partner-API node does the work on someone else\'s servers'
                  : 'Runs on the models on this machine'}
              >
                {RUNS_ON_LABELS[wf.runs_on] || 'Local models'}
              </Badge>
            </div>
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
          <DependencyList deps={deps.data} onOpenUid={onOpenUid}
            subject={isTemplate ? 'template' : 'workflow'} />
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
          <Section title={'Outputs from this ' + (isTemplate ? 'template' : 'workflow') +
            ' (' + fmtCount(outputsTotal) + ')'}>
            <Button size="sm" variant="ghost" icon={ImageIcon} className="gp-u-mb-4"
              label="Show them all in Outputs" onClick={showAllOutputs} />
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
          <Section title={'Outputs from this ' + (isTemplate ? 'template' : 'workflow')}>
            <EmptyState small icon={ImageIcon} title="No outputs traced back here"
              text={'No indexed output carries this ' + (isTemplate ? 'template' : 'workflow') +
                '\'s graph id. ComfyUI embeds it in every PNG it saves; a scan picks new ones up.'} />
          </Section>
        )}

        <Section title="File">
          <div className="gp-meta">
            <MetaRow label="origin" value={wf.origin_label || wf.origin} />
            <MetaRow label="format" value={wf.format} />
            <MetaRow label="source" value={wf.source} />
            <MetaRow label="nodes" value={fmtCount(counts.nodes)} num />
            <MetaRow label="links" value={fmtCount(counts.links)} num />
            <MetaRow label="groups" value={fmtCount(counts.groups)} num />
            <MetaRow label="subgraphs"
              value={wf.subgraph_count ? fmtCount(wf.subgraph_count)
                : wf.has_subgraphs ? 'yes' : 'no'}
              num={!!wf.subgraph_count} />
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
      {resolveNodes ? <ResolveNodesDialog workflowId={id} workflowName={wf.name}
        onClose={() => setResolveNodes(false)} /> : null}
    </>
  )
}
