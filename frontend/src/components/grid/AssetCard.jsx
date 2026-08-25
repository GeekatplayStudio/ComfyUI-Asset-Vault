import React, { useState, useCallback } from 'react'
import {
  Box, Package, Puzzle, Workflow, Image as ImageIcon, Film, Music, Boxes,
  FileText, Star, ExternalLink, Layers, Aperture, Type, Eye, Sliders,
  ZoomIn, Hash, Cpu
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

/* Category → icon + face accent class.  Matched by substring so folder
   variants ("sdxl_loras", "controlnet_aux") land in the right family. */
const CATEGORY_FACES = [
  ['lora', Layers, 'lora'], ['lycoris', Layers, 'lora'],
  ['vae', Aperture, 'vae'],
  ['clip_vision', Eye, 'vision'], ['vision', Eye, 'vision'],
  ['clip', Type, 'text'], ['text_encoder', Type, 'text'], ['t5', Type, 'text'],
  ['controlnet', Sliders, 'control'], ['control', Sliders, 'control'],
  ['upscale', ZoomIn, 'upscale'], ['esrgan', ZoomIn, 'upscale'],
  ['embedding', Hash, 'embedding'], ['textual', Hash, 'embedding'],
  ['gguf', Cpu, 'quant'], ['unet', Cpu, 'quant'],
  ['motion', Film, 'motion'], ['animate', Film, 'motion'],
  ['checkpoint', Box, 'checkpoint'], ['diffusion', Box, 'checkpoint']
]

function categoryFace(category) {
  const key = String(category || '').toLowerCase()
  for (const [needle, Icon, cls] of CATEGORY_FACES) {
    if (key.includes(needle)) return { Icon, cls }
  }
  return { Icon: Box, cls: 'default' }
}

/* One status tone per card face: ok = usable as-is, warn = needs attention,
   danger = broken or gone, neutral = status does not apply. */
function modelBar(item, integrity) {
  if (item.missing) return 'danger'
  if (integrity && integrity !== 'ok') return 'danger'
  if (item.hash && item.hash.state === 'failed') return 'warn'
  return 'ok'
}

const EXT_LABELS = {
  '.safetensors': 'safetensors', '.sft': 'safetensors', '.ckpt': 'checkpoint',
  '.pt': 'pytorch', '.pth': 'pytorch', '.bin': 'binary', '.onnx': 'onnx',
  '.gguf': 'gguf', '.pkl': 'pickle'
}

function extLabel(ext) {
  const key = String(ext || '').toLowerCase()
  return EXT_LABELS[key] || (key ? key.replace('.', '') : null)
}

function matchBadge(item) {
  const matched = item.match && item.match.matched
  if (!matched || !matched.length) return null
  const semantic = matched.includes('semantic')
  const label = matched.includes('name') ? 'name match'
    : semantic && !matched.includes('lexical') ? 'semantic match'
    : semantic ? 'text + semantic' : 'text match'
  return {
    key: 'match',
    node: <Badge tone={semantic ? 'ai' : 'neutral'} overlay>{label}</Badge>
  }
}

function formatDownloads(n) {
  if (!n || n < 1) return null
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + 'k'
  return String(n)
}

export function CommunityStars({ rating, downloads }) {
  const value = Number(rating)
  if (!value || value <= 0) return null
  const dl = formatDownloads(downloads)
  return (
    <span className="gp-stars" title={'Community rating ' + value.toFixed(1)
      + (dl ? ' · ' + dl + ' downloads' : '')}>
      <Star aria-hidden="true" />
      <span>{value.toFixed(1)}</span>
      {dl ? <span>· {dl}</span> : null}
    </span>
  )
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
    const { Icon, cls } = categoryFace(item.category)
    const community = item.community || {}
    return {
      uid: item.uid,
      title: item.filename || item.name,
      subtitle: humanise(item.category),
      icon: Icon,
      wide: false,
      inferred,
      missing: Boolean(item.missing),
      error: integrity && integrity !== 'ok',
      // Only fetch a server thumbnail when a real preview image exists; the
      // contextual card face replaces the generated gradient placeholder.
      noThumb: item.has_preview === false,
      face: {
        Icon, cls, bar: modelBar(item, integrity),
        type: extLabel(item.ext) || humanise(item.category),
        sub: humanise(item.category)
      },
      stars: <CommunityStars rating={community.rating} downloads={community.downloads} />,
      meta: [
        bytes(item.size),
        item.precision || null,
        item.params && item.params.display ? item.params.display : null
      ].filter(Boolean),
      badges: [
        { key: 'base', node: <BaseModelBadge base={item.base_model} overlay /> },
        matchBadge(item)
      ].filter(Boolean),
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
      noThumb: true,
      face: {
        Icon: Package, cls: 'default',
        bar: item.missing ? 'danger' : item.enabled === false ? 'warn' : 'ok',
        type: 'node pack',
        sub: item.author || null
      },
      meta: [
        item.class_count + (item.class_count === 1 ? ' class' : ' classes'),
        bytes(item.size)
      ].filter(Boolean),
      badges: [item.is_official
        ? { key: 'official', node: <Badge tone="brand" overlay>official</Badge> }
        : null, matchBadge(item)].filter(Boolean),
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
      face: {
        Icon: Puzzle, cls: 'default',
        bar: item.flags && item.flags.deprecated ? 'warn' : 'neutral',
        type: 'node class',
        sub: item.package ? item.package.name : null
      },
      meta: [
        item.package ? item.package.name : null,
        outputs.length ? outputs.join(', ') : null
      ].filter(Boolean),
      badges: [matchBadge(item)].filter(Boolean),
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
      face: {
        Icon: Workflow, cls: 'default',
        bar: item.missing || broken ? 'danger' : 'ok',
        type: 'workflow',
        sub: item.base_model && item.base_model !== 'Unknown' ? item.base_model : null
      },
      meta: [
        (item.counts && item.counts.nodes) + ' nodes',
        item.base_model && item.base_model !== 'Unknown' ? item.base_model : null,
        broken ? (missingNodes + missingModels) + ' missing' : 'runnable'
      ].filter(Boolean),
      badges: [
        item.base_model && item.base_model !== 'Unknown'
          ? { key: 'base', node: <Badge tone="base" overlay>{item.base_model}</Badge> }
          : null,
        matchBadge(item)
      ].filter(Boolean),
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
    face: {
      Icon: MediaIcon, cls: 'default',
      bar: item.missing ? 'danger' : 'neutral',
      type: item.media_kind || 'output',
      sub: null
    },
    badges: [
      item.media_kind !== 'image'
        ? { key: 'kind', node: <Badge tone="media" overlay>{item.media_kind}</Badge> }
        : null,
      matchBadge(item)
    ].filter(Boolean),
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

export function CardFace({ face, small, badges, corner }) {
  const Icon = (face && face.Icon) || Box
  const chips = [...(badges || []), ...(corner || [])]
  return (
    <div className={'gp-face gp-face--cat-' + ((face && face.cls) || 'default')
      + (small ? ' gp-face--small' : '')}>
      <span className={'gp-face__bar'
        + (face && face.bar && face.bar !== 'ok' ? ' gp-face__bar--' + face.bar : '')} />
      <span className="gp-face__icon"><Icon aria-hidden="true" /></span>
      {!small && face && face.type ? <span className="gp-face__type">{face.type}</span> : null}
      {!small && face && face.sub && face.sub !== face.type
        ? <span className="gp-face__sub">{face.sub}</span> : null}
      {!small && chips.length ? (
        <div className="gp-face__badges">
          {chips.map((b) => <React.Fragment key={b.key}>{b.node}</React.Fragment>)}
        </div>
      ) : null}
    </div>
  )
}

function Thumb({ uid, tier, alt, placeholderIcon: Icon, inferred, noThumb, face }) {
  const [failed, setFailed] = useState(noThumb === true)
  const onError = useCallback(() => setFailed(true), [])
  if (failed) {
    if (face) return <CardFace face={face} />
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
    item, scope, tier, selected, onOpen, onSelect, onContextMenu, checked,
    onToggleCheck, onOpenExternal
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
        {p.noThumb && p.face ? (
          /* Face cards keep every label in flow, below the bar, checkbox and
             corner icon — nothing overlays the text, so nothing gets cut. */
          <CardFace face={p.face} badges={p.badges} corner={p.corner} />
        ) : (
          <>
            <Thumb
              uid={p.uid}
              tier={tier}
              alt=""
              placeholderIcon={p.icon}
              inferred={p.inferred}
              noThumb={p.noThumb}
              face={p.face}
            />
            {p.badges.length || p.corner.length ? (
              <div className="gp-card__top">
                <div className="gp-card__badges">
                  {p.badges.map((b) => <React.Fragment key={b.key}>{b.node}</React.Fragment>)}
                </div>
                <div className="gp-card__corner">
                  {p.corner.map((b) => <React.Fragment key={b.key}>{b.node}</React.Fragment>)}
                </div>
              </div>
            ) : null}
          </>
        )}
      </button>
      {isPlayable(item.media_kind) ? (
        <InlinePlayer
          uid={p.uid}
          mediaKind={item.media_kind}
          label={'Play ' + p.title}
        />
      ) : null}
      {/* Sits over the thumbnail rather than inside it: the thumbnail is
          itself a button, and a button inside a button is not a control. */}
      {onOpenExternal ? (
        <button
          type="button"
          className="gp-card__action gp-btn gp-btn--ghost gp-btn--sm gp-btn--icon"
          aria-label={'Open ' + p.title + ' in ComfyUI'}
          title="Open in ComfyUI"
          onClick={(e) => { e.stopPropagation(); onOpenExternal(item) }}
        >
          <ExternalLink className="gp-btn__icon" aria-hidden="true" />
        </button>
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
          {p.stars}
        </div>
      </div>
    </article>
  )
}

export default React.memo(AssetCard)
