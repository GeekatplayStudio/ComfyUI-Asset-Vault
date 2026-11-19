import React from 'react'
import { HardDrive } from 'lucide-react'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { bytes as fmtBytes } from '../../services/format.js'
import { ScoreRing } from './charts.jsx'
import { Est, FitBadge, NeedBar, KindIcon, gb, VERDICT_HELP } from './parts.jsx'

/*
 * InstalledPanel - the same analysis, for the files already in the vault.
 *
 * Sizes and formats here are read from the owner's own disk (amber side);
 * the requirement, the verdict and the safety score are still estimates.
 */
export default function InstalledPanel({ data, loading, error, selectedId, onOpen, budget }) {
  if (error) {
    return <EmptyState tone="error" title="Your models could not be assessed"
      text={error.message} />
  }
  if (loading && !data) return <SkeletonRows rows={12} />
  const rows = (data && data.items) || []
  if (!rows.length) {
    const indexed = data && data.meta && data.meta.indexed_total
    return (
      <EmptyState icon={HardDrive}
        title={indexed ? 'Nothing matches these filters' : 'No models are indexed yet'}
        text={indexed ? 'Clear a filter to see the rest of your models.'
          : 'Point the vault at your ComfyUI folder and run a scan; every model it finds is assessed here.'} />
    )
  }
  const safePct = budget && budget.vram_max ? (budget.vram_safe / budget.vram_max) * 100 : null
  return (
    <table className="gp-table gp-advtable">
      <thead>
        <tr>
          <th scope="col">Model on disk</th>
          <th scope="col" className="gp-advcol--b">Folder</th>
          <th scope="col" className="gp-table__num gp-advcol--b">File</th>
          <th scope="col" title="Estimated peak memory">Needs</th>
          <th scope="col" title="Estimated for this machine">Fit</th>
          <th scope="col" title="Safety estimate, 0-100 - not a malware scan">Safety</th>
          <th scope="col" className="gp-advcol--a">Catalog match</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const target = 'installed:' + row.id
          return (
            <tr key={row.uid}
              className={'gp-advrow' + (selectedId === target ? ' gp-advrow--selected' : '')}
              tabIndex={0} aria-selected={selectedId === target}
              onClick={() => onOpen(target)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(target) }
              }}>
              <td className="gp-advrow__name">
                <span className="gp-advrow__cell">
                <KindIcon kind={row.kind} />
                <span className="gp-u-minw0">
                  <span className="gp-advrow__title">
                    <span className="gp-u-truncate gp-u-local">{row.name}</span>
                  </span>
                  <span className="gp-advrow__sub">
                    {row.kind_label}
                    {row.base_model && row.base_model !== 'Unknown' ? ' - ' + row.base_model : ''}
                    {row.precision ? ' - ' + row.precision : ''}
                    {row.quantization ? ' - ' + row.quantization : ''}
                  </span>
                </span>
                </span>
              </td>
              <td className="gp-u-meta gp-advcol--b">{row.category}</td>
              <td className="gp-table__num gp-advcol--b">{fmtBytes(row.size)}</td>
              <td className="gp-advrow__need">
                <Est>{gb(row.fit.need_bytes)}</Est>
                <NeedBar pct={row.fit.used_pct_of_ceiling} safePct={safePct}
                  verdict={row.fit.verdict} />
              </td>
              <td>
                <FitBadge verdict={row.fit.verdict} label={row.fit.label}
                  speed={row.fit.speed}
                  title={(row.reasons || [])[0] || VERDICT_HELP[row.fit.verdict]} />
              </td>
              <td className="gp-advrow__score">
                <ScoreRing score={row.security.score} tone={row.security.tone} />
              </td>
              <td className="gp-u-meta gp-advcol--flex gp-advcol--a">{row.catalog_name || '--'}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
