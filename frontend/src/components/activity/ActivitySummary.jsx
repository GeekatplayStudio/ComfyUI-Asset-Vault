import React, { useMemo } from 'react'
import { ShieldCheck, AlertTriangle } from 'lucide-react'
import Badge from '../common/Badge.jsx'
import { count as fmtCount, dateTime, ago } from '../../services/format.js'

/*
 * ActivitySummary - the headline layer of Settings -> Activity (C11).
 *
 * The question this answers at a glance is "has anything outside this app
 * changed my library, and did any of it fail?". Order is deliberate:
 *   1. three headline figures - calls recorded, items touched, failures;
 *   2. one primary visual - the stacked bar of destructive / other writes,
 *      which is where an unexpected pile of deletes becomes obvious;
 *   3. the per-tool table, which carries the detail.
 *
 * Everything in 1-3 is measured: a counted row, a summed column, a recorded
 * timestamp, so the whole block is amber-side (DECISIONS C4). The one violet
 * "~" statement is the quiet/active judgement, which is a reading of the
 * present from a past timestamp and not a fact the log holds.
 */

/* Fixed .gp-meter slots. Never reassigned by rank, so "destructive" is the
   same colour whether it is the largest slice or absent. */
const SEG = { destructive: 1, write: 2, unknown: 3, read: 5 }

const KIND_LABEL = {
  destructive: 'Deletes, moves and renames',
  write: 'Other changes',
  read: 'Reads',
  unknown: 'Unrecognised tools'
}

/** How long without a call before the log reads as quiet rather than active. */
const QUIET_MS = 7 * 24 * 60 * 60 * 1000

function StatTile({ label, value, sub, tone, title }) {
  return (
    <div className="gp-panel gp-u-grow gp-u-minw0" title={title}>
      <div className="gp-u-fs-10 gp-u-caps gp-u-meta">{label}</div>
      <div className={'gp-u-fs-18 gp-u-num gp-u-fw-600 gp-u-mt-4' +
        (tone ? ' gp-u-' + tone : '')}
      >
        {value}
      </div>
      {sub ? <div className="gp-u-fs-11 gp-u-meta gp-u-mt-4">{sub}</div> : null}
    </div>
  )
}

