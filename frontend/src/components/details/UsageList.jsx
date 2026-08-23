import React from 'react'
import { Workflow, Image as ImageIcon } from 'lucide-react'
import { thumbnailUrl } from '../../services/api.js'
import { count as fmtCount } from '../../services/format.js'
import EmptyState from '../common/EmptyState.jsx'

/*
 * UsageList - "used in N workflows" with working links, plus the recent outputs
 * that came out of this asset. This is the question the owner asked for by name.
 */
export default function UsageList({ usage, onOpenUid, compact }) {
  if (!usage) return null
  const workflows = usage.workflows || usage.top_workflows || []
  const outputs = usage.outputs || null

  if (!workflows.length && (!outputs || !outputs.count)) {
    return (
      <EmptyState
        small
        icon={Workflow}
        title="Not referenced yet"
        text="No indexed workflow or output points at this asset. It may be new, or it may be a spare copy."
      />
    )
  }

  return (
    <>
      {workflows.length ? (
        <div className="gp-list gp-u-mb-5">
          {workflows.map((w) => (
            <button
              key={w.uid}
              type="button"
              className="gp-row gp-focus-inset"
              onClick={() => onOpenUid(w.uid)}
              title={w.rel_path || w.name}
            >
              <Workflow className="gp-row__thumb gp-u-p-0" aria-hidden="true" />
              <span className="gp-row__name">{w.name}</span>
              {w.via && w.via.length ? (
                <span className="gp-row__cell gp-row__cell--grow">
                  {w.via[0].class}.{w.via[0].input}
                </span>
              ) : null}
              <span className="gp-row__cell gp-row__cell--num">
                {w.occurrences ? 'x' + w.occurrences : ''}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {outputs && outputs.count ? (
        <>
          <div className="gp-u-row gp-u-between gp-u-mb-4">
            <span className="gp-u-fs-11 gp-u-meta">
              <ImageIcon size={11} aria-hidden="true" /> {fmtCount(outputs.count)} output
              {outputs.count === 1 ? '' : 's'}
            </span>
          </div>
          {outputs.recent && outputs.recent.length ? (
            <div className="gp-grid">
              {outputs.recent.slice(0, compact ? 4 : 8).map((o) => (
                <button
                  key={o.uid}
                  type="button"
                  className="gp-card"
                  onClick={() => onOpenUid(o.uid)}
                  title={o.filename}
                >
                  <span className="gp-card__thumb">
                    <img
                      className="gp-card__media"
                      src={o.thumbnail_url || thumbnailUrl(o.uid, 160)}
                      width={160}
                      height={160}
                      loading="lazy"
                      decoding="async"
                      alt=""
                    />
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </>
  )
}
