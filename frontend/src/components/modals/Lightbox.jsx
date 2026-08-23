import React, { useEffect, useState, useCallback, useRef } from 'react'
import {
  X, ChevronLeft, ChevronRight, Download, Copy, Check, Box, FolderOpen
} from 'lucide-react'
import api, { rawUrl, downloadUrl, thumbnailUrl } from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { parseUid, bytes, dateTime, dimensions, duration } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'
const Model3DViewer = React.lazy(() => import('../common/Model3DViewer.jsx'))

/*
 * Lightbox - the full size preview.
 *
 * Media streams from /files/raw, which implements HTTP Range, so a 400 MB MP4
 * seeks properly in a <video> element. Grid thumbnails are never blown up here.
 */

function Stage({ uid, kind, media, name, ext, size }) {
  if (kind !== 'output') {
    return (
      <img className="gp-lightbox__media" src={thumbnailUrl(uid, 640)} alt={name}
        decoding="async" />
    )
  }
  if (media === 'video') {
    return (
      <video className="gp-lightbox__media" src={rawUrl(uid)} controls autoPlay={false}
        preload="metadata" />
    )
  }
  if (media === 'model3d') {
    return (
      <React.Suspense fallback={<div className="gp-3d gp-3d--msg"><p>Loading 3D…</p></div>}>
        <Model3DViewer uid={uid} ext={ext} sizeBytes={size} className="gp-3d--full" />
      </React.Suspense>
    )
  }
  if (media === 'audio') {
    return <audio className="gp-lightbox__media" src={rawUrl(uid)} controls preload="metadata" />
  }
  if (media === 'image') {
    return <img className="gp-lightbox__media" src={rawUrl(uid)} alt={name} decoding="async" />
  }
  return (
    <div className="gp-empty">
      <span className="gp-empty__icon"><Box aria-hidden="true" /></span>
      <h3 className="gp-empty__title">No inline preview</h3>
      <p className="gp-empty__text">
        This file type cannot be shown in the browser. Download it to open it in the
        application that made it.
      </p>
      <div className="gp-empty__actions">
        <a className="gp-btn gp-btn--primary" href={downloadUrl(uid)}>
          <Download className="gp-btn__icon" aria-hidden="true" />
          <span className="gp-btn__label">Download</span>
        </a>
      </div>
    </div>
  )
}