export default function ActivitySummary({ summary, onFilterOutcome, onFilterTool,
  activeOutcome, activeTool }) {
  const byKind = (summary && summary.by_kind) || {}
  const byOutcome = (summary && summary.by_outcome) || {}
  const byTransport = (summary && summary.by_transport) || {}
  const tools = (summary && summary.by_tool) || []
  const total = (summary && summary.total) || 0

  const segments = useMemo(() => ['destructive', 'write', 'unknown', 'read']
    .map((kind) => ({ kind, count: byKind[kind] || 0 }))
    .filter((s) => s.count > 0), [byKind])

  const shown = segments.reduce((a, s) => a + s.count, 0) || 1
  const failures = byOutcome.error || 0
  const partial = byOutcome.partial || 0

  /* Inferred, and marked as such: the log records when calls happened, not
     whether a client is connected right now. */
  const quiet = !summary || !summary.last_ts || (Date.now() - summary.last_ts) > QUIET_MS

  return (
    <>
      <div className="gp-u-row gp-u-gap-4 gp-u-wrap">
        <StatTile
          label="Calls recorded"
          value={fmtCount(total)}
          sub={summary && summary.filtered
            ? 'of ' + fmtCount(summary.vault_total) + ' in the log'
            : fmtCount(summary ? summary.sessions : 0) + ' client sessions'}
          title="Every mutating tool call an external MCP client has made, counted from the log."
        />
        <StatTile
          label="Items touched"
          value={fmtCount(summary ? summary.affected : 0)}
          sub="models, workflows and outputs"
          title="Summed from the affected count each call recorded."
        />
        <StatTile
          label="Failed calls"
          value={fmtCount(failures)}
          tone={failures ? 'danger' : undefined}
          sub={partial ? fmtCount(partial) + ' partly applied' : 'refused or errored'}
          title="Calls whose outcome was recorded as 'error'. Nothing was changed by these."
        />
      </div>

      {/* ------------------------------------------------- the primary visual */}
      {total > 0 ? (
        <div className="gp-panel gp-u-mt-5">
          <div className="gp-u-row gp-u-between gp-u-gap-4">
            <span className="gp-u-fs-11 gp-u-fw-600">What was called</span>
            <span className="gp-u-fs-10 gp-u-meta gp-u-num">
              {byTransport.http ? fmtCount(byTransport.http) + ' over HTTP' : null}
              {byTransport.http && byTransport.stdio ? ' / ' : null}
              {byTransport.stdio ? fmtCount(byTransport.stdio) + ' over stdio' : null}
            </span>
          </div>
          <div
            className="gp-meter gp-u-mt-5"
            role="img"
            aria-label={segments
              .map((s) => KIND_LABEL[s.kind] + ' ' + fmtCount(s.count))
              .join(', ')}
          >
            {segments.map((s) => (
              <span
                key={s.kind}
                className={'gp-meter__seg gp-meter__seg--' + SEG[s.kind]}
                style={{ width: ((s.count / shown) * 100).toFixed(2) + '%' }}
                title={KIND_LABEL[s.kind] + ' - ' + fmtCount(s.count) + ' calls'}
              />
            ))}
          </div>
          <div className="gp-meter-legend">
            {segments.map((s) => (
              <span className="gp-meter-legend__item" key={'legend:' + s.kind}>
                <span className={'gp-meter-legend__swatch gp-meter-legend__swatch--' +
                  SEG[s.kind]}
                />
                {KIND_LABEL[s.kind]} {fmtCount(s.count)}
              </span>
            ))}
          </div>

          <div className="gp-u-row gp-u-gap-4 gp-u-wrap gp-u-mt-5 gp-u-fs-11">
            <span className="gp-u-row gp-u-gap-3">
              {failures
                ? <AlertTriangle size={13} aria-hidden="true" className="gp-u-danger" />
                : <ShieldCheck size={13} aria-hidden="true" className="gp-u-ok" />}
              <span className="gp-u-meta">
                {fmtCount(byOutcome.ok || 0)} succeeded, {fmtCount(failures)} failed
              </span>
            </span>
            {summary && summary.last_ts ? (
              <span className="gp-u-meta">
                Last call{' '}
                <span className="gp-u-local gp-u-num" title={dateTime(summary.last_ts)}>
                  {ago(summary.last_ts)}
                </span>
                {summary.first_ts ? (
                  <>
                    {', first '}
                    <span className="gp-u-num" title={dateTime(summary.first_ts)}>
                      {ago(summary.first_ts)}
                    </span>
                  </>
                ) : null}
              </span>
            ) : null}
            <span
              className="gp-inferred gp-u-fs-11"
              title={quiet
                ? 'Inferred from the newest timestamp in the log, not from a live connection: no MCP client has changed anything in the last 7 days.'
                : 'Inferred from the newest timestamp in the log, not from a live connection: a client acted within the last 7 days.'}
            >
              {quiet ? 'quiet — nothing changed recently' : 'recently active'}
            </span>
          </div>
        </div>
      ) : null}

      {/* ------------------------------------------------------- the detail */}
      {tools.length ? (
        <>
          <div className="gp-group-head gp-u-mt-6">
            <span className="gp-group-head__label">By tool</span>
            <span className="gp-group-head__count">{fmtCount(tools.length)}</span>
            <span className="gp-group-head__rule" />
            <span className="gp-u-fs-10 gp-u-meta">click a row to filter the list</span>
          </div>
          <table className="gp-table gp-table--compact">
            <thead>
              <tr>
                <th>Tool</th>
                <th>What it does</th>
                <th className="gp-table__num">Calls</th>
                <th className="gp-table__num">Failed</th>
                <th className="gp-table__num">Items</th>
                <th className="gp-table__num">Last</th>
              </tr>
            </thead>
            <tbody>
              {tools.map((t) => (
                <tr
                  key={t.tool}
                  className={activeTool === t.tool ? 'gp-u-bg-raised' : undefined}
                  onClick={() => onFilterTool(activeTool === t.tool ? null : t.tool)}
                >
                  <td className="gp-u-break-all gp-u-pointer">{t.tool}</td>
                  <td>
                    {t.kind === 'unknown' ? (
                      <span className="gp-inferred"
                        title="This tool is not in the current tool catalogue, so what it changed cannot be stated from here."
                      >
                        unrecognised
                      </span>
                    ) : (
                      <Badge tone={t.destructive ? 'danger' : (t.mutating ? 'brand' : 'neutral')}>
                        {t.destructive ? 'destructive' : (t.mutating ? 'writes' : 'reads')}
                      </Badge>
                    )}
                  </td>
                  <td className="gp-table__num">{fmtCount(t.count)}</td>
                  <td className={'gp-table__num' + (t.errors ? ' gp-u-danger' : '')}>
                    {t.errors ? fmtCount(t.errors) : '-'}
                  </td>
                  <td className="gp-table__num">{fmtCount(t.affected)}</td>
                  <td className="gp-table__num" title={dateTime(t.last_ts)}>
                    {ago(t.last_ts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {summary && summary.by_tool_truncated ? (
            <p className="gp-u-fs-10 gp-u-meta gp-u-mt-4">
              Only the busiest tools are listed; filter the log below to see the rest.
            </p>
          ) : null}
          {failures && !activeOutcome ? (
            <div className="gp-u-mt-4">
              <button
                type="button"
                className="gp-btn gp-btn--sm gp-btn--danger-ghost"
                onClick={() => onFilterOutcome('error')}
              >
                <span className="gp-btn__label">
                  Show the {fmtCount(failures)} failed call{failures === 1 ? '' : 's'}
                </span>
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </>
  )
}
