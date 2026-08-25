import React, { useCallback } from 'react'
import {
  Download, ExternalLink, FolderOpen, Star, RefreshCw, Info, AlertTriangle, Maximize2
} from 'lucide-react'
import api, { thumbnailUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge, { IntegrityBadge, BaseModelBadge, ConfidenceBadge } from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, DetailsFallback } from './MetaRow.jsx'
import { CommunityStars } from '../grid/AssetCard.jsx'
import ComponentBreakdown from './ComponentBreakdown.jsx'
import Annotations from './Annotations.jsx'
import HashStatus from './HashStatus.jsx'
import UsageList from './UsageList.jsx'
import {
  bytes, params, dateTime, humanise, count as fmtCount
} from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * ModelDetails - the deep record for one model file.
 *
 * Order follows the question the owner asks in front of a card:
 *   what is it, what is inside it, is it verified, is there a newer one,
 *   how do I use it, where did it come from, and what already uses it.
 */
/** `{format, rank, alpha}` as something a person reads: "peft · rank 32". */
function formatAdapter(adapter) {
  if (!adapter || typeof adapter !== 'object') return adapter
  const parts = []
  if (adapter.format) parts.push(String(adapter.format))
  if (adapter.rank !== null && adapter.rank !== undefined) parts.push('rank ' + adapter.rank)
  if (adapter.alpha !== null && adapter.alpha !== undefined) parts.push('alpha ' + adapter.alpha)
  return parts.length ? parts.join(' · ') : null
}


