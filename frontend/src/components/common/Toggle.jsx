import React from 'react'

/*
 * Toggle - .gp-toggle. The `ai` variant paints the on-state violet and is
 * reserved for inference-backed switches (Smart search, local summaries).
 *
 * When Smart search is unavailable the toggle renders disabled with the
 * server's `smart_reason` as its title. Per DECISIONS C2 that is never a toast.
 */
export default function Toggle(props) {
  const { checked, onChange, label, ai, disabled, title, id } = props
  const classes = ['gp-toggle']
  if (ai) classes.push('gp-toggle--ai')
  if (disabled) classes.push('gp-toggle--disabled')
  return (
    <label className={classes.join(' ')} title={title} htmlFor={id}>
      <input
        id={id}
        className="gp-toggle__input"
        type="checkbox"
        checked={Boolean(checked)}
        disabled={Boolean(disabled)}
        onChange={(e) => onChange && onChange(e.target.checked)}
      />
      <span className="gp-toggle__track"><span className="gp-toggle__thumb" /></span>
      {label ? <span className="gp-toggle__label">{label}</span> : null}
    </label>
  )
}
