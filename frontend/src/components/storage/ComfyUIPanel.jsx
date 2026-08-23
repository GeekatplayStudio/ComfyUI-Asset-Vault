import React, { useState } from 'react'
import {
  Package, Play, RefreshCw, AlertTriangle, Info, CheckCircle2, WifiOff, FileJson
} from 'lucide-react'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow from '../details/MetaRow.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import UpdateDialog from './UpdateDialog.jsx'
import { count as fmtCount, humanise, dateTime } from '../../services/format.js'

/*
 * ComfyUIPanel - which ComfyUI is installed, whether a newer one exists, and
 * the one place in this product that starts a process (C8).
 *
 * Two things are deliberately quiet here. A failed version check is a callout,
 * never an error toast: the panel has to render with no network at all. And the
 * "is ComfyUI running" probe reports its own confidence: a port that answered
 * as ComfyUI is stated plainly, while a port that is merely taken - or a
 * silence - is painted violet with the "~" marker rather than stated as fact.
 */

function SectionHead({ label, aside }) {
  return (
    <div className="gp-group-head">
      <span className="gp-group-head__label">{label}</span>
      <span className="gp-group-head__rule" />
      {aside}
    </div>
  )
}

function LatestState({ latest }) {
  if (!latest) return null

  if (latest.state === 'behind' && latest.latest) {
    return (
      <div className="gp-callout gp-callout--info gp-u-mb-6">
        <span className="gp-callout__icon"><Info aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <div className="gp-callout__title">
            ComfyUI {latest.latest} is available - you have {latest.installed}
          </div>
          {latest.release_notes || 'Read the release notes before updating.'}
        </div>
      </div>
    )
  }

  if (latest.state === 'current') {
    return (
      <div className="gp-callout gp-callout--ok gp-u-mb-6">
        <span className="gp-callout__icon"><CheckCircle2 aria-hidden="true" /></span>
        <div className="gp-callout__body">
          <div className="gp-callout__title">ComfyUI {latest.installed} is up to date</div>
          Checked {dateTime(latest.checked_at)} against {latest.source || 'the release feed'}.
        </div>
      </div>
    )
  }

  /* unknown / offline / rate limited / lookups switched off - all 200s. */
  return (
    <div className="gp-callout gp-callout--info gp-u-mb-6">
      <span className="gp-callout__icon"><WifiOff aria-hidden="true" /></span>
      <div className="gp-callout__body">
        <div className="gp-callout__title">
          The latest version was not checked{latest.reason ? ' - ' + humanise(latest.reason) : ''}
        </div>
        {latest.hint || 'The vault works entirely offline; this check is optional and never automatic.'}
      </div>
    </div>
  )
}