export default function ModelDetails({ id, onOpenUid, onLightbox }) {
  const { state, toast, toastError, invalidate } = useVault()
  const epoch = state.dataEpoch

  const detail = useResource('model:' + id, (s) => api.model(id, s), { epoch })
  const usage = useResource('model-usage:' + id, (s) => api.modelUsage(id, { limit: 12 }, s), { epoch })

  const model = detail.data

  const onFavorite = useCallback(async () => {
    if (!model) return
    try {
      await api.patchModel(id, { favorite: !model.favorite })
      detail.refresh()
      invalidate()
    } catch (err) {
      toastError(err, 'Could not update the model')
    }
  }, [model, id, detail, invalidate, toastError])

  const onAnnotate = useCallback(async (fields) => {
    try {
      await api.patchModel(id, fields)
      detail.refresh()
      invalidate()
    } catch (err) {
      toastError(err, 'Could not save')
    }
  }, [id, detail, invalidate, toastError])

  const onRefreshMetadata = useCallback(async () => {
    try {
      await api.modelRefreshMetadata(id, false)
      toast({ tone: 'ok', title: 'Metadata refresh queued', message: 'Civitai lookup will run in the background.' })
    } catch (err) {
      toastError(err, 'Metadata refresh unavailable')
    }
  }, [id, toast, toastError])

  const onReveal = useCallback(async () => {
    try {
      await api.reveal('model:' + id)
    } catch (err) {
      toastError(err, 'Could not open the folder')
    }
  }, [id, toastError])

  if (detail.loading && !model) {
    return (
      <DetailsFallback eyebrow="Model" title="Loading model">
        <SkeletonMeta rows={10} />
      </DetailsFallback>
    )
  }
  if (detail.error) {
    return (
      <DetailsFallback eyebrow="Model" title="Unavailable">
        <EmptyState
          tone="error"
          small
          icon={AlertTriangle}
          title="Could not load this model"
          text={detail.error.message}
          actions={<Button onClick={detail.refresh}>Retry</Button>}
        />
      </DetailsFallback>
    )
  }
  if (!model) return null

  const tech = model.technical || {}
  const detection = tech.detection || {}
  const build = model.build_spec || {}
  const update = model.update || {}
  const integrity = model.integrity || {}
  const civitai = model.civitai || {}
  const actions = model.actions || {}
  const description = model.description || {}
  const inferredDetection = detection.source === 'inferred' ||
    (detection.confidence !== undefined && detection.confidence !== null && detection.confidence < 0.7)
  const descriptionInferred = description.source === 'ollama' || description.source === 'derived'

  return (
    <>
      <div className={'gp-details__header' + (inferredDetection ? ' gp-details__header--inferred' : '')}>
        <div className="gp-details__eyebrow">{humanise(model.category)} / {model.role}</div>
        <h2 className="gp-details__title" title={model.filename}>{model.filename}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          <BaseModelBadge base={model.base_model} />
          {model.precision ? <Badge tone="precision">{model.precision}</Badge> : null}
          {model.params && model.params.display
            ? <Badge tone="mono">{model.params.display}</Badge> : null}
          <ConfidenceBadge
            confidence={detection.source === 'metadata' ? 'declared' : 'inferred'}
            title={'Detection source: ' + (detection.source || 'unknown') +
              (detection.confidence ? ', confidence ' + detection.confidence.toFixed(2) : '')}
          />
          <IntegrityBadge status={integrity.status} note={integrity.note} />
        </div>
      </div>

      <div className="gp-details__body">
        <button
          type="button"
          className="gp-details__hero"
          onClick={() => onLightbox(model.uid)}
          aria-label="Open a full size preview"
          title="Open a full size preview"
        >
          <img src={thumbnailUrl(model.uid, 640)} width="640" height="400"
            loading="lazy" decoding="async" alt="" />
        </button>

        <div className="gp-u-row gp-u-gap-3 gp-u-mb-6 gp-u-wrap">
          <Button size="sm" icon={Maximize2} label="Preview"
            onClick={() => onLightbox(model.uid)} />
          <Button size="sm" icon={Star} label={model.favorite ? 'Favourited' : 'Favourite'}
            aria-pressed={model.favorite} onClick={onFavorite} />
          <Button size="sm" variant="ghost" icon={FolderOpen} label="Reveal"
            title="Show this file in your file manager" onClick={onReveal} />
        </div>

        {integrity.status && integrity.status !== 'ok' ? (
          <div className="gp-callout gp-callout--danger gp-u-mb-6">
            <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">{humanise(integrity.status)}</div>
              {integrity.note || 'This file did not pass the header check. It is indexed, but ComfyUI is unlikely to load it.'}
            </div>
          </div>
        ) : null}

        <Section title="Your library">
          <Annotations item={model} onPatch={onAnnotate} />
        </Section>

        <Section title="Technical">
          <div className="gp-meta">
            <MetaRow label="architecture" value={model.architecture} />
            <MetaRow
              label="base model"
              value={model.base_model && model.base_model.family}
              inferred={model.base_model && model.base_model.confidence < 0.7}
              inferredTitle={'Detected from ' + (model.base_model && model.base_model.source) +
                ', confidence ' + (model.base_model && Number(model.base_model.confidence).toFixed(2))}
            />
            <MetaRow label="variant" value={model.base_model && model.base_model.variant} />
            <MetaRow label="modality" value={model.modality} />
            <MetaRow label="precision" value={model.precision} />
            <MetaRow label="quantisation" value={model.quantization} />
            <MetaRow label="parameters" value={params(tech_primary(model))} num />
            <MetaRow label="total parameters" value={params(model.params && model.params.total)} num />
            <MetaRow label="tensors" value={fmtCount(tech.tensor_count)} num />
            <MetaRow label="container" value={tech.format} />
            <MetaRow label="prediction" value={tech.prediction_type} />
            <MetaRow label="resolution hint" value={tech.resolution_hint}
              inferred inferredTitle="Derived from the architecture, not declared in the file" />
            <MetaRow label="bundled" value={model.is_bundled ? 'yes' : 'no'} />
            <MetaRow label="adapter" value={model.is_adapter ? 'yes' : 'no'} />
          </div>
        </Section>

        {tech.components && tech.components.length ? (
          <Section title="What is inside">
            <ComponentBreakdown components={tech.components} />
          </Section>
        ) : null}

        {detection.signals && detection.signals.length ? (
          <Section title="Detection signals">
            <ul className="gp-confirm__list">
              {detection.signals.map((sig, i) => <li key={sig + ':' + i}>{sig}</li>)}
            </ul>
            <div className="gp-provenance gp-u-mt-4">
              {detection.source === 'metadata'
                ? <span className="gp-provenance--declared">read from the file header</span>
                : <span className="gp-provenance--inferred">inferred from tensor shapes</span>}
              {detection.confidence !== null && detection.confidence !== undefined
                ? ' / confidence ' + Number(detection.confidence).toFixed(2)
                : null}
            </div>
          </Section>
        ) : null}

        <Section title="Verification">
          <HashStatus model={model} onDone={() => { detail.refresh() }} />
        </Section>

        <Section title="Newer version">
          {update.has_update ? (
            <div className="gp-callout gp-callout--info">
              <span className="gp-callout__icon"><Download aria-hidden="true" /></span>
              <div className="gp-callout__body">
                <div className="gp-callout__title">
                  {update.latest_version_name || 'A newer version exists'}
                </div>
                {update.benefits || 'The publisher lists a newer version of this file.'}
              </div>
            </div>
          ) : (
            <div className="gp-meta">
              <MetaRow label="update" value={update.checked_at ? 'up to date' : null}
                empty={civitai.reason === 'not_hashed'
                  ? 'Unknown - compute the hash first, then the publisher can be matched.'
                  : 'Never checked.'} />
              <MetaRow label="checked" value={dateTime(update.checked_at)} />
            </div>
          )}
          {actions.can_refresh_metadata ? (
            <div className="gp-u-mt-4">
              <Button size="sm" icon={RefreshCw} label="Check for updates"
                onClick={onRefreshMetadata} />
            </div>
          ) : actions.refresh_blocked_reason ? (
            <div className="gp-u-fs-11 gp-u-meta gp-u-mt-4">
              Blocked: {humanise(actions.refresh_blocked_reason)}
            </div>
          ) : null}
        </Section>

        <Section title="Description">
          {description.text ? (
            <p className={'gp-u-fs-12 ' + (descriptionInferred ? 'gp-u-ai' : 'gp-u-muted')}
              title={descriptionInferred
                ? 'Generated locally from the file metadata, not written by the publisher'
                : undefined}
            >
              {descriptionInferred
                ? <span className="gp-inferred" title="Generated locally">{description.text}</span>
                : description.text}
            </p>
          ) : (
            <p className="gp-u-fs-11 gp-u-meta">
              No description on file. Publisher text arrives with the metadata refresh once the
              file has been hashed.
            </p>
          )}
        </Section>

        <Section title="How to use it">
          <div className="gp-meta">
            <MetaRow label="usage notes" value={model.usage_notes} wrap
              empty="Not recorded for this file." />
            <MetaRow label="recommended" value={model.recommended_settings} wrap
              empty="No recommended settings on file." />
            <MetaRow label="license" value={build.license} />
          </div>
          {model.trigger_words && model.trigger_words.length ? (
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
              {model.trigger_words.map((w) => (
                <Badge key={w} tone="mono">{w}</Badge>
              ))}
            </div>
          ) : null}
        </Section>

        <Section title="Build spec">
          <div className="gp-meta">
            <MetaRow label="trained by" value={build.trained_by} />
            <MetaRow label="training steps" value={build.training_steps} num />
            <MetaRow label="dataset" value={build.dataset_notes} wrap />
            <MetaRow label="adapter" value={formatAdapter(build.adapter)} />
          </div>
        </Section>

        <Section title="Where it came from">
          <div className="gp-meta">
            <MetaRow label="source" value={model.download && model.download.source} />
            <MetaRow label="civitai" value={civitai.state}
              empty={civitai.hint || 'Not matched.'} />
            <MetaRow label="community" value={model.community && model.community.rating
              ? <CommunityStars rating={model.community.rating}
                  downloads={model.community.downloads} />
              : null} empty="No community stats yet." />
          </div>
          {model.download && model.download.url ? (
            <a className="gp-btn gp-btn--sm gp-u-mt-4" href={model.download.url}
              target="_blank" rel="noreferrer noopener"
            >
              <ExternalLink className="gp-btn__icon" aria-hidden="true" />
              <span className="gp-btn__label">Download page</span>
            </a>
          ) : null}
          {civitai.url ? (
            <a className="gp-btn gp-btn--sm gp-u-mt-4" href={civitai.url}
              target="_blank" rel="noreferrer noopener"
            >
              <ExternalLink className="gp-btn__icon" aria-hidden="true" />
              <span className="gp-btn__label">View on Civitai</span>
            </a>
          ) : null}
          {!model.download?.url && !civitai.url ? (
            <div className="gp-callout gp-callout--info gp-u-mt-4">
              <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
              <div className="gp-callout__body">
                No download source is known for this file yet. Computing its hash lets the
                publisher be identified.
              </div>
            </div>
          ) : null}
        </Section>

        <Section
          title="Used in"
          aside={<span className="gp-u-fs-10 gp-u-meta">
            {fmtCount((model.usage && model.usage.workflow_count) || 0)} workflows /
            {' '}{fmtCount((model.usage && model.usage.output_count) || 0)} outputs
          </span>}
        >
          <UsageList usage={usage.data} onOpenUid={onOpenUid} compact />
        </Section>

        <Section title="File">
          <div className="gp-meta">
            <MetaRow label="size" value={bytes(model.size)} num />
            <MetaRow label="modified" value={dateTime(model.modified_at)} />
            <MetaRow label="folder" value={model.category} />
            <MetaRow label="path" value={model.abs_path} wrap />
            <MetaRow label="root" value={'#' + model.root_id} />
          </div>
        </Section>
      </div>
    </>
  )
}

function tech_primary(model) {
  return model.params && model.params.primary ? model.params.primary : null
}
