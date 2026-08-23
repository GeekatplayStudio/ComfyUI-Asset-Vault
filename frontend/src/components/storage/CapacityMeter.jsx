import React, { useMemo } from 'react'
import { bytes as fmtBytes, percent } from '../../services/format.js'

/*
 * CapacityMeter - the one primary visual for "where did my terabyte go" (C11).
 *
 * Part-to-whole over a fixed total, so it is a horizontal stacked bar: the
 * design system's .gp-meter, whose track is already the empty-space colour.
 * Free space is therefore the UNFILLED track rather than a fifth fill - the
 * reader sees the headroom directly instead of decoding another swatch.
 *
 * Segment order is fixed and never cycled, so a volume with no outputs paints
 * its models the same colour as a volume that has both. The legend always
 * carries the name AND the value, so identity never rests on colour alone.
 *
 * Every number here is measured from the filesystem, so the whole block is
 * amber-side (DECISIONS C4): nothing in it is inferred.
 */

/** Fixed slot -> .gp-meter__seg--N. Never reassigned by rank. */
export const SEG = {
  models: 1,
  outputs: 2,
  comfyui: 4,
  other: 5
}

export default function CapacityMeter(props) {
  const { total, segments, label, freeLabel } = props

  const rows = useMemo(() => (segments || [])
    .filter((s) => s.bytes > 0)
    .map((s) => ({
      ...s,
      share: total > 0 ? (s.bytes / total) * 100 : 0
    })), [segments, total])

  const used = rows.reduce((a, s) => a + s.bytes, 0)
  const free = Math.max(0, total - used)

  const summary = rows
    .map((s) => s.label + ' ' + fmtBytes(s.bytes) + ' (' + percent(s.share) + ')')
    .join(', ')

  return (
    <>
      <div
        className="gp-meter"
        role="img"
        aria-label={(label ? label + '. ' : '') + summary +
          ', free ' + fmtBytes(free) + ' (' + percent(total > 0 ? (free / total) * 100 : 0) + ')'}
      >
        {rows.map((s) => (
          <span
            key={s.key}
            className={'gp-meter__seg gp-meter__seg--' + SEG[s.slot || s.key]}
            style={{ width: percent(s.share, 2) }}
            title={s.label + ' - ' + fmtBytes(s.bytes) + ', ' + percent(s.share) + ' of ' +
              fmtBytes(total)}
          />
        ))}
      </div>
      <div className="gp-meter-legend">
        {rows.map((s) => (
          <span className="gp-meter-legend__item" key={'legend:' + s.key}>
            <span className={'gp-meter-legend__swatch gp-meter-legend__swatch--' +
              SEG[s.slot || s.key]}
            />
            {s.label} {fmtBytes(s.bytes)}
          </span>
        ))}
        <span className="gp-meter-legend__item">
          <span className="gp-meter-legend__swatch gp-u-bg-inset" />
          {freeLabel || 'Free'} {fmtBytes(free)}
        </span>
      </div>
    </>
  )
}