export default function Lightbox({ uid, items, scope, onClose, onNavigate, onOpenUid }) {
  const { state, toast, toastError } = useVault()
  const { kind, id } = parseUid(uid)
  const [copied, setCopied] = useState(false)
  const closeRef = useRef(null)

  const index = items ? items.findIndex((i) => i.uid === uid) : -1
  const hasPrev = index > 0
  const hasNext = index >= 0 && index < items.length - 1

  const detail = useResource(
    kind === 'output' ? 'lightbox-output:' + id : null,
    (s) => api.output(id, s),
    { epoch: state.dataEpoch }
  )
  const listItem = index >= 0 ? items[index] : null
  const record = detail.data || listItem

  const go = useCallback((delta) => {
    if (!items || index < 0) return
    const next = items[index + delta]
    if (next) onNavigate(next.uid)
  }, [items, index, onNavigate])

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose() }
      else if (event.key === 'ArrowLeft') { event.preventDefault(); go(-1) }
      else if (event.key === 'ArrowRight') { event.preventDefault(); go(1) }
    }
    window.addEventListener('keydown', onKey, true)
    if (closeRef.current) closeRef.current.focus()
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose, go])

  const copyPrompt = useCallback(async () => {
    const text = record && record.positive_prompt
    if (!text || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch (err) {
      toast({ tone: 'warn', title: 'Clipboard unavailable' })
    }
  }, [record, toast])

  const reveal = useCallback(async () => {
    try {
      await api.reveal(uid)
    } catch (err) {
      toastError(err, 'Could not open the folder')
    }
  }, [uid, toastError])

  const name = (record && (record.filename || record.name)) || uid

  return (
    <div className="gp-lightbox" role="dialog" aria-modal="true" aria-label={'Preview ' + name}>
      <div className="gp-lightbox__bar">
        <button ref={closeRef} type="button" className="gp-btn gp-btn--ghost gp-btn--icon"
          aria-label="Close preview" onClick={onClose}>
          <X className="gp-btn__icon" aria-hidden="true" />
        </button>
        <span className="gp-lightbox__name" title={name}>{name}</span>
        {index >= 0 ? (
          <span className="gp-lightbox__pos">{index + 1} / {items.length}</span>
        ) : null}
        <a className="gp-btn gp-btn--sm" href={downloadUrl(uid)}>
          <Download className="gp-btn__icon" aria-hidden="true" />
          <span className="gp-btn__label">Download</span>
        </a>
        <Button size="sm" variant="ghost" icon={FolderOpen} label="Reveal" onClick={reveal} />
      </div>

      <div className="gp-lightbox__stage">
        <Stage uid={uid} kind={kind} media={record && record.media_kind} name={name}
          ext={record && record.ext} size={record && record.size} />
        <button type="button" className="gp-lightbox__nav gp-lightbox__nav--prev"
          aria-label="Previous asset" disabled={!hasPrev} onClick={() => go(-1)}>
          <ChevronLeft aria-hidden="true" />
        </button>
        <button type="button" className="gp-lightbox__nav gp-lightbox__nav--next"
          aria-label="Next asset" disabled={!hasNext} onClick={() => go(1)}>
          <ChevronRight aria-hidden="true" />
        </button>
      </div>

      <aside className="gp-lightbox__side">
        {record ? (
          <>
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mb-5">
              {record.media_kind ? <Badge tone="media">{record.media_kind}</Badge> : null}
              {dimensions(record.width, record.height)
                ? <Badge tone="mono">{dimensions(record.width, record.height)}</Badge> : null}
              {record.duration_ms
                ? <Badge tone="mono">{duration(record.duration_ms)}</Badge> : null}
              <Badge tone="mono">{bytes(record.size)}</Badge>
            </div>

            {record.positive_prompt !== undefined ? (
              <>
                <div className="gp-details__section-head">
                  <span>Prompt</span>
                  {record.positive_prompt ? (
                    <Button size="sm" variant="ghost" icon={copied ? Check : Copy}
                      label={copied ? 'Copied' : 'Copy'} onClick={copyPrompt} />
                  ) : null}
                </div>
                <div className={'gp-prompt' + (record.positive_prompt ? '' : ' gp-prompt--empty')}>
                  {record.positive_prompt || 'No prompt recorded in this file.'}
                </div>
                {record.negative_prompt ? (
                  <div className="gp-prompt gp-prompt--negative gp-u-mt-4">
                    {record.negative_prompt}
                  </div>
                ) : null}
              </>
            ) : null}

            <div className="gp-details__section-head gp-u-mt-6"><span>Details</span></div>
            <div className="gp-meta">
              <MetaRow label="seed" value={record.seed} num />
              <MetaRow label="steps" value={record.steps} num />
              <MetaRow label="cfg" value={record.cfg} num />
              <MetaRow label="sampler" value={record.sampler} />
              <MetaRow label="scheduler" value={record.scheduler} />
              <MetaRow label="model" value={record.model_name} wrap />
              <MetaRow label="created" value={dateTime(record.created_at)} />
              <MetaRow label="folder" value={record.folder || record.category} />
            </div>

            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-5">
              {record.model_uid ? (
                <Button size="sm" label="Open model"
                  onClick={() => { onClose(); onOpenUid(record.model_uid) }} />
              ) : null}
              {record.workflow_uid ? (
                <Button size="sm" label="Open workflow"
                  onClick={() => { onClose(); onOpenUid(record.workflow_uid) }} />
              ) : null}
            </div>
          </>
        ) : (
          <p className="gp-u-fs-11 gp-u-meta">Loading record...</p>
        )}
      </aside>
    </div>
  )
}
