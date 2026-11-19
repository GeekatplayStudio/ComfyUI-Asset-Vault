import React, { useCallback, useRef, useState } from 'react'
import { ExternalLink, Lock, Sparkles, SearchX } from 'lucide-react'
import EmptyState from '../common/EmptyState.jsx'
import Badge from '../common/Badge.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { bytes as fmtBytes } from '../../services/format.js'
import { ScoreRing, BudgetBar } from './charts.jsx'
import {
  Est, FitBadge, NeedBar, KindIcon, gb, compactCount, paramsText, isoDate,
  SOURCE_LABEL, VERDICT_HELP
} from './parts.jsx'

/*
 * CatalogPanel - every known model, assessed for this machine, as one table.
 *
 * Hovering a row opens a summary card; clicking it opens the full record in
 * the details pane. The card is a preview, never the only way in: the same
 * facts are in the pane, which is keyboard reachable.
 *
 * Columns that are published facts (name, size, downloads) are plain; columns
 * the advisor worked out (needs, fit, safety) are violet.
 */

const HOVER_DELAY_MS = 320
const CARD_W = 372

function HoverCard({ row, at, budget }) {
  if (!row || !at) return null
  const fit = row.fit || {}
  const llm = row.kind === 'llm'
  const total = budget ? (budget.backend === 'cpu' ? budget.ram_total : budget.vram_total) : 0
  const safe = budget ? (budget.backend === 'cpu' ? budget.ram_safe
    : (llm ? budget.pooled_safe : budget.vram_safe)) : null
  return (
    <div className="gp-hovercard" role="tooltip" style={{ left: at.left, top: at.top }}>
      <div className="gp-hovercard__head">
        <KindIcon kind={row.kind} />
        <span className="gp-hovercard__title">{row.name}</span>
        <ScoreRing score={row.security.score} tone={row.security.tone} />
      </div>
      <div className="gp-hovercard__sub">
        {row.publisher} - {SOURCE_LABEL[row.source] || row.source}
        {row.family ? ' - ' + row.family : ''}
      </div>
      {row.description
        ? <p className="gp-hovercard__text">{row.description}</p> : null}
      <div className="gp-hovercard__fit">
        <FitBadge verdict={fit.verdict} label={fit.label} speed={fit.speed} />
        {fit.need_bytes ? <span className="gp-u-fs-11">needs <Est>{gb(fit.need_bytes)}</Est></span> : null}
      </div>
      {total && fit.need_bytes && fit.verdict !== 'base' ? (
        <BudgetBar total={total} safe={safe} need={fit.need_bytes}
          label="This model against the memory plan" />
      ) : null}
      {row.fit_reason ? <p className="gp-hovercard__reason">{row.fit_reason}</p> : null}
      <dl className="gp-hovercard__facts">
        <div><dt>Best variant</dt><dd>{row.lead.label}</dd></div>
        <div><dt>File</dt><dd>{fmtBytes(row.lead.bytes)} {row.lead.format}</dd></div>
        {row.params ? <div><dt>Parameters</dt><dd>{paramsText(row.params)}</dd></div> : null}
        <div><dt>Licence</dt><dd>{row.license || 'not stated'}</dd></div>
        <div><dt>Safety</dt><dd><Est title="An estimate from published facts - not a malware scan">
          {row.security.label}</Est></dd></div>
        {row.variant_count > 1
          ? <div><dt>Variants</dt><dd>{row.variant_count}</dd></div> : null}
      </dl>
      <div className="gp-hovercard__foot">Click for the full record</div>
    </div>
  )
}

export function useHoverCard() {
  const [hover, setHover] = useState(null)
  const timer = useRef(null)

  const open = useCallback((row, event) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const x = event.clientX
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      const left = Math.max(8, Math.min(window.innerWidth - CARD_W - 12, x + 18))
      const below = rect.bottom + 6
      const top = below + 330 > window.innerHeight
        ? Math.max(8, rect.top - 336) : below
      setHover({ row, at: { left: Math.round(left), top: Math.round(top) } })
    }, HOVER_DELAY_MS)
  }, [])

  const close = useCallback(() => {
    clearTimeout(timer.current)
    setHover(null)
  }, [])

  return { hover, open, close }
}

