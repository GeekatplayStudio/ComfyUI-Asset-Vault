import { useEffect, useRef, useState } from 'react'

/*
 * useEventSource - subscribe to one of the documented SSE endpoints and fall
 * back to 2 s polling once the stream has failed twice (ARCHITECTURE 2.3).
 *
 * The caller supplies the named events it cares about; anything else is ignored.
 */

export default function useEventSource(url, options) {
  const opts = options || {}
  const {
    enabled = true,
    events = [],
    onEvent,
    poll,
    pollInterval = 2000
  } = opts

  const [transport, setTransport] = useState('idle')
  const handlerRef = useRef(onEvent)
  handlerRef.current = onEvent
  const pollRef = useRef(poll)
  pollRef.current = poll

  useEffect(() => {
    if (!enabled || !url) {
      setTransport('idle')
      return undefined
    }

    let source = null
    let pollTimer = null
    let failures = 0
    let disposed = false

    const emit = (name, raw) => {
      if (disposed) return
      let payload = raw
      if (typeof raw === 'string') {
        try { payload = JSON.parse(raw) } catch (err) { payload = { raw } }
      }
      if (handlerRef.current) handlerRef.current(name, payload)
    }

    const startPolling = () => {
      if (disposed || pollTimer || !pollRef.current) return
      setTransport('poll')
      const tick = async () => {
        if (disposed) return
        try {
          const data = await pollRef.current()
          if (!disposed && data) emit('poll', data)
        } catch (err) { /* a poll miss is not fatal; the next tick retries */ }
      }
      tick()
      pollTimer = setInterval(tick, pollInterval)
    }

    const connect = () => {
      if (disposed) return
      try {
        source = new EventSource(url)
      } catch (err) {
        startPolling()
        return
      }
      source.onopen = () => {
        if (disposed) return
        failures = 0
        setTransport('stream')
      }
      source.onerror = () => {
        if (disposed) return
        failures += 1
        if (source) { source.close(); source = null }
        if (failures >= 2) {
          startPolling()
        } else {
          setTimeout(connect, 800)
        }
      }
      source.onmessage = (evt) => emit('message', evt.data)
      for (const name of events) {
        source.addEventListener(name, (evt) => emit(name, evt.data))
      }
    }

    connect()

    return () => {
      disposed = true
      if (source) source.close()
      if (pollTimer) clearInterval(pollTimer)
      setTransport('idle')
    }
    // events is a literal array from the caller; join it so the identity is stable
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, enabled, pollInterval, events.join(',')])

  return transport
}
