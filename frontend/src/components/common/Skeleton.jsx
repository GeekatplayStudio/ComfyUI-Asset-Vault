import React from 'react'

/** Skeleton - shaped like the content it replaces so nothing jumps on arrival. */
export function Skeleton({ variant }) {
  return <div className={'gp-skel' + (variant ? ' gp-skel--' + variant : '')} />
}

export function SkeletonCard() {
  return (
    <div className="gp-skel-card">
      <div className="gp-skel gp-skel--thumb" />
      <div className="gp-skel-card__body">
        <div className="gp-skel gp-skel--title" />
        <div className="gp-skel gp-skel--line-sm" />
      </div>
    </div>
  )
}

export function SkeletonRows({ rows }) {
  return (
    <div className="gp-list">
      {Array.from({ length: rows || 8 }, (_, i) => (
        <div key={'skel-row-' + i} className="gp-skel gp-skel--row" />
      ))}
    </div>
  )
}

export function SkeletonGrid({ cards }) {
  return (
    <div className="gp-grid">
      {Array.from({ length: cards || 12 }, (_, i) => (
        <SkeletonCard key={'skel-card-' + i} />
      ))}
    </div>
  )
}

export function SkeletonMeta({ rows }) {
  return (
    <div className="gp-meta">
      {Array.from({ length: rows || 6 }, (_, i) => (
        <div key={'skel-meta-' + i} className="gp-skel gp-skel--text" />
      ))}
    </div>
  )
}

export default Skeleton