export function ModelRow({ row, selected, onOpen, onHover, onLeave, safePct }) {
  const fit = row.fit || {}
  return (
    <tr
      className={'gp-advrow' + (selected ? ' gp-advrow--selected' : '') +
        (fit.verdict === 'no' ? ' gp-advrow--dim' : '')}
      tabIndex={0}
      aria-selected={selected}
      onClick={() => onOpen(row.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(row.id) }
      }}
      onMouseEnter={onHover ? (e) => onHover(row, e) : undefined}
      onMouseLeave={onLeave}
    >
      <td className="gp-advrow__name">
        <span className="gp-advrow__cell">
        <KindIcon kind={row.kind} />
        <span className="gp-u-minw0">
          <span className="gp-advrow__title">
            <span className="gp-u-truncate">{row.name}</span>
            {row.is_new ? <Badge tone="brand" title="First seen in the last week">new</Badge> : null}
            {row.gated ? (
              <Lock className="gp-advrow__flag" aria-label="Gated"
                title="Gated: accept the licence on the host before downloading" />
            ) : null}
            {row.curated ? (
              <Sparkles className="gp-advrow__flag" aria-label="Pinned"
                title="Pinned in the source list" />
            ) : null}
          </span>
          <span className="gp-advrow__sub">
            {row.publisher} - {SOURCE_LABEL[row.source] || row.source}
            {row.family ? ' - ' + row.family : ''}
          </span>
        </span>
        </span>
      </td>
      <td className="gp-advrow__variant gp-advcol--a">
        <span className="gp-u-truncate" title={row.lead.label}>{row.lead.label}</span>
        <span className="gp-advrow__sub">
          {(row.lead.precision || row.lead.format || '')}
          {row.variant_count > 1 ? ' - ' + row.variant_count + ' variants' : ''}
        </span>
      </td>
      <td className="gp-table__num gp-advcol--b">{fmtBytes(row.lead.bytes)}</td>
      <td className="gp-advrow__need">
        {fit.need_bytes && fit.verdict !== 'unknown' ? (
          <>
            <Est>{gb(fit.need_bytes)}</Est>
            <NeedBar pct={fit.used_pct_of_ceiling} safePct={safePct} verdict={fit.verdict} />
          </>
        ) : <span className="gp-u-meta">--</span>}
      </td>
      <td>
        <FitBadge verdict={fit.verdict} label={fit.label} speed={fit.speed}
          title={row.fit_reason || VERDICT_HELP[fit.verdict]} />
      </td>
      <td className="gp-advrow__score">
        <ScoreRing score={row.security.score} tone={row.security.tone}
          title={'Safety estimate ' + row.security.score + ' of 100 (' +
            row.security.label + ') - not a malware scan'} />
      </td>
      <td className="gp-table__num gp-advcol--c">{compactCount(row.downloads) || '--'}</td>
      <td className="gp-table__num gp-u-meta gp-advcol--c">{isoDate(row.updated_at) || '--'}</td>
      <td className="gp-advrow__link">
        {row.url ? (
          <a
            className="gp-btn gp-btn--ghost gp-btn--icon gp-btn--sm"
            href={row.url} target="_blank" rel="noopener noreferrer"
            aria-label={'Open ' + row.name + ' on ' + (SOURCE_LABEL[row.source] || 'the web')}
            title={'Open on ' + (SOURCE_LABEL[row.source] || 'the web')}
            onClick={(e) => e.stopPropagation()}
          >
            <ExternalLink className="gp-btn__icon" aria-hidden="true" />
          </a>
        ) : null}
      </td>
    </tr>
  )
}

export function ModelTable({ rows, selectedId, onOpen, budget, hoverable = true }) {
  const { hover, open, close } = useHoverCard()
  const llmSafe = budget && budget.pooled_max ? (budget.pooled_safe / budget.pooled_max) * 100 : null
  const safe = budget && budget.vram_max ? (budget.vram_safe / budget.vram_max) * 100 : null
  return (
    <>
      <table className="gp-table gp-advtable" onMouseLeave={close}>
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col" className="gp-advcol--a">Best variant for this machine</th>
            <th scope="col" className="gp-table__num gp-advcol--b">File</th>
            <th scope="col" title="Estimated peak memory">Needs</th>
            <th scope="col" title="Estimated for this machine">Fit</th>
            <th scope="col" title="Safety estimate, 0-100 - not a malware scan">Safety</th>
            <th scope="col" className="gp-table__num gp-advcol--c">Downloads</th>
            <th scope="col" className="gp-table__num gp-advcol--c">Updated</th>
            <th scope="col"><span className="gp-u-sr-only">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <ModelRow
              key={row.id} row={row} selected={row.id === selectedId}
              onOpen={(id) => { close(); onOpen(id) }}
              onHover={hoverable ? open : undefined} onLeave={close}
              safePct={row.kind === 'llm' ? llmSafe : safe}
            />
          ))}
        </tbody>
      </table>
      {hover ? <HoverCard row={hover.row} at={hover.at} budget={budget} /> : null}
    </>
  )
}

export default function CatalogPanel(props) {
  const { data, loading, error, selectedId, onOpen, budget, onClear, filtered } = props

  if (error) {
    return <EmptyState tone="error" title="The catalog could not be loaded"
      text={error.message} />
  }
  if (loading && !data) return <SkeletonRows rows={14} />
  const rows = (data && data.items) || []
  if (!rows.length) {
    return (
      <EmptyState
        icon={SearchX}
        title={filtered ? 'Nothing matches these filters' : 'The catalog is empty'}
        text={filtered
          ? 'Loosen a filter, or clear them to see every model the advisor knows.'
          : 'Open Sources and updates and refresh the catalog.'}
        actions={filtered && onClear
          ? <button type="button" className="gp-btn" onClick={onClear}>
            <span className="gp-btn__label">Clear filters</span></button> : null}
      />
    )
  }
  return <ModelTable rows={rows} selectedId={selectedId} onOpen={onOpen} budget={budget} />
}
