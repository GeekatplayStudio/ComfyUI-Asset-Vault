import React, { useCallback, useEffect, useState } from 'react'
import { Star, X } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'

/*
 * Annotations - the user's own metadata on an asset: star rating, colour
 * label, tags and notes. One PATCH per change through the shared AssetPatch
 * contract; models and outputs use identical shapes, so both panels compose
 * from here and can never drift apart.
 */

/** Interactive five-star control. Clicking the current value clears it. */
export function StarRating({ value, onChange, label = 'Rating' }) {
  const current = Number(value) || 0
  return (
    <span className="gp-rating" role="radiogroup" aria-label={label}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          role="radio"
          aria-checked={current === n}
          aria-label={n + (n === 1 ? ' star' : ' stars')}
          className={'gp-rating__star' + (n <= current ? ' gp-rating__star--on' : '')}
          title={n <= current && current === n ? 'Clear the rating' : n + ' of 5'}
          onClick={() => onChange(current === n ? 0 : n)}
        >
          <Star aria-hidden="true" fill={n <= current ? 'currentColor' : 'none'} />
        </button>
      ))}
    </span>
  )
}

/** The label palette mirrors the app's own status hues, plus none. */
const LABELS = ['amber', 'violet', 'green', 'blue', 'red']

export function ColorLabelPicker({ value, onChange }) {
  return (
    <span className="gp-labelpick" role="radiogroup" aria-label="Colour label">
      {LABELS.map((name) => (
        <button
          key={name}
          type="button"
          role="radio"
          aria-checked={value === name}
          aria-label={name + ' label'}
          title={value === name ? 'Clear the ' + name + ' label' : name}
          className={'gp-labelpick__swatch gp-labelpick__swatch--' + name
            + (value === name ? ' gp-labelpick__swatch--on' : '')}
          onClick={() => onChange(value === name ? null : name)}
        />
      ))}
    </span>
  )
}

/**
 * Tag chips with an add field. Sends the complete resulting list, which the
 * PATCH contract treats as authoritative - no add/remove race to manage.
 */
export function TagEditor({ tags, onChange }) {
  const [draft, setDraft] = useState('')
  const known = useResource('tags:suggest', (s) => api.tags({ limit: 200 }, s), {})
  const current = tags || []

  const add = useCallback(() => {
    const name = draft.trim()
    if (!name) return
    setDraft('')
    if (current.some((t) => t.toLowerCase() === name.toLowerCase())) return
    onChange([...current, name])
  }, [draft, current, onChange])

  const remove = useCallback((name) => {
    onChange(current.filter((t) => t !== name))
  }, [current, onChange])

  const suggestions = ((known.data && known.data.items) || [])
    .map((t) => t.name)
    .filter((n) => !current.some((t) => t.toLowerCase() === n.toLowerCase()))

  return (
    <div className="gp-tagedit">
      {current.map((name) => (
        <span key={name} className="gp-tagedit__chip">
          {name}
          <button type="button" className="gp-tagedit__remove"
            aria-label={'Remove the tag ' + name}
            onClick={() => remove(name)}>
            <X aria-hidden="true" />
          </button>
        </span>
      ))}
      <input
        className="gp-input gp-tagedit__input"
        value={draft}
        list="gp-tag-suggestions"
        placeholder={current.length ? 'Add tag' : 'Add a tag'}
        aria-label="Add a tag"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); add() }
          if (e.key === 'Backspace' && !draft && current.length) remove(current[current.length - 1])
        }}
        onBlur={add}
      />
      <datalist id="gp-tag-suggestions">
        {suggestions.map((n) => <option key={n} value={n} />)}
      </datalist>
    </div>
  )
}

/** Free-text notes, saved when focus leaves the field and the text changed. */
export function NotesEditor({ value, onSave }) {
  const [draft, setDraft] = useState(value || '')
  useEffect(() => { setDraft(value || '') }, [value])
  return (
    <textarea
      className="gp-input gp-notes"
      value={draft}
      rows={3}
      maxLength={20000}
      placeholder="Notes only you will see - trigger words that worked, settings, warnings."
      aria-label="Your notes"
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => { if (draft !== (value || '')) onSave(draft) }}
    />
  )
}

/**
 * The composed block both details panels mount. `item` is a model or output
 * detail record; `onPatch(fields)` applies one AssetPatch and refreshes.
 */
export default function Annotations({ item, onPatch }) {
  return (
    <div className="gp-annotations">
      <div className="gp-u-row gp-u-gap-5 gp-u-wrap">
        <StarRating value={item.user_rating}
          onChange={(n) => onPatch({ user_rating: n })} />
        <ColorLabelPicker value={item.color_label}
          onChange={(c) => onPatch({ color_label: c })} />
      </div>
      <TagEditor tags={item.tags} onChange={(tags) => onPatch({ tags })} />
      <NotesEditor value={item.user_notes} onSave={(text) => onPatch({ user_notes: text })} />
    </div>
  )
}
