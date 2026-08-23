import React, { useCallback, useState } from 'react'
import {
  AlertTriangle, FolderOpen, Download, Maximize2, Star, Copy, Check, FileJson
} from 'lucide-react'
import api, { thumbnailUrl, downloadUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, ProvenanceRow, DetailsFallback } from './MetaRow.jsx'
import {
  bytes, dateTime, dimensions, duration, count as fmtCount
} from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * OutputDetails - the full generation record behind one output file.
 *
 * Every generation field is rendered through its provenance entry, so a value
 * the backend could not resolve shows as an em dash with the reason in its
 * title rather than a raw link tuple.
 */
export default function OutputDetails({ id, onOpenUid, onLightbox }) {
  const { state, toast, toastError, invalidate } = useVault()
  const epoch = state.dataEpoch
  const [copied, setCopied] = useState(false)

  const detail = useResource('output:' + id, (s) => api.output(id, s), { epoch })
  const out = detail.data

  const onFavorite = useCallback(async () => {
    if (!out) return
    try {
      await api.patchOutput(id, { favorite: !out.favorite })
      detail.refresh()
      invalidate()
    } catch (err) {
      toastError(err, 'Could not update the output')
    }
  }, [out, id, detail, invalidate, toastError])

  const onReveal = useCallback(async () => {
    try {
      await api.reveal('output:' + id)
    } catch (err) {
      toastError(err, 'Could not open the folder')
    }
  }, [id, toastError])

  const copyPrompt = useCallback(async () => {
    if (!out || !out.positive_prompt || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(out.positive_prompt)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch (err) {
      toast({ tone: 'warn', title: 'Clipboard unavailable' })
    }
  }, [out, toast])

  const extract = useCallback(async () => {
    if (!out) return
    try {
      const res = await api.extractWorkflow(id, {
        root_id: out.root_id,
        folder: 'extracted',
        name: out.filename.replace(/\.[^.]+$/, '')
      })
      toast({
        tone: 'ok',
        title: 'Workflow extracted',
        message: res.abs_path,
        action: { label: 'Open', run: () => onOpenUid(res.uid) }
      })
      invalidate()
    } catch (err) {
      toastError(err, 'Could not extract the workflow')
    }
  }, [out, id, toast, toastError, invalidate, onOpenUid])

  if (detail.loading && !out) {
    return (
      <DetailsFallback eyebrow="Output" title="Loading output">
        <SkeletonMeta rows={9} />
      </DetailsFallback>
    )
  }
  if (detail.error) {
    return (
      <DetailsFallback eyebrow="Output" title="Unavailable">
        <EmptyState tone="error" small icon={AlertTriangle}
          title="Could not load this output" text={detail.error.message}
          actions={<Button onClick={detail.refresh}>Retry</Button>} />
      </DetailsFallback>
    )
  }
  if (!out) return null

  const prov = out.provenance || {}
  const loras = out.loras || []
  const allModels = out.all_models || []

  return (
    <>
      <div className="gp-details__header">
        <div className="gp-details__eyebrow">{out.folder || 'output root'}</div>
        <h2 className="gp-details__title" title={out.filename}>{out.filename}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          <Badge tone="media">{out.media_kind}</Badge>
          {dimensions(out.width, out.height)
            ? <Badge tone="mono">{dimensions(out.width, out.height)}</Badge> : null}
          {out.duration_ms ? <Badge tone="mono">{duration(out.duration_ms)}</Badge> : null}
          <Badge tone="mono">{bytes(out.size)}</Badge>
          {out.has_metadata
            ? <Badge tone="ok">{out.metadata_format}</Badge>
            : <Badge tone="neutral">no metadata</Badge>}
        </div>
      </div>

      <div className="gp-details__body">
        <button type="button" className="gp-details__hero"
          onClick={() => onLightbox(out.uid)}
          aria-label="Open a full size preview" title="Open a full size preview">
          <img src={thumbnailUrl(out.uid, 640)} width="640" height="400"
            loading="lazy" decoding="async" alt="" />
        </button>

        <div className="gp-u-row gp-u-gap-3 gp-u-mb-6 gp-u-wrap">
          <Button size="sm" icon={Maximize2} label="Full size"
            onClick={() => onLightbox(out.uid)} />
          <Button size="sm" icon={Star} label={out.favorite ? 'Favourited' : 'Favourite'}
            aria-pressed={out.favorite} onClick={onFavorite} />
          <a className="gp-btn gp-btn--sm" href={downloadUrl(out.uid)}>
            <Download className="gp-btn__icon" aria-hidden="true" />
            <span className="gp-btn__label">Download</span>
          </a>
          <Button size="sm" variant="ghost" icon={FolderOpen} label="Reveal"
            onClick={onReveal} />
        </div>

        <Section
          title="Prompt"
          aside={out.positive_prompt ? (
            <Button size="sm" variant="ghost" icon={copied ? Check : Copy}
              label={copied ? 'Copied' : 'Copy'} onClick={copyPrompt} />
          ) : null}
        >
          {out.positive_prompt ? (
            <div className="gp-prompt">{out.positive_prompt}</div>
          ) : (
            <div className="gp-prompt gp-prompt--empty"
              title={prov.positive_prompt && prov.positive_prompt.reason
                ? 'Unresolved: ' + prov.positive_prompt.reason
                : 'No prompt recorded in this file'}
            >
              No positive prompt recorded.
            </div>
          )}
          {out.negative_prompt ? (
            <div className="gp-prompt gp-prompt--negative gp-u-mt-4">{out.negative_prompt}</div>
          ) : null}
        </Section>

        <Section title="Generation">
          <div className="gp-meta">
            <ProvenanceRow label="seed" value={out.seed} provenance={prov.seed} num />
            <ProvenanceRow label="steps" value={out.steps} provenance={prov.steps} num />
            <ProvenanceRow label="cfg" value={out.cfg} provenance={prov.cfg} num />
            <ProvenanceRow label="denoise" value={out.denoise} provenance={prov.denoise} num />
            <ProvenanceRow label="sampler" value={out.sampler} provenance={prov.sampler} />
            <ProvenanceRow label="scheduler" value={out.scheduler} provenance={prov.scheduler} />
            <MetaRow label="nodes in graph" value={fmtCount(out.node_count)} num />
            <MetaRow label="unresolved" value={out.unresolved_inputs} num
              tone={out.unresolved_inputs ? 'danger' : undefined} />
          </div>
        </Section>

        {allModels.length ? (
          <Section title="Models used">
            <div className="gp-list">
              {allModels.map((m, i) => (
                <button key={(m.uid || m.name) + ':' + i} type="button"
                  className="gp-row gp-focus-inset"
                  disabled={!m.uid}
                  onClick={() => m.uid && onOpenUid(m.uid)}
                  title={m.name}>
                  <span className="gp-row__name">{m.name}</span>
                  <span className="gp-row__cell">
                    <Badge tone="role">{m.role}</Badge>
                  </span>
                </button>
              ))}
            </div>
          </Section>
        ) : null}

        {loras.length ? (
          <Section title="LoRAs">
            <table className="gp-table gp-table--compact">
              <thead>
                <tr><th>Adapter</th><th className="gp-table__num">Strength</th></tr>
              </thead>
              <tbody>
                {loras.map((l, i) => (
                  <tr key={(l.name || 'lora') + ':' + i}>
                    <td>{l.name}</td>
                    <td className="gp-table__num">{l.strength}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        ) : null}

        <Section title="Source">
          <div className="gp-meta">
            <MetaRow label="model" value={out.model_name} />
            <MetaRow label="workflow hash" value={out.workflow_hash} wrap />
            <MetaRow label="metadata" value={out.metadata_format} />
          </div>
          <div className="gp-u-row gp-u-gap-3 gp-u-mt-4 gp-u-wrap">
            {out.model_uid ? (
              <Button size="sm" label="Open model" onClick={() => onOpenUid(out.model_uid)} />
            ) : null}
            {out.workflow_uid ? (
              <Button size="sm" label="Open workflow" onClick={() => onOpenUid(out.workflow_uid)} />
            ) : null}
            {out.graph_available ? (
              <Button size="sm" variant="ghost" icon={FileJson} label="Save as workflow"
                title="Write the embedded graph out as a real workflow file"
                onClick={extract} />
            ) : null}
          </div>
        </Section>

        {out.siblings && out.siblings.length ? (
          <Section title={'From the same graph (' + out.siblings.length + ')'}>
            <div className="gp-grid">
              {out.siblings.slice(0, 8).map((s) => (
                <button key={s.uid} type="button" className="gp-card"
                  onClick={() => onOpenUid(s.uid)} title={s.filename}>
                  <span className="gp-card__thumb">
                    <img className="gp-card__media"
                      src={s.thumbnail_url || thumbnailUrl(s.uid, 160)}
                      width={160} height={160} loading="lazy" decoding="async" alt="" />
                  </span>
                </button>
              ))}
            </div>
          </Section>
        ) : null}

        <Section title="File">
          <div className="gp-meta">
            <MetaRow label="type" value={out.mime} />
            <MetaRow label="dimensions" value={dimensions(out.width, out.height)} num />
            <MetaRow label="duration" value={duration(out.duration_ms)} num />
            <MetaRow label="colour" value={out.color_mode} />
            <MetaRow label="frames" value={out.frame_count} num />
            <MetaRow label="size" value={bytes(out.size)} num />
            <MetaRow label="created" value={dateTime(out.created_at)} />
            <MetaRow label="modified" value={dateTime(out.modified_at)} />
            <MetaRow label="path" value={out.abs_path || out.rel_path} wrap />
          </div>
        </Section>
      </div>
    </>
  )
}
