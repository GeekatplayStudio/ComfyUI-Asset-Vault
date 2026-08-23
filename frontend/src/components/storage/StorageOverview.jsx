import React, { useMemo } from 'react'
import {
  HardDrive, AlertTriangle, ShieldQuestion, Info, RefreshCw, Database
} from 'lucide-react'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import CapacityMeter from './CapacityMeter.jsx'
import ConfidenceMark from './ConfidenceMark.jsx'
import { bytes as fmtBytes, count as fmtCount, percent, humanise } from '../../services/format.js'

/*
 * StorageOverview - the summary layer of C11.
 *
 * Order is deliberate and each layer is one step more detailed than the last:
 *   1. three headline figures - footprint, reclaimable, headroom;
 *   2. one capacity bar per volume, which is the primary visual;
 *   3. the whole-role caution, before any table invites a deletion;
 *   4. tables - what is reclaimable, what the footprint is made of, the roots.
 * Nothing here deletes anything; every row hands off to the Cleanup section.
 */

/* Buckets that are neither models nor outputs are folded into one segment on
   the bar - the ladder caps a stacked bar at ~6 parts and these are all under
   half a percent. The footprint table below carries every bucket separately. */
const BAR_BUCKETS = { models: 'models', outputs: 'outputs' }

function StatTile({ label, value, sub, children, title }) {
  return (
    <div className="gp-panel gp-u-grow gp-u-minw0" title={title}>
      <div className="gp-u-fs-10 gp-u-caps gp-u-meta">{label}</div>
      <div className="gp-u-fs-18 gp-u-num gp-u-fw-600 gp-u-mt-4">{value}</div>
      {sub ? <div className="gp-u-fs-11 gp-u-meta gp-u-mt-4">{sub}</div> : null}
      {children}
    </div>
  )
}

function SectionHead({ label, count, aside }) {
  return (
    <div className="gp-group-head">
      <span className="gp-group-head__label">{label}</span>
      {count !== undefined && count !== null
        ? <span className="gp-group-head__count">{count}</span> : null}
      <span className="gp-group-head__rule" />
      {aside}
    </div>
  )
}

function VolumeBlock({ volume, footprint, primary }) {
  const buckets = (footprint && footprint.buckets) || []
  const segments = []
  let comfyOther = 0

  if (primary) {
    for (const b of buckets) {
      if (BAR_BUCKETS[b.key]) {
        segments.push({ key: b.key, slot: b.key, label: b.label, bytes: b.bytes || 0 })
      } else {
        comfyOther += b.bytes || 0
      }
    }
    if (comfyOther > 0) {
      segments.push({
        key: 'comfyui', slot: 'comfyui', label: 'Inputs, nodes, cache & program',
        bytes: comfyOther
      })
    }
  }

  const accounted = segments.reduce((a, s) => a + s.bytes, 0)
  const otherOnDrive = Math.max(0, (volume.used_bytes || 0) - accounted)
  if (otherOnDrive > 0) {
    segments.push({
      key: 'other', slot: 'other', label: 'Other data on this drive', bytes: otherOnDrive
    })
  }

  const roots = volume.roots || []
  const tone = volume.used_pct >= 90 ? 'danger' : (volume.used_pct >= 80 ? 'warn' : 'ok')

  return (
    <div className="gp-panel gp-u-mb-5">
      <div className="gp-u-row gp-u-between gp-u-gap-5">
        <div className="gp-u-row gp-u-gap-3 gp-u-minw0">
          <HardDrive size={14} aria-hidden="true" className="gp-u-meta gp-u-shrink0" />
          <span className="gp-u-fw-600 gp-u-num">{volume.mount}</span>
          <span className="gp-u-fs-11 gp-u-meta gp-u-truncate">
            {roots.map((r) => r.label).join(', ') || 'no configured root'}
          </span>
          {primary ? <Badge tone="brand">primary</Badge> : null}
        </div>
        <div className="gp-u-row gp-u-gap-3 gp-u-shrink0">
          <span className={'gp-u-num gp-u-fw-600 gp-u-' + tone}>
            {percent(volume.used_pct)}
          </span>
          <span className="gp-u-fs-11 gp-u-meta">full</span>
        </div>
      </div>

      {volume.available === false ? (
        <p className="gp-u-fs-11 gp-u-danger gp-u-mt-5">
          {volume.error || 'This volume did not answer.'}
        </p>
      ) : (
        <>
          <div className="gp-u-mt-5">
            <CapacityMeter
              total={volume.total_bytes}
              segments={segments}
              label={volume.mount + ' capacity'}
            />
          </div>
          <div className="gp-u-fs-11 gp-u-meta gp-u-mt-5 gp-u-num">
            <span className="gp-u-fg gp-u-fw-600">{fmtBytes(volume.free_bytes)}</span>
            {' free of '}{fmtBytes(volume.total_bytes)}
          </div>
        </>
      )}
    </div>
  )
}

