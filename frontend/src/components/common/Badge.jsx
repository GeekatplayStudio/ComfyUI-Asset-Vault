import React from 'react'
import { enumClass, humanise } from '../../services/format.js'

/*
 * Badge - every frozen enum in API_CONTRACT 16 has one class per value, so the
 * mapping is a template string with no conditional styling anywhere.
 */

export default function Badge(props) {
  const {
    tone, mono, large, overlay, children, className, title, ...rest
  } = props
  const classes = ['gp-badge']
  if (tone) classes.push('gp-badge--' + tone)
  if (mono) classes.push('gp-badge--mono')
  if (large) classes.push('gp-badge--lg')
  if (overlay) classes.push('gp-badge--overlay')
  if (className) classes.push(className)
  return (
    <span className={classes.join(' ')} title={title} {...rest}>{children}</span>
  )
}

/** hash_state: unhashed | queued | hashing | done | failed | stale */
export function HashBadge({ state, overlay, title }) {
  if (!state) return null
  return (
    <Badge tone={'hash-' + enumClass(state)} overlay={overlay}
      title={title || 'Hash state: ' + state}>
      {state}
    </Badge>
  )
}

/** integrity: ok | invalid_header | not_a_model | truncated | unreadable | unsupported_format */
export function IntegrityBadge({ status, overlay, note }) {
  if (!status || status === 'ok') return null
  return (
    <Badge tone={'integrity-' + enumClass(status)} overlay={overlay}
      title={note || 'Integrity: ' + humanise(status)}>
      {humanise(status)}
    </Badge>
  )
}

/** dep_status: satisfied | missing | ambiguous | unknown */
export function DepBadge({ status, title }) {
  return (
    <Badge tone={'dep-' + enumClass(status || 'unknown')} title={title}>
      {status || 'unknown'}
    </Badge>
  )
}

/** confidence: declared | inferred | registry */
export function ConfidenceBadge({ confidence, title }) {
  if (!confidence) return null
  return (
    <Badge tone={'conf-' + enumClass(confidence)} title={title}>
      {confidence}
    </Badge>
  )
}

/** search_mode: lexical | hybrid */
export function ModeBadge({ mode }) {
  if (!mode) return null
  return (
    <Badge tone={'mode-' + enumClass(mode)}
      title={mode === 'hybrid'
        ? 'Hybrid ranking: keyword index fused with vector similarity'
        : 'Keyword index only'}>
      {mode}
    </Badge>
  )
}

/**
 * Base-model badge. Low confidence flips it to the violet inferred treatment
 * with the mandatory "~" marker and a title naming the detection source.
 */
export function BaseModelBadge({ base, overlay }) {
  if (!base) return null
  const family = typeof base === 'string' ? base : base.family
  if (!family || family === 'Unknown') return null
  const confidence = typeof base === 'string' ? null : base.confidence
  const source = typeof base === 'string' ? null : base.source
  const inferred = confidence !== null && confidence !== undefined && confidence < 0.7
  const title = inferred
    ? 'Inferred base model - source ' + (source || 'unknown') +
      ', confidence ' + Number(confidence).toFixed(2)
    : 'Base model from ' + (source || 'file metadata')
  return (
    <Badge tone="base" overlay={overlay} className={inferred ? 'gp-badge--ai' : undefined}
      title={title}>
      {inferred ? <span className="gp-inferred">{family}</span> : family}
    </Badge>
  )
}
