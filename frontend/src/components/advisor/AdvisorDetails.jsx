import React, { useCallback, useState } from 'react'
import {
  X, ExternalLink, Download, Link2, MousePointerClick, FolderOpen, Check,
  AlertTriangle, ShieldCheck, Copy, HardDrive
} from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import { useVault } from '../../state/VaultContext.jsx'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import ErrorBoundary from '../common/ErrorBoundary.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import MetaRow, { Section } from '../details/MetaRow.jsx'
import { bytes as fmtBytes, count as fmtCount } from '../../services/format.js'
import { ScoreRing, BudgetBar } from './charts.jsx'
import {
  Est, FitBadge, KindIcon, gb, paramsText, isoDate, SOURCE_LABEL, VERDICT_HELP
} from './parts.jsx'
import '../../styles/advisor.css'

/*
 * AdvisorDetails - the full record for one model, in the shell's details slot.
 *
 * `target` is a catalog id ("hf:owner/name") or "installed:<model id>" for a
 * file already in the vault. Both render the same four questions in the same
 * order: does it fit, is it safe, which file, and what else does it need.
 *
 * Published facts (sizes, licence, dates, counts) are plain. Everything the
 * advisor worked out - the requirement, the verdict, the score - is violet.
 */

function budgetFor(budget, kind) {
  if (!budget) return { total: 0, safe: null }
  if (budget.backend === 'cpu') return { total: budget.ram_total, safe: budget.ram_safe }
  if (kind === 'llm') return { total: budget.vram_pooled || budget.vram_total, safe: budget.pooled_safe }
  return { total: budget.vram_total, safe: budget.vram_safe }
}

function FitBlock({ fit, kind, budget, reasons }) {
  if (!fit) return null
  const { total, safe } = budgetFor(budget, kind)
  const list = reasons || fit.reasons || []
  return (
    <Section title="Fit on this machine">
      <div className="gp-advd-fit">
        <FitBadge large verdict={fit.verdict} label={fit.label} speed={fit.speed}
          title={VERDICT_HELP[fit.verdict]} />
        {fit.need_bytes && fit.verdict !== 'unknown' ? (
          <span className="gp-advd-fit__need">
            needs <Est>{gb(fit.need_bytes)}</Est>
            {safe ? <> of <Est title="The safe budget: installed memory less the system share and the headroom">
              {gb(safe)}</Est> safe</> : null}
          </span>
        ) : null}
      </div>
      {total && fit.need_bytes && fit.verdict !== 'base' && fit.verdict !== 'unknown' ? (
        <BudgetBar total={total} safe={safe} need={fit.need_bytes}
          label="This model against the memory plan" />
      ) : null}
      {list.length ? (
        <ul className="gp-advd-reasons">
          {list.map((r) => <li key={r}>{r}</li>)}
        </ul>
      ) : null}
    </Section>
  )
}

function Requirement({ need }) {
  if (!need) return null
  return (
    <Section title="Memory breakdown">
      <div className="gp-meta">
        <MetaRow label="Weights" value={fmtBytes(need.weights_bytes)} num />
        <MetaRow label="Working memory" value={gb(need.overhead_bytes)} num inferred
          inferredTitle="Latents, attention buffers and decoder - estimated by model type" />
        {need.companion_bytes ? (
          <MetaRow label="Encoders and VAE" value={fmtBytes(need.companion_bytes)} num
            title="Loaded around the main model, then released" />
        ) : null}
        <MetaRow label="Peak on the GPU" value={gb(need.gpu_bytes)} num inferred
          inferredTitle="Weights plus working memory" />
        <MetaRow label="System memory alongside" value={gb(need.ram_bytes)} num inferred
          inferredTitle="What loading and encoding need in system memory" />
        <MetaRow label="Everything at once" value={gb(need.total_bytes)} num inferred
          inferredTitle="What a CPU-only or unified-memory run must hold" />
      </div>
    </Section>
  )
}