export default function ComfyUIPanel(props) {
  const { info, latest, plan, templates, loading, error, onRefresh, onCheckLatest } = props
  const [updating, setUpdating] = useState(false)

  if (error) {
    return (
      <>
      <div className="gp-toolbar"><span className="gp-toolbar__label">ComfyUI</span></div>
      <div className="gp-facetbar gp-facetbar--empty" />
      <div className="gp-main__body">
        <EmptyState tone="error" icon={AlertTriangle} title="Could not read the installation"
          text={error.message}
          actions={<Button variant="primary" onClick={onRefresh}>Try again</Button>} />
      </div>
      </>
    )
  }

  if (loading && !info) {
    return (
      <>
        <div className="gp-toolbar"><span className="gp-toolbar__label">ComfyUI</span></div>
        <div className="gp-facetbar gp-facetbar--empty" />
        <div className="gp-main__body"><SkeletonRows rows={10} /></div>
      </>
    )
  }
  if (!info) return null

  if (!info.configured) {
    return (
      <>
        <div className="gp-toolbar"><span className="gp-toolbar__label">ComfyUI</span></div>
        <div className="gp-facetbar gp-facetbar--empty" />
        <div className="gp-main__body">
          <EmptyState icon={Package} title="No ComfyUI installation is configured"
            text="Point the vault at a ComfyUI folder in Settings to see its version and run its updater." />
        </div>
      </>
    )
  }

  const running = info.running || {}
  const git = info.git || {}
  const packages = info.packages || {}
  const canRun = Boolean(plan && plan.can_run)

  return (
    <>
      <div className="gp-toolbar">
        <div className="gp-toolbar__group">
          <Package size={14} aria-hidden="true" className="gp-u-meta" />
          <span className="gp-u-fw-600 gp-u-num gp-u-fs-15">ComfyUI {info.version}</span>
          <Badge tone="neutral">{info.flavour}</Badge>
          {running.running ? (
            <Badge tone="warn" title={running.note}>
              {running.confirmed
                ? 'running'
                : <span className="gp-inferred"
                  title={'Decided by a ' + (running.method || 'loopback probe') +
                    '. ' + (running.note || '')}>port in use</span>}
            </Badge>
          ) : (
            <Badge tone="neutral" title={running.note || 'Loopback port probe'}>
              {running.confidence === 'inferred'
                ? <span className="gp-inferred"
                  title={'Decided by a ' + (running.method || 'loopback probe') +
                    '. ' + (running.note || '')}>not running</span>
                : 'not running'}
            </Badge>
          )}
        </div>
        <div className="gp-toolbar__spacer" />
        <div className="gp-toolbar__group">
          <Button size="sm" variant="ghost" icon={RefreshCw} label="Check for updates"
            title="One read-only lookup. Never automatic, never scheduled."
            onClick={onCheckLatest} />
        </div>
        <span className="gp-divider gp-divider--v" />
        <div className="gp-toolbar__group">
          <Button
            size="sm"
            variant="danger"
            icon={Play}
            label="Run the official updater"
            disabled={!plan}
            title={plan
              ? (canRun
                ? 'Runs ' + plan.path + ' - you confirm the exact path first'
                : 'Blocked: ' + humanise(plan.blocked_reason || 'unavailable'))
              : 'No updater was found for this installation'}
            onClick={() => setUpdating(true)}
          />
        </div>
      </div>

      <div className="gp-facetbar">
        <span className="gp-toolbar__label">Installed</span>
        <span className="gp-chip gp-chip--mono gp-chip--sm">ComfyUI {info.version}</span>
        {info.packages && info.packages.comfyui_frontend_package ? (
          <span className="gp-chip gp-chip--mono gp-chip--sm">
            frontend {info.packages.comfyui_frontend_package}
          </span>
        ) : null}
        {templates && templates.available ? (
          <span className="gp-chip gp-chip--mono gp-chip--sm">
            {fmtCount(templates.total)} official templates
          </span>
        ) : null}
      </div>

      <div className="gp-main__body">
        <LatestState latest={latest} />

        {!plan ? (
          <div className="gp-callout gp-callout--warn gp-u-mb-6">
            <span className="gp-callout__icon"><AlertTriangle aria-hidden="true" /></span>
            <div className="gp-callout__body">
              <div className="gp-callout__title">No updater was found for this install</div>
              The vault discovers the update mechanism rather than assuming one. Update ComfyUI
              the way you normally do, then re-scan the vault.
            </div>
          </div>
        ) : null}

        {/* ------------------------------------------------------- the install */}
        <SectionHead label="This installation" />
        <div className="gp-panel gp-u-mb-6">
          <div className="gp-meta">
            <MetaRow label="Version" value={info.version} num
              title={'Parsed from ' + (info.version_source || 'the source tree')} />
            <MetaRow label="Folder" value={info.comfyui_path} wrap />
            <MetaRow label="Flavour" value={humanise(info.flavour)} />
            <MetaRow label="Python" value={info.python_home} wrap />
            {git.present ? (
              <>
                <MetaRow label="Git branch" value={git.branch} />
                <MetaRow label="Commit" value={git.commit ? git.commit.slice(0, 12) : null} num
                  title={git.commit} />
                <MetaRow label="Remote" value={git.remote} wrap />
                {git.shallow ? (
                  <MetaRow label="Checkout depth" value="shallow"
                    title="A shallow clone cannot always fast-forward; the portable updater is safer." />
                ) : null}
              </>
            ) : null}
          </div>

          {info.flavour_evidence && info.flavour_evidence.length ? (
            <>
              <div className="gp-u-fs-10 gp-u-caps gp-u-meta gp-u-mt-6 gp-u-mb-4">
                Why it was read as {info.flavour}
              </div>
              <ul className="gp-confirm__list">
                {info.flavour_evidence.map((line, i) => (
                  <li key={'evidence:' + i}>{line}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>

        {/* ---------------------------------------------------------- packages */}
        {Object.keys(packages).length ? (
          <>
            <SectionHead label="Packages in the interpreter that launches ComfyUI" />
            <table className="gp-table gp-u-w-full gp-u-mb-6">
              <thead>
                <tr>
                  <th>Package</th>
                  <th className="gp-table__num">Version</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(packages).map(([name, version]) => (
                  <tr key={name}>
                    <td className="gp-u-fg">{name}</td>
                    <td className="gp-table__num gp-u-num">{version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : null}

        {/* --------------------------------------------------------- templates */}
        {templates && templates.available ? (
          <>
            <SectionHead
              label="Workflow templates shipped with ComfyUI"
              aside={<span className="gp-group-head__count">{fmtCount(templates.total)}</span>}
            />
            <div className="gp-panel gp-u-mb-6">
              <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mb-5">
                {(templates.bundles || []).map((b) => (
                  <Badge key={b.key} tone="neutral" mono title={b.key}>
                    {humanise(b.key)} {fmtCount(b.count)}
                  </Badge>
                ))}
              </div>
              <div className="gp-callout gp-callout--info">
                <span className="gp-callout__icon"><FileJson aria-hidden="true" /></span>
                <div className="gp-callout__body">{templates.note}</div>
              </div>
            </div>
          </>
        ) : null}
      </div>

      {updating && plan ? (
        <UpdateDialog plan={plan} info={info} onClose={() => setUpdating(false)} />
      ) : null}
    </>
  )
}
