import React, { useState, useEffect, useCallback } from 'react'
import { Trash2, Pencil, ExternalLink } from 'lucide-react'
import { thumbnailUrl } from '../../services/api.js'
import { presentAsset } from './AssetCard.jsx'
import InlinePlayer, { isPlayable } from '../common/InlinePlayer.jsx'

/*
 * AssetListRow - the dense list mode. Numbers live in __cell--num so columns of
 * sizes and counts line up in the tabular monospace face.
 */

function RowThumb({ uid, alt, noThumb }) {
  const [failed, setFailed] = useState(Boolean(noThumb))
  const onError = useCallback(() => setFailed(true), [])
  if (failed) return <span className="gp-row__thumb gp-u-ph-local" aria-hidden="true" />
  return (
    <img
      className="gp-row__thumb"
      src={thumbnailUrl(uid, 160)}
      width={24}
      height={24}
      loading="lazy"
      decoding="async"
      alt={alt}
      onError={onError}
    />
  )
}

function AssetListRow(props) {
  const {
    item, scope, selected, checked, onSelect, onOpen, onRename, onDelete,
    onOpenExternal, onContextMenu
  } = props
  const p = presentAsset(item, scope)

  const [playing, setPlaying] = useState(false)
  useEffect(() => { setPlaying(false) }, [p.uid])

  const classes = ['gp-row', 'gp-focus-inset']
  if (playing) classes.push('gp-row--playing')
  if (selected || checked) classes.push('gp-row--selected')
  if (p.missing) classes.push('gp-row--missing')
  if (p.error) classes.push('gp-row--error')
  else if (p.inferred) classes.push('gp-row--inferred')

  return (
    <div
      className={classes.join(' ')}
      role="row"
      tabIndex={0}
      aria-selected={selected ? 'true' : undefined}
      data-uid={p.uid}
      onClick={(e) => onSelect(item, e)}
      onDoubleClick={() => onOpen(item)}
      onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); onOpen(item) } }}
      onContextMenu={onContextMenu}
    >
      <RowThumb uid={p.uid} alt="" noThumb={p.noThumb} />
      <span className="gp-row__name" title={p.title}>{p.title}</span>
      {/* Every cell is rendered even when it has nothing to say.  Skipping the
          empty ones used to pull the following columns left, so no two rows
          lined up -- each row laid itself out independently. */}
      {p.rowCells.map((cell) => {
        const empty = cell.node === null || cell.node === undefined || cell.node === ''
        const cls = ['gp-row__cell']
        if (cell.num) cls.push('gp-row__cell--num')
        if (cell.grow) cls.push('gp-row__cell--grow')
        if (empty) cls.push('gp-row__cell--empty')
        return (
          <span key={cell.key} className={cls.join(' ')}>
            {empty ? null : cell.node}
          </span>
        )
      })}
      <span className="gp-row__actions">
        {isPlayable(item.media_kind) ? (
          <InlinePlayer
            uid={p.uid}
            mediaKind={item.media_kind}
            size="sm"
            label={'Play ' + p.title}
            onActivate={() => setPlaying(true)}
          />
        ) : null}
        {onOpenExternal ? (
          <button
            type="button"
            className="gp-btn gp-btn--ghost gp-btn--sm gp-btn--icon"
            aria-label={'Open ' + p.title + ' in ComfyUI'}
            title="Open in ComfyUI"
            onClick={(e) => { e.stopPropagation(); onOpenExternal(item) }}
          >
            <ExternalLink className="gp-btn__icon" aria-hidden="true" />
          </button>
        ) : null}
        {onRename ? (
          <button
            type="button"
            className="gp-btn gp-btn--ghost gp-btn--sm gp-btn--icon"
            aria-label={'Rename ' + p.title}
            onClick={(e) => { e.stopPropagation(); onRename(item) }}
          >
            <Pencil className="gp-btn__icon" aria-hidden="true" />
          </button>
        ) : null}
        {onDelete ? (
          <button
            type="button"
            className="gp-btn gp-btn--danger-ghost gp-btn--sm gp-btn--icon"
            aria-label={'Delete ' + p.title}
            onClick={(e) => { e.stopPropagation(); onDelete(item) }}
          >
            <Trash2 className="gp-btn__icon" aria-hidden="true" />
          </button>
        ) : null}
      </span>
    </div>
  )
}

export default React.memo(AssetListRow)
