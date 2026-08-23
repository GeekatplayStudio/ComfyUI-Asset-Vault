import React, { useState, useCallback } from 'react'
import {
  Box, Package, Puzzle, Workflow, Image as ImageIcon, Film, Music, Boxes,
  FileText, Star, AlertTriangle
} from 'lucide-react'
import { thumbnailUrl } from '../../services/api.js'
import InlinePlayer, { isPlayable } from '../common/InlinePlayer.jsx'
import { bytes, params, dimensions, duration, humanise } from '../../services/format.js'
import Badge, { HashBadge, IntegrityBadge, BaseModelBadge } from '../common/Badge.jsx'

/*
 * presentAsset - one place that turns any of the five entity shapes into the
 * card / row vocabulary. Both AssetCard and AssetListRow read from it, so the
 * grid and the list can never disagree about what an asset is called.
 */

const MEDIA_ICONS = {
  image: ImageIcon, video: Film, audio: Music, model3d: Boxes, text: FileText
}

export function presentAsset(item, scope) {
  if (scope === 'models') {
    const family = item.base_model && item.base_model.family
    const confidence = item.base_model && item.base_model.confidence
    const inferred = Boolean(family && family !== 'Unknown' &&
      confidence !== null && confidence !== undefined && confidence < 0.7)
    const integrity = typeof item.integrity === 'string'
      ? item.integrity
      : (item.integrity && item.integrity.status)
    return {
      uid: item.uid,
      title: item.filename || item.name,
      subtitle: humanise(item.category),
      icon: Box,
      wide: false,
      inferred,
      missing: Boolean(item.missing),
      error: integrity && integrity !== 'ok',
      meta: [
        bytes(item.size),
        item.precision || null,
        item.params && item.params.display ? item.params.display : null
      ].filter(Boolean),
      badges: [{ key: 'base', node: <BaseModelBadge base={item.base_model} overlay /> }],
      corner: [
        { key: 'integrity', node: <IntegrityBadge status={integrity} overlay /> },
        { key: 'hash', node: <HashBadge state={item.hash && item.hash.state} overlay /> }
      ],
      rowCells: [
        { key: 'role', node: <Badge tone="role">{item.role}</Badge> },
        { key: 'base', node: <BaseModelBadge base={item.base_model} /> },
        { key: 'size', num: true, node: bytes(item.size) },
        { key: 'params', num: true, node: (item.params && item.params.display) || '' },
        { key: 'hash', node: <HashBadge state={item.hash && item.hash.state} /> }
      ],
      favorite: item.favorite
    }
  }

  if (scope === 'node_packages') {
    return {
      uid: item.uid,
      title: item.display_name || item.folder_name,
      subtitle: item.author || 'unknown author',
      icon: Package,
      wide: false,
      inferred: item.extraction && item.extraction.confidence === 'inferred',
      missing: Boolean(item.missing),
      error: false,
      meta: [
        item.class_count + (item.class_count === 1 ? ' class' : ' classes'),
        bytes(item.size)
      ].filter(Boolean),
      badges: [item.is_official
        ? { key: 'official', node: <Badge tone="brand" overlay>official</Badge> }
        : null].filter(Boolean),
      corner: [item.update && item.update.has_update
        ? { key: 'upd', node: <Badge tone="info" overlay>update</Badge> }
        : null].filter(Boolean),
      rowCells: [
        { key: 'author', node: item.author || '' },
        { key: 'classes', num: true, node: item.class_count },
        { key: 'workflows', num: true, node: (item.counts && item.counts.workflows) || 0 },
        { key: 'size', num: true, node: bytes(item.size) },
        {
          key: 'state',
          node: item.enabled
            ? null
            : <Badge tone="warn">disabled</Badge>
        }
      ],
      favorite: false
    }
  }

  if (scope === 'node_classes') {
    const outputs = (item.outputs && item.outputs.types) || []
    return {
      uid: item.uid,
      title: item.display_name || item.node_id,
      subtitle: item.category || 'uncategorised',
      icon: Puzzle,
      wide: false,
      inferred: item.confidence === 'inferred',
      missing: false,
      error: false,
      noThumb: true,
      meta: [
        item.package ? item.package.name : null,
        outputs.length ? outputs.join(', ') : null
      ].filter(Boolean),
      badges: [],
      corner: item.flags && item.flags.deprecated
        ? [{ key: 'dep', node: <Badge tone="warn" overlay>deprecated</Badge> }]
        : [],
      rowCells: [
        { key: 'pkg', node: item.package ? item.package.name : '' },
        { key: 'category', node: item.category || '' },
        { key: 'out', node: outputs.length ? outputs.join(', ') : '' },
        { key: 'wf', num: true, node: (item.counts && item.counts.workflows) || 0 },
        { key: 'conf', node: <Badge tone={'conf-' + (item.confidence || 'inferred')}>{item.confidence}</Badge> }
      ],
      favorite: false
    }
  }

  if (scope === 'workflows') {
    const missingNodes = (item.counts && item.counts.missing_nodes) || 0
    const missingModels = (item.counts && item.counts.missing_models) || 0
    const broken = missingNodes + missingModels > 0
    return {
      uid: item.uid,
      title: item.name,
      subtitle: item.folder || 'root',
      icon: Workflow,
      wide: true,
      inferred: item.description_source === 'derived' || item.description_source === 'ollama',
      missing: Boolean(item.missing),
      error: broken,
      meta: [
        (item.counts && item.counts.nodes) + ' nodes',
        item.base_model && item.base_model !== 'Unknown' ? item.base_model : null,
        broken ? (missingNodes + missingModels) + ' missing' : 'runnable'
      ].filter(Boolean),
      badges: item.base_model && item.base_model !== 'Unknown'
        ? [{ key: 'base', node: <Badge tone="base" overlay>{item.base_model}</Badge> }]
        : [],
      corner: broken
        ? [{ key: 'broken', node: <Badge tone="dep-missing" overlay>{missingNodes + missingModels} missing</Badge> }]
        : [{ key: 'ok', node: <Badge tone="dep-satisfied" overlay>runnable</Badge> }],
      rowCells: [
        { key: 'base', node: item.base_model && item.base_model !== 'Unknown' ? <Badge tone="base">{item.base_model}</Badge> : null },
        { key: 'nodes', num: true, node: (item.counts && item.counts.nodes) || 0 },
        { key: 'folder', grow: true, node: item.folder || '' },
        {
          key: 'state',
          node: broken
            ? <Badge tone="dep-missing">{missingNodes + missingModels} missing</Badge>
            : <Badge tone="dep-satisfied">runnable</Badge>
        }
      ],
      favorite: false
    }
  }

  // outputs
  const MediaIcon = MEDIA_ICONS[item.media_kind] || FileText
  return {
    uid: item.uid,
    title: item.filename,
    subtitle: item.folder || 'output root',
    icon: MediaIcon,
    wide: false,
    inferred: false,
    missing: Boolean(item.missing),
    error: false,
    meta: [
      dimensions(item.width, item.height) ||
        (item.duration_ms ? duration(item.duration_ms) : null),
      bytes(item.size)
    ].filter(Boolean),
    badges: item.media_kind !== 'image'
      ? [{ key: 'kind', node: <Badge tone="media" overlay>{item.media_kind}</Badge> }]
      : [],
    corner: item.favorite
      ? [{ key: 'fav', node: <Badge tone="brand" overlay><Star size={9} aria-hidden="true" /></Badge> }]
      : [],
    rowCells: [
      { key: 'kind', node: <Badge tone="media">{item.media_kind}</Badge> },
      { key: 'dims', num: true, node: dimensions(item.width, item.height) || duration(item.duration_ms) || '' },
      { key: 'model', grow: true, node: item.model_name || '' },
      { key: 'size', num: true, node: bytes(item.size) }
    ],
    favorite: item.favorite
  }
}

