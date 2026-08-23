import React from 'react'

/**
 * Slider - .gp-sliderwrap. Used for the grid tile size, hash concurrency and
 * the hash throttle. The tile slider writes a single custom property on the
 * shell; the grid children are not re-rendered by a drag.
 */
export default function Slider(props) {
  const {
    value, min, max, step, onChange, ariaLabel, valueLabel, disabled, title, id
  } = props
  return (
    <span className="gp-sliderwrap" title={title}>
      <input
        id={id}
        className="gp-slider"
        type="range"
        min={min}
        max={max}
        step={step || 1}
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={(e) => onChange && onChange(Number(e.target.value))}
      />
      {valueLabel !== undefined && valueLabel !== null
        ? <span className="gp-sliderwrap__value">{valueLabel}</span>
        : null}
    </span>
  )
}
