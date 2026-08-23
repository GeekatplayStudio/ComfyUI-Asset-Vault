import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Play, X } from 'lucide-react'
import { rawUrl } from '../../services/api.js'

/** Media kinds that have something to play. */
const PLAYABLE = new Set(['video', 'audio'])

export function isPlayable(mediaKind) {
  return PLAYABLE.has(mediaKind)
}

/**
 * A play button that turns into a real player in place.
 *
 * The poster stays mounted underneath until the user asks for playback, so a
 * grid of several thousand outputs never holds thousands of media elements --
 * only the one being watched.  Exclusivity across the whole app is handled by
 * `useExclusiveMedia` at the shell, not here: this component only has to stop
 * showing itself as active when its own element pauses or ends.
 */
export default function InlinePlayer({
  uid,
  mediaKind,
  className = '',
  label = 'Play',
  size = 'md',
  onActivate
}) {
  const [active, setActive] = useState(false)
  const ref = useRef(null)

  // Reset when the card is recycled onto a different asset.
  useEffect(() => { setActive(false) }, [uid])

  useEffect(() => {
    if (!active) return undefined
    const el = ref.current
    if (!el) return undefined
    // `ended` folds the player away and hands the poster back.  `pause` does
    // NOT: pause has to stay pause, both for the native control and for the
    // coordinator that pauses this one when something else starts playing.
    // Closing is the stop button's job, and only the stop button's.
    const close = () => setActive(false)
    el.addEventListener('ended', close)
    const play = el.play()
    if (play && typeof play.catch === 'function') {
      play.catch(() => { /* autoplay refused; the controls are still there */ })
    }
    return () => { el.removeEventListener('ended', close) }
  }, [active])

  const start = useCallback((event) => {
    event.stopPropagation()
    event.preventDefault()
    setActive(true)
    if (onActivate) onActivate()
  }, [onActivate])

  if (!isPlayable(mediaKind)) return null

  if (!active) {
    return (
      <button
        type="button"
        className={'gp-play gp-play--' + size + (className ? ' ' + className : '')}
        onClick={start}
        aria-label={label}
        title={label}
      >
        <Play aria-hidden="true" />
      </button>
    )
  }

  const common = {
    ref,
    className: 'gp-play__media',
    src: rawUrl(uid),
    controls: true,
    preload: 'metadata',
    onClick: (e) => e.stopPropagation(),
    onDoubleClick: (e) => e.stopPropagation()
  }

  const stop = (event) => {
    event.stopPropagation()
    event.preventDefault()
    const el = ref.current
    if (el) {
      try {
        el.pause()
        el.currentTime = 0
      } catch { /* a detached element can throw */ }
    }
    setActive(false)
  }

  return (
    <div className="gp-play__stage" onClick={(e) => e.stopPropagation()}>
      {mediaKind === 'audio'
        ? <audio {...common} />
        : <video {...common} playsInline />}
      {/* The native controls pause; this stops -- it rewinds and hands the
          poster back, so the tile returns to how it looked before. */}
      <button
        type="button"
        className="gp-play__stop"
        onClick={stop}
        aria-label="Stop and close the player"
        title="Stop"
      >
        <X aria-hidden="true" />
      </button>
    </div>
  )
}
