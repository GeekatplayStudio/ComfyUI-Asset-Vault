import React from 'react'
import { ChevronDown } from 'lucide-react'

/** Select - .gp-selectwrap + .gp-select + the caret element. */
export default function Select(props) {
  const { value, onChange, options, bare, invalid, ariaLabel, id, disabled, title } = props
  const classes = ['gp-select']
  if (bare) classes.push('gp-select--bare')
  if (invalid) classes.push('gp-select--invalid')
  return (
    <span className="gp-selectwrap">
      <select
        id={id}
        className={classes.join(' ')}
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-invalid={invalid ? 'true' : undefined}
        title={title}
        onChange={(e) => onChange && onChange(e.target.value)}
      >
        {options.map((opt) => (
          <option key={String(opt.value)} value={opt.value}>{opt.label}</option>
        ))}
      </select>
      <ChevronDown className="gp-selectwrap__caret" aria-hidden="true" />
    </span>
  )
}
