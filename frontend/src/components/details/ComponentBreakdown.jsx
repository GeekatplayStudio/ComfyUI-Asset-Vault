import React from 'react'
import { params as fmtParams, percent } from '../../services/format.js'

/*
 * ComponentBreakdown - the meter that answers "what is actually inside this
 * file". Every number here is read straight out of the safetensors header, so
 * the whole block is amber-side: measured, not inferred.
 */
export default function ComponentBreakdown({ components }) {
  if (!components || !components.length) return null
  const top = components.slice(0, 5)
  return (
    <>
      <div className="gp-meter" role="img"
        aria-label={'Component split: ' + top.map((c) => c.name).join(', ')}
      >
        {top.map((c, i) => (
          <span
            key={c.name + ':' + i}
            className={'gp-meter__seg gp-meter__seg--' + (i + 1)}
            style={{ width: percent((c.share || 0) * 100) }}
            title={c.name + ' - ' + fmtParams(c.params) + ' parameters, ' +
              percent((c.share || 0) * 100)}
          />
        ))}
      </div>
      <div className="gp-meter-legend">
        {top.map((c, i) => (
          <span className="gp-meter-legend__item" key={'legend:' + c.name + ':' + i}>
            <span className={'gp-meter-legend__swatch gp-meter-legend__swatch--' + (i + 1)} />
            {c.name} {fmtParams(c.params)} {c.dtype ? '/ ' + c.dtype : ''}
          </span>
        ))}
      </div>
      <table className="gp-table gp-table--compact gp-u-mt-5">
        <thead>
          <tr>
            <th>Component</th>
            <th className="gp-table__num">Parameters</th>
            <th className="gp-table__num">Share</th>
            <th>Type</th>
          </tr>
        </thead>
        <tbody>
          {components.map((c, i) => (
            <tr key={'row:' + c.name + ':' + i}>
              <td>{c.name}</td>
              <td className="gp-table__num">{fmtParams(c.params) || '—'}</td>
              <td className="gp-table__num">{percent((c.share || 0) * 100)}</td>
              <td>{c.dtype || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