function SafetyBlock({ security }) {
  if (!security) return null
  return (
    <Section title="Safety estimate">
      <div className="gp-advd-safety">
        <ScoreRing size="lg" score={security.score} tone={security.tone} />
        <div className="gp-u-minw0">
          <div className="gp-advd-safety__label">
            <Est title="An estimate from published facts - not a malware scan">
              {security.label}
            </Est>
            <span className="gp-u-meta"> - grade {security.grade}</span>
          </div>
          <p className="gp-advd-safety__note">{security.disclaimer}</p>
        </div>
      </div>
      <ul className="gp-advd-factors">
        {(security.factors || []).map((f) => (
          <li key={f.id} className={'gp-advd-factor gp-advd-factor--' + f.tone}>
            <span className="gp-advd-factor__pts">
              {f.points > 0 ? '+' + f.points : f.points}
            </span>
            <span className="gp-u-minw0">
              <span className="gp-advd-factor__label">{f.label}</span>
              <span className="gp-advd-factor__detail">{f.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </Section>
  )
}

function LinkCheck({ entryId, variant }) {
  const { toastError } = useVault()
  const [state, setState] = useState(null)
  const [busy, setBusy] = useState(false)
  const check = useCallback(() => {
    setBusy(true)
    api.advisorVerifyLink(entryId, variant.id)
      .then(setState)
      .catch((err) => toastError(err, 'The link could not be checked'))
      .finally(() => setBusy(false))
  }, [entryId, variant.id, toastError])

  if (state) {
    const ok = state.state === 'ok'
    const tone = ok ? 'ok' : (state.state === 'gated' ? 'warn'
      : (state.state === 'dead' ? 'danger' : 'neutral'))
    return (
      <Badge tone={tone} title={state.message +
        (state.size_matches === false ? ' The size differs from the catalog.' : '')}>
        {ok ? <Check size={10} aria-hidden="true" /> : null}
        {ok ? 'link live' : state.state}
      </Badge>
    )
  }
  return (
    <Button variant="ghost" size="sm" icon={Link2} label="Check link" loading={busy}
      onClick={check} title="Ask the host whether this file is still there" />
  )
}

function Variants({ entry }) {
  const fit = entry.fit || {}
  return (
    <Section title="Variants" aside={<span className="gp-u-fs-10 gp-u-meta">
      {entry.variants.length}</span>}>
      <ul className="gp-advd-variants">
        {entry.variants.map((v) => {
          const vf = v.fit || {}
          const tags = []
          if (v.id === fit.optimum_variant) tags.push(['brand', 'optimum'])
          if (v.id === fit.maximum_variant && v.id !== fit.optimum_variant) tags.push(['warn', 'maximum'])
          if (v.id === fit.stretch_variant) tags.push(['info', 'stretch'])
          return (
            <li key={v.id} className={'gp-advd-variant' +
              (v.id === fit.lead_variant ? ' gp-advd-variant--lead' : '')}>
              <div className="gp-advd-variant__head">
                <span className="gp-advd-variant__name" title={v.file || v.label}>{v.label}</span>
                {tags.map(([tone, text]) => (
                  <Badge key={text} tone={tone}
                    title="Chosen by the advisor for this machine">{text}</Badge>
                ))}
              </div>
              <div className="gp-advd-variant__row">
                <span className="gp-u-num">{fmtBytes(v.bytes)}</span>
                <Badge mono>{v.format}</Badge>
                {v.precision ? <Badge mono>{v.precision}</Badge> : null}
                {v.parts > 1 ? <Badge mono>{v.parts} parts</Badge> : null}
                {v.security && v.security.tone !== 'ok' ? (
                  <Badge tone={v.security.tone} title={'Safety estimate ' + v.security.score}>
                    {v.security.label}
                  </Badge>
                ) : null}
              </div>
              <div className="gp-advd-variant__row">
                <FitBadge verdict={vf.verdict} label={vf.label} speed={vf.speed}
                  title={(vf.reasons || [])[0]} />
                {vf.need_bytes ? <Est>{gb(vf.need_bytes)}</Est> : null}
                <span className="gp-u-auto-l" />
                {v.download_url ? (
                  <>
                    <LinkCheck entryId={entry.id} variant={v} />
                    <a className="gp-btn gp-btn--sm gp-linkbtn" href={v.download_url}
                      target="_blank" rel="noopener noreferrer"
                      title={v.direct ? 'Download this file from ' +
                        (SOURCE_LABEL[entry.source] || 'the host')
                        : 'Open the file listing on the host'}>
                      {v.direct
                        ? <Download className="gp-btn__icon" aria-hidden="true" />
                        : <ExternalLink className="gp-btn__icon" aria-hidden="true" />}
                      <span className="gp-btn__label">{v.direct ? 'Download' : 'Open'}</span>
                    </a>
                  </>
                ) : null}
              </div>
              {v.save_path ? (
                <div className="gp-advd-variant__path" title="Where ComfyUI expects this file">
                  <FolderOpen size={11} aria-hidden="true" />ComfyUI/{v.save_path}/
                </div>
              ) : null}
              {(v.trained_words || []).length ? (
                <div className="gp-advd-variant__path" title="Trigger words">
                  {v.trained_words.join(', ')}
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function Companions({ items }) {
  if (!items || !items.length) return null
  return (
    <Section title="Also needs" aside={<span className="gp-u-fs-10 gp-u-meta">
      one of each role</span>}>
      <ul className="gp-advd-variants">
        {items.map((c) => (
          <li key={c.role + c.label} className="gp-advd-variant">
            <div className="gp-advd-variant__head">
              <Badge mono>{String(c.role).replace('_', ' ')}</Badge>
              <span className="gp-advd-variant__name" title={c.label}>{c.label}</span>
            </div>
            <div className="gp-advd-variant__row">
              <span className="gp-u-num">{fmtBytes(c.bytes)}</span>
              {c.format ? <Badge mono>{c.format}</Badge> : null}
              <span className="gp-u-auto-l" />
              {c.download_url ? (
                <a className="gp-btn gp-btn--sm gp-btn--ghost gp-linkbtn" href={c.download_url}
                  target="_blank" rel="noopener noreferrer">
                  <Download className="gp-btn__icon" aria-hidden="true" />
                  <span className="gp-btn__label">Download</span>
                </a>
              ) : null}
            </div>
            {c.save_path ? (
              <div className="gp-advd-variant__path">
                <FolderOpen size={11} aria-hidden="true" />ComfyUI/{c.save_path}/
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function CatalogRecord({ id, onOpenUid }) {
  const { toast } = useVault()
  const entry = useResource('advisor:entry:' + id, (s) => api.advisorEntry(id, s))
  if (entry.error) {
    return (
      <div className="gp-details__body">
        <EmptyState tone="error" title="This record could not be loaded"
          text={entry.error.message} />
      </div>
    )
  }
  const e = entry.data
  if (!e) return <div className="gp-details__body"><SkeletonRows rows={12} /></div>
  const safety = e.safety || {}
  return (
    <>
      <div className="gp-details__header">
        <div className="gp-details__eyebrow">
          <KindIcon kind={e.kind} /> {e.kind_label}
          {e.is_new ? <Badge tone="brand">new</Badge> : null}
          {e.gated ? <Badge tone="warn" title="Accept the licence on the host first">gated</Badge> : null}
        </div>
        <h2 className="gp-details__title" title={e.name}>{e.name}</h2>
        <div className="gp-u-fs-11 gp-u-meta gp-u-truncate">
          {e.publisher} - {SOURCE_LABEL[e.source] || e.source}
        </div>
      </div>
      <div className="gp-details__body">
        <div className="gp-advd-actions">
          {e.url ? (
            <a className="gp-btn gp-btn--primary gp-linkbtn" href={e.url}
              target="_blank" rel="noopener noreferrer">
              <ExternalLink className="gp-btn__icon" aria-hidden="true" />
              <span className="gp-btn__label">
                Open on {SOURCE_LABEL[e.source] || 'the web'}
              </span>
            </a>
          ) : null}
          {e.pull ? (
            <Button icon={Copy} label="Copy pull command" onClick={() => {
              if (navigator.clipboard) navigator.clipboard.writeText(e.pull)
              toast({ tone: 'ok', title: 'Copied', message: e.pull })
            }} />
          ) : null}
        </div>

        <FitBlock fit={e.fit} kind={e.kind} budget={e.budget} />
        <SafetyBlock security={e.security} />

        {(e.installed || []).length ? (
          <Section title="Already in your vault">
            {e.installed.map((m) => (
              <button key={m.uid} type="button" className="gp-advd-installed"
                onClick={() => onOpenUid(m.uid)} title="Open in the Models tab">
                <HardDrive size={12} aria-hidden="true" />
                <span className="gp-u-truncate">{m.filename}</span>
                <span className="gp-u-meta gp-u-truncate">{m.folder}</span>
              </button>
            ))}
          </Section>
        ) : null}

        <Variants entry={e} />
        <Companions items={e.companions} />

        <Section title="About">
          {e.description ? <p className="gp-advd-text">{e.description}</p> : null}
          <div className="gp-meta">
            <MetaRow label="Publisher" value={e.publisher} />
            <MetaRow label="Source" value={SOURCE_LABEL[e.source] || e.source} />
            <MetaRow label="Task" value={e.task} />
            <MetaRow label="Family" value={e.family} />
            <MetaRow label="Base model" value={e.base_model} wrap />
            <MetaRow label="Parameters" value={paramsText(e.params)} num />
            <MetaRow label="Context" value={e.context_length
              ? fmtCount(e.context_length) + ' tokens' : null} num />
            <MetaRow label="Architecture" value={e.architecture} />
            <MetaRow label="Runs in" value={e.runtime} />
            <MetaRow label="Licence" value={e.license} wrap
              empty="No licence is stated by the publisher" />
            <MetaRow label="Downloads" value={e.downloads !== undefined && e.downloads !== null
              ? fmtCount(e.downloads) : null} num />
            <MetaRow label="Likes" value={e.likes !== undefined && e.likes !== null
              ? fmtCount(e.likes) : null} num />
            <MetaRow label="Published" value={isoDate(e.created_at)} num />
            <MetaRow label="Updated" value={isoDate(e.updated_at)} num />
            <MetaRow label="Host scan" value={safety.scan}
              tone={safety.scan === 'issues' ? 'danger' : undefined} />
            <MetaRow label="First seen here" value={e.first_seen_at
              ? isoDate(e.first_seen_at) : 'shipped with the app'} num />
            <MetaRow label="Catalog id" value={e.id} wrap />
          </div>
          {(e.tags || []).length ? (
            <div className="gp-advd-tags">
              {e.tags.map((t) => <Badge key={t} mono>{t}</Badge>)}
            </div>
          ) : null}
        </Section>
      </div>
    </>
  )
}

function InstalledRecord({ id, onOpenUid, onOpenCatalog }) {
  const item = useResource('advisor:installed:' + id,
    (s) => api.advisorInstalledDetail(id, s))
  if (item.error) {
    return (
      <div className="gp-details__body">
        <EmptyState tone="error" title="This model could not be assessed"
          text={item.error.message} />
      </div>
    )
  }
  const m = item.data
  if (!m) return <div className="gp-details__body"><SkeletonRows rows={12} /></div>
  return (
    <>
      <div className="gp-details__header">
        <div className="gp-details__eyebrow">
          <KindIcon kind={m.kind} /> {m.kind_label} - in your vault
        </div>
        <h2 className="gp-details__title" title={m.name}>{m.name}</h2>
        <div className="gp-u-fs-11 gp-u-meta gp-u-truncate">{m.filename}</div>
      </div>
      <div className="gp-details__body">
        <div className="gp-advd-actions">
          <Button variant="primary" icon={HardDrive} label="Open in Models"
            onClick={() => onOpenUid(m.uid)} />
          {m.catalog_id ? (
            <Button icon={ShieldCheck} label="Catalog record"
              title={'Matched by file name to ' + m.catalog_name}
              onClick={() => onOpenCatalog(m.catalog_id)} />
          ) : null}
        </div>
        <FitBlock fit={m.fit} kind={m.kind} budget={m.budget} reasons={m.reasons} />
        <Requirement need={m.requirement} />
        <SafetyBlock security={m.security} />
        <Section title="File">
          <div className="gp-meta">
            <MetaRow label="Size" value={fmtBytes(m.size)} num />
            <MetaRow label="Folder" value={m.category} />
            <MetaRow label="Base model" value={m.base_model} />
            <MetaRow label="Precision" value={m.precision} />
            <MetaRow label="Quantisation" value={m.quantization} />
            <MetaRow label="Parameters" value={paramsText(m.params)} num />
            <MetaRow label="Integrity" value={m.integrity}
              tone={m.integrity && m.integrity !== 'ok' ? 'danger' : undefined} />
            <MetaRow label="Hash" value={m.hash_state} />
            <MetaRow label="Path" value={m.abs_path} wrap />
          </div>
        </Section>
      </div>
    </>
  )
}

export default function AdvisorDetails({ target, onClose, onOpenUid }) {
  const { dispatch } = useVault()
  const openCatalog = useCallback((id) => {
    dispatch({ type: 'patch-view', tab: 'advisor', patch: { detailUid: id }, resetOffset: false })
  }, [dispatch])

  let body
  if (!target) {
    body = (
      <>
        <div className="gp-details__header">
          <div className="gp-details__eyebrow">Details</div>
          <h2 className="gp-details__title">No model selected</h2>
        </div>
        <div className="gp-details__body">
          <EmptyState icon={MousePointerClick} title="Nothing selected"
            text="Pick a model to see whether it fits, how safe it looks, which file to take and what else it needs." />
        </div>
      </>
    )
  } else if (String(target).startsWith('installed:')) {
    body = <InstalledRecord id={String(target).slice(10)} onOpenUid={onOpenUid}
      onOpenCatalog={openCatalog} />
  } else {
    body = <CatalogRecord id={target} onOpenUid={onOpenUid} />
  }

  return (
    <aside className="gp-details gp-advd" aria-label="Model details">
      {target ? (
        <Button className="gp-advd__close" variant="ghost" size="sm" iconOnly icon={X}
          aria-label="Close details" title="Close details" onClick={onClose} />
      ) : null}
      <ErrorBoundary title="The details panel hit an error" small>
        {body}
      </ErrorBoundary>
      {target ? (
        <div className="gp-details__footer gp-advd__foot">
          <AlertTriangle size={11} aria-hidden="true" />
          Estimates for planning - not a benchmark and not a malware scan.
        </div>
      ) : null}
    </aside>
  )
}
