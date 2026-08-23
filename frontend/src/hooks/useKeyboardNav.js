import { useEffect, useRef } from 'react'

/*
 * useKeyboardNav - the global shortcut map.
 *   /            focus search
 *   Esc          close the topmost overlay / clear the selection
 *   arrows       move through the grid
 *   Enter        open the focused asset
 *   Delete       delete the selection (always via a confirm dialog)
 *   Ctrl+A       select all on the page
 *   F5 / Ctrl+R  reindex
 * Keys are ignored while the user is typing in a field.
 */

function isTypingTarget(target) {
  if (!target) return false
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable
}

export default function useKeyboardNav(handlers, enabled) {
  const ref = useRef(handlers)
  ref.current = handlers

  useEffect(() => {
    if (enabled === false) return undefined
    const onKeyDown = (event) => {
      const h = ref.current || {}
      const typing = isTypingTarget(event.target)

      if (event.key === 'Escape') {
        if (h.onEscape) { h.onEscape(event); }
        return
      }
      if (typing) return

      if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault()
        if (h.onFocusSearch) h.onFocusSearch()
        return
      }
      if ((event.ctrlKey || event.metaKey) && (event.key === 'a' || event.key === 'A')) {
        event.preventDefault()
        if (h.onSelectAll) h.onSelectAll()
        return
      }
      if (event.key === 'F5' || ((event.ctrlKey || event.metaKey) && (event.key === 'r' || event.key === 'R'))) {
        event.preventDefault()
        if (h.onReindex) h.onReindex()
        return
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (h.onDelete) { event.preventDefault(); h.onDelete() }
        return
      }
      if (event.key === 'Enter') {
        if (h.onOpen) { event.preventDefault(); h.onOpen() }
        return
      }
      const moves = {
        ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
        Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown'
      }
      const move = moves[event.key]
      if (move && h.onMove) {
        event.preventDefault()
        h.onMove(move, event)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [enabled])
}
