import { useCallback, useRef } from 'react'

/*
 * useResizablePanel - drag (or arrow-key) the rail / details separator.
 * Writes only the panel width, which the shell consumes as a custom property.
 */
export default function useResizablePanel(options) {
  const { value, onChange, min, max, side, onDragChange } = options
  const stateRef = useRef({ startX: 0, startValue: 0 })

  const clamp = useCallback(
    (n) => Math.max(min, Math.min(max, Math.round(n))),
    [min, max]
  )

  const onPointerDown = useCallback((event) => {
    event.preventDefault()
    const target = event.currentTarget
    target.setPointerCapture(event.pointerId)
    stateRef.current = { startX: event.clientX, startValue: value }
    if (onDragChange) onDragChange(true)

    const onMove = (moveEvent) => {
      const delta = moveEvent.clientX - stateRef.current.startX
      const next = side === 'left'
        ? stateRef.current.startValue + delta
        : stateRef.current.startValue - delta
      onChange(clamp(next))
    }
    const onUp = () => {
      target.releasePointerCapture(event.pointerId)
      target.removeEventListener('pointermove', onMove)
      target.removeEventListener('pointerup', onUp)
      target.removeEventListener('pointercancel', onUp)
      if (onDragChange) onDragChange(false)
    }
    target.addEventListener('pointermove', onMove)
    target.addEventListener('pointerup', onUp)
    target.addEventListener('pointercancel', onUp)
  }, [value, onChange, clamp, side, onDragChange])

  const onKeyDown = useCallback((event) => {
    const step = event.shiftKey ? 32 : 8
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      onChange(clamp(side === 'left' ? value - step : value + step))
    } else if (event.key === 'ArrowRight') {
      event.preventDefault()
      onChange(clamp(side === 'left' ? value + step : value - step))
    } else if (event.key === 'Home') {
      event.preventDefault()
      onChange(min)
    } else if (event.key === 'End') {
      event.preventDefault()
      onChange(max)
    }
  }, [value, onChange, clamp, side, min, max])

  return { onPointerDown, onKeyDown }
}
