import React from 'react'
import {
  Image as ImageIcon, Film, MessageSquare, Music, Boxes, Puzzle, SlidersHorizontal,
  Maximize2, Eye, Type, Box, CircleDot, Layers
} from 'lucide-react'
import { bytes as fmtBytes } from '../../services/format.js'

/*
 * parts.jsx - the small shared pieces of the System Advisor.
 *
 * Everything the advisor concludes is an estimate, so every one of these
 * renders through the inferred (violet) convention and carries a title naming
 * where the figure came from (DESIGN_SYSTEM 2). Status still needs a hue the
 * eye can sort by, so a verdict keeps a small status mark beside violet text:
 * identity never rests on colour alone.
 */

export const KIND_ICONS = {
  image: ImageIcon, video: Film, llm: MessageSquare, audio: Music, '3d': Boxes,
  lora: Puzzle, controlnet: SlidersHorizontal, upscale: Maximize2, vision: Eye,
  text_encoder: Type, vae: Box, embedding: CircleDot, other: Layers
}

export const VERDICT_TONE = {
  optimal: 'ok', good: 'ok', max: 'warn', offload: 'info', cpu: 'info',
  base: 'neutral', no: 'danger', unknown: 'neutral'
}

export const VERDICT_HELP = {
  optimal: 'Fits with room to spare.',
  good: 'Fits inside the safe budget.',
  max: 'Runs on the GPU only by using the safety headroom.',
  offload: 'Larger than the GPU: part of it runs from system memory. Slow.',
  cpu: 'No usable GPU: runs on the processor.',
  base: 'An add-on. Its cost is its base model plus a little of its own.',
  no: 'Needs more memory than this machine has.',
  unknown: 'The file size is not published, so nothing can be estimated.'
}

export const SPEED_LABEL = {
  fast: 'Fast', moderate: 'Moderate', slow: 'Slow', very_slow: 'Very slow', none: null
}

export const SOURCE_LABEL = {
  huggingface: 'Hugging Face', civitai: 'Civitai', comfy_manager: 'ComfyUI-Manager',
  openmodeldb: 'OpenModelDB', ollama: 'Ollama', github: 'GitHub', vault: 'Asset Vault'
}

const ESTIMATE = 'Estimated from the file size and this machine\'s memory'

/** A value the advisor worked out. The tilde comes from the design system. */
export function Est({ children, title, className }) {
  return (
    <span className={'gp-inferred' + (className ? ' ' + className : '')}
      title={title || ESTIMATE}>
      {children}
    </span>
  )
}

/** Gigabytes with one decimal: the unit memory is planned in. */
export function gb(value, digits) {
  if (value === null || value === undefined) return '--'
  const n = value / 1073741824
  return n.toFixed(digits === undefined ? (n >= 100 ? 0 : 1) : digits) + ' GB'
}

export function FitBadge({ verdict, label, speed, title, large }) {
  const tone = VERDICT_TONE[verdict] || 'neutral'
  const speedText = SPEED_LABEL[speed]
  return (
    <span
      className={'gp-fit gp-fit--' + tone + (large ? ' gp-fit--lg' : '')}
      title={(title || VERDICT_HELP[verdict] || '') + ' ' + ESTIMATE + '.'}
    >
      <span className="gp-fit__dot" aria-hidden="true" />
      <span className="gp-inferred gp-inferred--nomark gp-fit__label">
        {label || verdict}
      </span>
      {speedText ? <span className="gp-fit__speed">{speedText}</span> : null}
    </span>
  )
}

/** A thin bar: how much of the ceiling a model would take. */
export function NeedBar({ pct, safePct, verdict }) {
  if (pct === null || pct === undefined) return <span className="gp-needbar gp-needbar--empty" />
  const tone = VERDICT_TONE[verdict] || 'neutral'
  return (
    <span className={'gp-needbar gp-needbar--' + tone} aria-hidden="true">
      <span className="gp-needbar__fill" style={{ '--gp-adv-v': Math.min(100, pct) + '%' }} />
      {safePct ? (
        <span className="gp-needbar__safe" style={{ '--gp-adv-x': Math.min(100, safePct) + '%' }} />
      ) : null}
    </span>
  )
}

export function KindIcon({ kind, className }) {
  const Icon = KIND_ICONS[kind] || Layers
  return <Icon className={className || 'gp-kindicon'} aria-hidden="true" />
}

export function compactCount(n) {
  if (n === null || n === undefined) return null
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k'
  return String(n)
}

export function paramsText(n) {
  if (!n) return null
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'B'
  if (n >= 1e6) return Math.round(n / 1e6) + 'M'
  return String(n)
}

export function isoDate(value) {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10)
}

export function rate(bytesPerSec) {
  if (bytesPerSec === null || bytesPerSec === undefined) return '--'
  return fmtBytes(bytesPerSec) + '/s'
}

export function SectionHead({ label, count, aside, sub }) {
  return (
    <div className="gp-group-head gp-adv-head">
      <span className="gp-group-head__label">{label}</span>
      {count !== undefined && count !== null
        ? <span className="gp-group-head__count">{count}</span> : null}
      {sub ? <span className="gp-u-fs-11 gp-u-meta gp-u-truncate">{sub}</span> : null}
      <span className="gp-group-head__rule" />
      {aside}
    </div>
  )
}