/* --------------------------------------------------------------------- card */

function Thumb({ uid, tier, alt, placeholderIcon: Icon, inferred, noThumb }) {
  const [failed, setFailed] = useState(noThumb === true)
  const onError = useCallback(() => setFailed(true), [])
  if (failed) {
    return (
      <div className={'gp-card__placeholder' + (inferred ? ' gp-card__placeholder--inferred' : '')}>
        <Icon aria-hidden="true" />
      </div>
    )
  }
  return (
    <img
      className="gp-card__media"
      src={thumbnailUrl(uid, tier)}
      width={tier}
      height={tier}
      loading="lazy"
      decoding="async"
      alt={alt}
      onError={onError}
    />
  )
}

function AssetCard(props) {
  const {
    item, scope, tier, selected, onOpen, onSelect, onContextMenu, checked, onToggleCheck
  } = props
  const p = presentAsset(item, scope)

  const classes = ['gp-card']
  if (selected || checked) classes.push('gp-card--selected')
  if (p.missing) classes.push('gp-card--missing')
  if (p.error) classes.push('gp-card--error')
  else if (p.inferred) classes.push('gp-card--inferred')

  return (
    <article
      className={classes.join(' ')}
      aria-selected={selected ? 'true' : undefined}
      data-uid={p.uid}
      onContextMenu={onContextMenu}
      onClick={(e) => onSelect(item, e)}
      onDoubleClick={() => onOpen(item)}
    >
      <div className="gp-card__mediawrap">
      <button
        type="button"
        className={'gp-card__thumb gp-focus-inset' + (p.wide ? ' gp-card__thumb--wide' : '')}
        aria-label={'Open ' + p.title}
      >
        <Thumb
          uid={p.uid}
          tier={tier}
          alt=""
          placeholderIcon={p.icon}
          inferred={p.inferred}
          noThumb={p.noThumb}
        />
        {p.badges.length ? (
          <div className="gp-card__badges">
            {p.badges.map((b) => <React.Fragment key={b.key}>{b.node}</React.Fragment>)}
          </div>
        ) : null}
        {p.corner.length ? (
          <div className="gp-card__corner">
            {p.corner.map((b) => <React.Fragment key={b.key}>{b.node}</React.Fragment>)}
          </div>
        ) : null}
      </button>
      {isPlayable(item.media_kind) ? (
        <InlinePlayer
          uid={p.uid}
          mediaKind={item.media_kind}
          label={'Play ' + p.title}
        />
      ) : null}
      </div>
      <label className="gp-card__check gp-check" onClick={(e) => e.stopPropagation()}>
        <input
          className="gp-check__input"
          type="checkbox"
          checked={Boolean(checked)}
          aria-label={'Select ' + p.title}
          onChange={() => onToggleCheck(item)}
        />
        <span className="gp-check__box">
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          </svg>
        </span>
      </label>
      <div className="gp-card__body">
        <div className="gp-card__title" title={p.title}>{p.title}</div>
        <div className="gp-card__meta">
          {p.meta.map((text, i) => (
            <React.Fragment key={text + ':' + i}>
              {i > 0 ? <span className="gp-card__meta-sep">/</span> : null}
              <span>{text}</span>
            </React.Fragment>
          ))}
        </div>
      </div>
    </article>
  )
}

export default React.memo(AssetCard)
