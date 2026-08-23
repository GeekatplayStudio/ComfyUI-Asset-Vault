import React from 'react'
import { X } from 'lucide-react'
import Badge from '../common/Badge.jsx'
import Chip from '../common/Chip.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { KindBadge } from './ActivityRow.jsx'
import { count as fmtCount, dateTime, shortDuration } from '../../services/format.js'

/*
 * ActivityDetail - the "on demand" half of C11, opened by clicking a row.
 *
 * The arguments are the point of the whole log: rail 3 of DECISIONS C5 records
 * argument VALUES for a mutation precisely so the owner can see WHAT an external
 * client asked for, not merely that it asked. They are therefore printed in
 * full, verbatim, and never summarised away.
 */

export default function ActivityDetail({ entry, onClose, onFilterSession }) {
  if (!entry) return null
  const failed = entry.outcome === 'error'
  const args = entry.arguments && Object.keys(entry.arguments).length
    ? entry.arguments
    : null

  return (
    <div className={'gp-panel gp-u-mt-5' + (failed ? ' gp-callout--danger' : '')}>
      <div className="gp-u-row gp-u-between gp-u-gap-4">
        <span className="gp-u-row gp-u-gap-3 gp-u-minw0">
          <span className="gp-u-fw-600 gp-u-num gp-u-truncate">{entry.tool}</span>
          <KindBadge entry={entry} />
          {failed ? (
            <Badge tone="danger">{entry.error_code || 'failed'}</Badge>
          ) : entry.outcome === 'partial' ? (
            <Badge tone="warn">partial</Badge>
          ) : (
            <Badge tone="ok">applied</Badge>
          )}
        </span>
        <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm gp-btn--icon"
          aria-label="Close this entry" onClick={onClose}
        >
          <X className="gp-btn__icon" aria-hidden="true" />
        </button>
      </div>

      {entry.title ? (
        <p className="gp-u-fs-11 gp-u-meta gp-u-mt-4">{entry.title}</p>
      ) : null}

      <div className="gp-meta gp-u-mt-5">
        <MetaRow label="when" value={dateTime(entry.ts)} num />
        <MetaRow label="outcome"
          value={failed ? 'failed — nothing was changed' : entry.outcome}
          tone={failed ? 'danger' : 'ok'} />
        <MetaRow label="items changed" value={fmtCount(entry.affected)} num />
        <MetaRow label="took" value={shortDuration(entry.elapsed_ms)} num />
        <MetaRow label="transport"
          value={entry.transport === 'stdio'
            ? 'stdio (a local MCP client process)'
            : 'http (a client on this machine)'} />
        <MetaRow label="session" value={entry.session_id} num
          empty="No session was recorded for this call." />
        <MetaRow label="entry" value={'#' + entry.id} num
          title="The audit row id. Rows are appended and never rewritten." />
      </div>

      {entry.session_id ? (
        <div className="gp-u-mt-4">
          <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm"
            onClick={() => onFilterSession(entry.session_id)}
          >
            <span className="gp-btn__label">Show everything this session did</span>
          </button>
        </div>
      ) : null}

      <div className="gp-details__section-head gp-u-mt-5">
        <span>Arguments it was given</span>
      </div>
      {args ? (
        <pre className="gp-code gp-u-fs-10 gp-u-break" style={{ maxHeight: 180 }}>
          {JSON.stringify(args, null, 2)}
        </pre>
      ) : (
        <p className="gp-u-fs-11 gp-u-meta">This call took no arguments.</p>
      )}

      {entry.uids && entry.uids.length ? (
        <>
          <div className="gp-details__section-head gp-u-mt-5">
            <span>Items it named</span>
            <Badge tone="neutral">{fmtCount(entry.uids.length)}</Badge>
          </div>
          <div className="gp-u-row gp-u-gap-3 gp-u-wrap">
            {entry.uids.slice(0, 60).map((uid) => (
              <Chip key={uid} label={uid} mono small />
            ))}
            {entry.uids.length > 60 ? (
              <span className="gp-u-fs-10 gp-u-meta">
                and {fmtCount(entry.uids.length - 60)} more
              </span>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  )
}
