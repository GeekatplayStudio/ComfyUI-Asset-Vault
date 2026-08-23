import React from 'react'
import { FileText, AlertTriangle } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'

/*
 * Formatted preview for .txt / .json and anything else that decodes as text.
 *
 * The server decides what counts as text by looking at the bytes, so a `.pt`
 * tensor file comes back as `kind: "binary"` and is reported as such rather
 * than rendered as a screen of mojibake.
 */

/** Media kinds worth trying to read. */
export function isTextual(mediaKind) {
  return mediaKind === 'text' || mediaKind === 'other'
}

function bytes(n) {
  const v = Number(n) || 0
  if (v < 1024) return v + ' B'
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB'
  return (v / 1024 / 1024).toFixed(1) + ' MB'
}

export default function TextPreview({ uid, className = '' }) {
  const { data, error, loading } = useResource(
    uid ? 'text:' + uid : null,
    (signal) => api.textPreview(uid, signal))

  if (loading) {
    return <div className={'gp-text gp-text--msg ' + className}><p>Reading…</p></div>
  }
  if (error) {
    return (
      <div className={'gp-text gp-text--msg ' + className}>
        <AlertTriangle className="gp-text__icon" aria-hidden="true" />
        <p>{error.message || 'Could not read this file.'}</p>
      </div>
    )
  }
  if (!data) return null

  if (data.kind === 'binary') {
    return (
      <div className={'gp-text gp-text--msg ' + className}>
        <FileText className="gp-text__icon" aria-hidden="true" />
        <p>{data.message}</p>
        <p className="gp-text__sub">{bytes(data.total_bytes)} on disk</p>
      </div>
    )
  }

  return (
    <div className={'gp-text ' + className}>
      <div className="gp-text__bar">
        <span className="gp-text__badge">{data.json ? 'JSON' : 'TEXT'}</span>
        <span className="gp-text__meta">
          {data.lines} {data.lines === 1 ? 'line' : 'lines'} · {bytes(data.total_bytes)}
          {data.truncated ? ' · showing the first part only' : ''}
        </span>
      </div>
      <pre className="gp-text__body"><code>{data.text}</code></pre>
      {data.truncated ? (
        <div className="gp-text__note">
          Truncated at {bytes(data.bytes_read)} — open the file for the rest.
        </div>
      ) : null}
    </div>
  )
}
