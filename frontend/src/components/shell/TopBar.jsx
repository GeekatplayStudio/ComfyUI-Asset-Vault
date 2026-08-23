import React from 'react'
import {
  Box, Package, Workflow, Image as ImageIcon, HardDrive,
  Settings, Activity, RefreshCw, Hash
} from 'lucide-react'
import Button from '../common/Button.jsx'
import Toggle from '../common/Toggle.jsx'
import SearchInput from '../common/SearchInput.jsx'
import { TABS } from '../../state/actions.js'
import { count as fmtCount, parseUid } from '../../services/format.js'

/* The Geekatplay mark, inlined so it inherits the amber from its host. */
function BrandMark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3.25" y="3.25" width="17.5" height="17.5" rx="3"
        stroke="currentColor" strokeWidth="1.4" opacity=".55" />
      <rect x="6.6" y="6.8" width="2" height="10.4" rx="1" fill="currentColor" />
      <rect x="10.8" y="7.1" width="7" height="1.6" rx=".8" fill="currentColor" opacity=".9" />
      <rect x="10.8" y="11.2" width="5.2" height="1.6" rx=".8" fill="currentColor" opacity=".6" />
      <rect x="10.8" y="15.3" width="3.4" height="1.6" rx=".8" fill="currentColor" opacity=".38" />
    </svg>
  )
}

const TAB_ICONS = {
  models: Box, nodes: Package, workflows: Workflow, outputs: ImageIcon, storage: HardDrive
}

/*
 * TopBar - brand lockup, the four asset tabs (plus the reserved Storage slot),
 * search, the Smart toggle and the global actions.
 */
export default function TopBar(props) {
  const {
    tab, onTab, stats, query, onQuery, onPickSuggestion,
    smart, onSmart, smartAvailable, smartReason,
    onSettings, onHealth, onReindex, onHash, searchRef, indexing, searching
  } = props

  const tabCount = (t) => {
    if (!stats || !t.statKey) return null
    const value = stats[t.statKey]
    return value === undefined || value === null ? null : fmtCount(value)
  }

  return (
    <header className="gp-topbar">
      <div className="gp-topbar__brand">
        <div className="gp-brand">
          <span className="gp-brand__mark"><BrandMark /></span>
          <span className="gp-brand__text">
            <span className="gp-brand__word">GEEKATPLAY</span>
            <span className="gp-brand__sub">ASSET VAULT</span>
          </span>
        </div>
      </div>

      <span className="gp-topbar__divider" />

      <nav className="gp-topbar__tabs">
        <div className="gp-tabs" role="tablist" aria-label="Asset kinds">
          {TABS.map((t) => {
            const Icon = TAB_ICONS[t.id]
            const active = tab === t.id
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={active}
                disabled={t.pending}
                title={t.pending
                  ? 'Storage and maintenance - arriving in a later build'
                  : t.label}
                className={'gp-tab' + (active ? ' gp-tab--active' : '')}
                onClick={() => onTab(t.id)}
              >
                <Icon aria-hidden="true" />
                <span>{t.label}</span>
                {tabCount(t) ? <span className="gp-tab__count">{tabCount(t)}</span> : null}
              </button>
            )
          })}
        </div>
      </nav>

      <div className="gp-topbar__search">
        <SearchInput
          value={query}
          inputRef={searchRef}
          busy={searching}
          onChange={onQuery}
          onPick={(item) => onPickSuggestion(parseUid(item.uid), item)}
          placeholder="Search models, nodes, workflows and outputs"
        />
      </div>

      <Toggle
        id="smart-search-toggle"
        ai
        label="Smart"
        checked={smart && smartAvailable}
        disabled={!smartAvailable}
        onChange={onSmart}
        title={smartAvailable
          ? 'Hybrid ranking: keyword index fused with vector similarity'
          : 'Smart search unavailable - ' + (smartReason || 'not installed')}
      />

      <div className="gp-topbar__spacer" />

      <div className="gp-topbar__actions">
        <Button
          variant="ghost"
          iconOnly
          icon={Activity}
          aria-label="System health"
          title="System health"
          onClick={onHealth}
        />
        <Button
          variant="ghost"
          iconOnly
          icon={Settings}
          aria-label="Settings"
          title="Settings"
          onClick={onSettings}
        />
        <Button
          icon={RefreshCw}
          label="Reindex"
          loading={indexing}
          title="Rescan the ComfyUI installation (F5)"
          onClick={onReindex}
        />
        <Button
          variant="primary"
          icon={Hash}
          label="Hash"
          title="Compute file hashes so Civitai matching can run"
          onClick={onHash}
        />
      </div>
    </header>
  )
}
