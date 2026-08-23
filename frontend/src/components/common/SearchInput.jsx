import React, { useState, useEffect, useRef, useCallback } from 'react'
import { Search, X } from 'lucide-react'
import api, { isAbort } from '../../services/api.js'
import useDebounced from '../../hooks/useDebounced.js'

/*
 * SearchInput - 140 ms debounce, AbortController cancellation, prefix-served
 * type-ahead from /search/suggest. "/" focuses it from anywhere in the app.
 */
export default function SearchInput(props) {
  const { value, onChange, onPick, placeholder, inputRef, busy } = props
  const [open, setOpen] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  const [active, setActive] = useState(-1)
  const wrapRef = useRef(null)
  const debounced = useDebounced(value, 140)

  useEffect(() => {
    if (!debounced || debounced.length < 2 || !open) {
      setSuggestions([])
      return undefined
    }
    const controller = new AbortController()
    let alive = true
    api.suggest(debounced, 8, controller.signal)
      .then((res) => {
        if (!alive) return
        setSuggestions((res && res.suggestions) || [])
        setActive(-1)
      })
      .catch((err) => { if (!isAbort(err) && alive) setSuggestions([]) })
    return () => { alive = false; controller.abort() }
  }, [debounced, open])

  useEffect(() => {
    const onDocDown = (event) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    return () => document.removeEventListener('mousedown', onDocDown)
  }, [])

  const choose = useCallback((item) => {
    setOpen(false)
    setSuggestions([])
    if (item && item.uid && onPick) onPick(item)
    else if (item) onChange(item.text)
  }, [onChange, onPick])

  const onKeyDown = (event) => {
    if (!suggestions.length) {
      if (event.key === 'Escape') { event.currentTarget.blur(); setOpen(false) }
      return
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((i) => (i + 1) % suggestions.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((i) => (i <= 0 ? suggestions.length - 1 : i - 1))
    } else if (event.key === 'Enter' && active >= 0) {
      event.preventDefault()
      choose(suggestions[active])
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
    }
  }

  return (
    <div className={'gp-search' + (busy ? ' gp-search--busy' : '')} ref={wrapRef}>
      <Search className="gp-search__icon" aria-hidden="true" />
      <input
        ref={inputRef}
        className="gp-search__input"
        type="search"
        value={value}
        placeholder={placeholder || 'Search the vault'}
        aria-label="Search"
        autoComplete="off"
        spellCheck="false"
        onChange={(e) => { onChange(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {value ? (
        <button
          type="button"
          className="gp-search__clear"
          aria-label="Clear search"
          onClick={() => { onChange(''); setSuggestions([]); setOpen(false) }}
        >
          <X size={12} aria-hidden="true" />
        </button>
      ) : (
        <span className="gp-search__hint"><kbd className="gp-kbd">/</kbd></span>
      )}

      {open && suggestions.length > 0 ? (
        <div className="gp-suggest" role="listbox" aria-label="Search suggestions">
          {suggestions.map((item, i) => (
            <button
              key={(item.uid || item.field || 'q') + ':' + item.text + ':' + i}
              type="button"
              role="option"
              aria-selected={i === active}
              className={'gp-suggest__item' + (i === active ? ' gp-suggest__item--active' : '')}
              onMouseEnter={() => setActive(i)}
              onClick={() => choose(item)}
            >
              <span className="gp-u-truncate">{item.text}</span>
              <span className="gp-suggest__kind">{item.kind}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