export default function StorageOverview(props) {
  const { summary, roots, coverage, loading, error, onRefresh, onReview, staleDays } = props

  const footprint = summary && summary.footprint
  const volumes = (summary && summary.volumes) || []
  const primaryKey = summary && summary.primary_volume && summary.primary_volume.key
  const reclaim = (summary && summary.reclaim) || {}
  const groups = (reclaim.groups || []).filter((g) => g.count > 0)
  const index = (summary && summary.index) || {}

  const ordered = useMemo(() => {
    const list = [...volumes]
    list.sort((a, b) => (a.key === primaryKey ? -1 : 0) - (b.key === primaryKey ? -1 : 0))
    return list
  }, [volumes, primaryKey])

  const modelsBucket = (footprint && footprint.buckets || []).find((b) => b.key === 'models')
  const unusedGroup = (reclaim.groups || []).find((g) => g.key === 'unused_models')
  const modelBytes = (index.models && index.models.bytes) || (modelsBucket && modelsBucket.bytes) || 0
  const unusedBytes = (unusedGroup && unusedGroup.bytes) || 0
  const unusedShare = modelBytes > 0 ? (unusedBytes / modelBytes) * 100 : 0

  if (error) {
    return (
      <EmptyState
        tone="error"
        icon={AlertTriangle}
        title="Could not read the storage summary"
        text={error.message}
        actions={<Button variant="primary" onClick={onRefresh}>Try again</Button>}
      />
    )
  }

  if (loading && !summary) return <SkeletonRows rows={10} />
  if (!summary) return null

  if (!summary.configured) {
    return (
      <EmptyState
        icon={HardDrive}
        title="No ComfyUI installation is configured"
        text="Point the vault at a ComfyUI folder in Settings, then run a scan. The footprint, the reclaim candidates and the updater all read from it."
      />
    )
  }

  const primaryVolume = summary.primary_volume

  return (
    <>
      {/* ---------------------------------------------------- headline figures */}
      <div className="gp-u-row gp-u-gap-5 gp-u-wrap gp-u-mb-6">
        <StatTile
          label="ComfyUI footprint"
          value={fmtBytes(footprint && footprint.total_bytes)}
          sub={fmtCount((footprint && footprint.buckets || []).length) + ' folders on ' +
            summary.comfyui_path}
          title={'Measured from disk in ' + (footprint.elapsed_ms || 0) + ' ms'}
        />
        <StatTile
          label="Reclaimable, unused models"
          value={fmtBytes(unusedBytes)}
          sub={fmtCount(unusedGroup ? unusedGroup.count : 0) +
            ' models referenced by no workflow and no output'}
        >
          {/* Emphasis, not categorical: one part is the point, the rest is context. */}
          <div className="gp-u-mt-4">
            <div className="gp-meter" role="img"
              aria-label={'Unused models are ' + percent(unusedShare) + ' of the model library'}
            >
              <span className="gp-meter__seg gp-meter__seg--1"
                style={{ width: percent(unusedShare, 2) }}
                title={'Unused ' + fmtBytes(unusedBytes)} />
              <span className="gp-meter__seg gp-meter__seg--5"
                style={{ width: percent(100 - unusedShare, 2) }}
                title={'In use ' + fmtBytes(modelBytes - unusedBytes)} />
            </div>
            <div className="gp-u-fs-10 gp-u-meta gp-u-mt-4 gp-u-num">
              {percent(unusedShare)} of the {fmtBytes(modelBytes)} model library
            </div>
          </div>
        </StatTile>
        <StatTile
          label={'Headroom on ' + (primaryVolume ? primaryVolume.mount : '-')}
          value={fmtBytes(primaryVolume && primaryVolume.free_bytes)}
          sub={primaryVolume
            ? percent(primaryVolume.used_pct) + ' of ' + fmtBytes(primaryVolume.total_bytes) +
              ' is used'
            : null}
        />
      </div>

      {/* ------------------------------------------------------------ volumes */}
      <SectionHead
        label="Drives"
        count={fmtCount(ordered.length)}
        aside={(
          <Button size="sm" variant="ghost" icon={RefreshCw} label="Re-measure"
            title="Walk the installation again instead of using the cached footprint"
            onClick={() => onRefresh(true)} />
        )}
      />
      {ordered.map((v) => (
        <VolumeBlock
          key={v.key}
          volume={v}
          footprint={footprint}
          primary={v.key === primaryKey}
        />
      ))}

      {/* ------------------------------------------- the whole-role caution */}
      {coverage.flagged.length ? (
        <div className="gp-callout gp-callout--warn gp-u-mb-6">
          <span className="gp-callout__icon"><ShieldQuestion aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">
              {coverage.flagged.length === 1 ? 'One role is' : coverage.flagged.length + ' roles are'}
              {' '}flagged 100% unused - check these before deleting
            </div>
            <p>
              When <em>every</em> model of a role shows zero references, the likelier
              explanation is that the indexer cannot read that role out of a workflow graph
              than that none of the files is needed. Treat the count below as a question,
              not a verdict.
            </p>
            <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
              {coverage.flagged.map((r) => (
                <Badge key={r.role} tone="warn" mono
                  title={'All ' + r.total + ' ' + humanise(r.role) + ' models show 0 references - ' +
                    fmtBytes(r.bytes)}
                >
                  {humanise(r.role)} {r.unused}/{r.total}
                </Badge>
              ))}
              <span className="gp-u-fs-11 gp-u-meta gp-u-num">
                {fmtCount(coverage.count)} files, {fmtBytes(coverage.bytes)} in total
              </span>
            </div>
            <div className="gp-callout__actions">
              <Button size="sm" label="Review them separately"
                disabled={!coverage.categoryFilterExact}
                title={coverage.categoryFilterExact
                  ? 'Open the cleanup list filtered to these roles'
                  : 'These roles share a folder with other roles, so they cannot be isolated by filter'}
                onClick={() => onReview({ category: coverage.categories, reason: null })} />
            </div>
          </div>
        </div>
      ) : null}

      {/* ------------------------------------------------- reclaim breakdown */}
      <SectionHead label="What could be reclaimed" count={fmtCount(groups.length) + ' groups'} />
      <table className="gp-table gp-u-w-full gp-u-mb-6">
        <thead>
          <tr>
            <th>Group</th>
            <th>Basis</th>
            <th className="gp-table__num">Items</th>
            <th className="gp-table__num">Size</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.key}>
              <td className="gp-u-fg">{g.label}</td>
              <td><ConfidenceMark confidence={g.confidence} reason={g.reason} group={g} /></td>
              <td className="gp-table__num gp-u-num">{fmtCount(g.count)}</td>
              <td className="gp-table__num gp-u-num">{fmtBytes(g.bytes)}</td>
              <td className="gp-table__num">
                {g.reason ? (
                  <Button size="sm" variant="ghost" label="Review"
                    title={'List the ' + fmtCount(g.count) + ' items behind "' + g.label + '"'}
                    onClick={() => onReview({ reason: [g.reason], category: null })} />
                ) : null}
              </td>
            </tr>
          ))}
          {!groups.length ? (
            <tr>
              <td colSpan={5} className="gp-u-meta">
                Nothing is flagged for reclaim at {fmtCount(staleDays)} days.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>

      {/* ------------------------------------------------ footprint by folder */}
      <SectionHead
        label="Footprint by folder"
        count={fmtBytes(footprint && footprint.total_bytes)}
      />
      <table className="gp-table gp-u-w-full gp-u-mb-6">
        <thead>
          <tr>
            <th>Folder</th>
            <th className="gp-table__num">Files</th>
            <th className="gp-table__num">On disk</th>
            <th className="gp-table__num">Indexed</th>
            <th className="gp-table__num">Share</th>
          </tr>
        </thead>
        <tbody>
          {(footprint.buckets || []).map((b) => {
            const share = footprint.total_bytes
              ? (b.bytes / footprint.total_bytes) * 100 : 0
            return (
              <tr key={b.key}>
                <td className="gp-u-fg">
                  {b.label}
                  {b.dirs && b.dirs.length ? (
                    <span className="gp-u-fs-10 gp-u-meta">{' '}{b.dirs.join(', ')}</span>
                  ) : null}
                  {b.truncated ? (
                    <Badge tone="warn" title="The walk hit its file budget; this is a floor.">
                      partial
                    </Badge>
                  ) : null}
                </td>
                <td className="gp-table__num gp-u-num">{fmtCount(b.files)}</td>
                <td className="gp-table__num gp-u-num">{fmtBytes(b.bytes)}</td>
                <td className="gp-table__num gp-u-num"
                  title={b.indexed_bytes === null || b.indexed_bytes === undefined
                    ? 'This folder is not indexed as vault assets'
                    : fmtCount(b.indexed_count) + ' rows in the index'}
                >
                  {b.indexed_bytes === null || b.indexed_bytes === undefined
                    ? <span className="gp-u-dim">not indexed</span>
                    : fmtBytes(b.indexed_bytes)}
                </td>
                <td className="gp-table__num gp-u-num">{percent(share)}</td>
              </tr>
            )
          })}
          {footprint.vault ? (
            <tr>
              <td className="gp-u-fg">
                {footprint.vault.label}
                <span className="gp-u-fs-10 gp-u-meta">
                  {footprint.vault.outside_comfyui ? ' outside ComfyUI' : ''}
                </span>
              </td>
              <td className="gp-table__num gp-u-num">{fmtCount(footprint.vault.files)}</td>
              <td className="gp-table__num gp-u-num">{fmtBytes(footprint.vault.bytes)}</td>
              <td className="gp-table__num gp-u-dim">not counted</td>
              <td className="gp-table__num gp-u-dim">-</td>
            </tr>
          ) : null}
        </tbody>
      </table>
      <div className="gp-callout gp-callout--info gp-u-mb-6">
        <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <strong>On disk</strong> is measured by walking the folder;{' '}
          <strong>indexed</strong> is what the vault holds rows for. The gap is real -
          sidecars, previews, partial downloads and files the scanner skipped - so both
          are shown rather than reconciled.
        </div>
      </div>

      {/* ---------------------------------------------------------- the roots */}
      <SectionHead label="Roots" count={fmtCount((roots && roots.items || []).length)} />
      <table className="gp-table gp-u-w-full">
        <thead>
          <tr>
            <th>Root</th>
            <th>Path</th>
            <th className="gp-table__num">Models</th>
            <th className="gp-table__num">Outputs</th>
            <th className="gp-table__num">Indexed</th>
            <th className="gp-table__num">Free</th>
          </tr>
        </thead>
        <tbody>
          {((roots && roots.items) || []).map((r) => (
            <tr key={r.id}>
              <td className="gp-u-fg">
                {r.label}
                {r.is_default ? <Badge tone="brand">default</Badge> : null}
                {r.retired ? (
                  <Badge tone="warn" title="Indexed under a root that is no longer configured">
                    retired
                  </Badge>
                ) : null}
                {!r.exists ? <Badge tone="danger">missing</Badge> : null}
              </td>
              <td className="gp-u-fs-11 gp-u-meta gp-u-break-all">{r.path}</td>
              <td className="gp-table__num gp-u-num"
                title={fmtBytes(r.contents && r.contents.models && r.contents.models.bytes)}>
                {fmtCount(r.contents && r.contents.models && r.contents.models.count)}
              </td>
              <td className="gp-table__num gp-u-num"
                title={fmtBytes(r.contents && r.contents.outputs && r.contents.outputs.bytes)}>
                {fmtCount(r.contents && r.contents.outputs && r.contents.outputs.count)}
              </td>
              <td className="gp-table__num gp-u-num">{fmtBytes(r.indexed_bytes)}</td>
              <td className="gp-table__num gp-u-num">
                {r.volume && r.volume.available
                  ? fmtBytes(r.volume.free_bytes)
                  : <span className="gp-u-dim">-</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {roots && roots.retention_note ? (
        <div className="gp-callout gp-callout--info gp-u-mt-5">
          <span className="gp-callout__icon"><Database aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">
              If you change the ComfyUI path: {roots.retention_policy}
            </div>
            {roots.retention_note}
          </div>
        </div>
      ) : null}
    </>
  )
}
