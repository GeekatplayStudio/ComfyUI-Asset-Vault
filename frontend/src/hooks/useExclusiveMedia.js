import { useEffect } from 'react'

/**
 * One thing plays at a time, anywhere in the app.
 *
 * Mounted once, at the shell.  It listens for `play` in the CAPTURE phase --
 * media events do not bubble, so a listener on `document` only sees them on
 * the way down -- and pauses every other <video>/<audio> on the page.
 *
 * Doing it globally rather than per-view is deliberate: a grid tile, a list
 * row, the details preview and the lightbox can all hold a player, and they
 * know nothing about each other.  A shared coordinator here means none of them
 * has to, and a player added later is covered for free.
 */
export default function useExclusiveMedia() {
  useEffect(() => {
    const onPlay = (event) => {
      const started = event.target
      if (!started || !('pause' in started)) return
      for (const el of document.querySelectorAll('video, audio')) {
        if (el !== started && !el.paused) {
          try {
            el.pause()
          } catch {
            /* a detached element can throw; nothing to do about it */
          }
        }
      }
    }
    document.addEventListener('play', onPlay, true)
    return () => document.removeEventListener('play', onPlay, true)
  }, [])
}
