import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, X, FileText } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useDebounced from '../../hooks/useDebounced.js'
import { useVault, useTabView } from '../../state/VaultContext.jsx'
import { ADVISOR_SECTIONS, ADVISOR_SORTS } from '../../state/actions.js'
import Button from '../common/Button.jsx'
import Chip from '../common/Chip.jsx'
import Select from '../common/Select.jsx'
import Toggle from '../common/Toggle.jsx'
import ResourceDashboard from '../advisor/ResourceDashboard.jsx'
import Recommendations from '../advisor/Recommendations.jsx'
import CatalogPanel from '../advisor/CatalogPanel.jsx'
import InstalledPanel from '../advisor/InstalledPanel.jsx'
import SafeConfigPanel from '../advisor/SafeConfigPanel.jsx'
import SourcesPanel from '../advisor/SourcesPanel.jsx'
import { KindIcon } from '../advisor/parts.jsx'
import { buildReport, saveText } from '../advisor/report.js'
import { ago, humanise } from '../../services/format.js'
import '../../styles/advisor.css'

/*
 * AdvisorTab - "what can this machine run, and how safely?"
 *
 * One workspace, six layers, picked from the rail like Storage:
 *   dashboard   live resources
 *   recommend   optimum, maximum and stretch picks per model type
 *   catalog     every known model, assessed
 *   installed   the same analysis for files already in the vault
 *   safe        headroom and the launch settings that enforce it
 *   sources     where the data comes from, and the refresh
 *
 * Requests are made per section, so opening the tab costs the hardware profile
 * and one live reading.
 *
 * "Types to check" in the second toolbar row is a saved preference: it narrows
 * the recommendations, the catalog and the automatic refresh alike.
 */

const INSTALLED_SORTS = [
  { value: '-need', label: 'Heaviest first' },
  { value: 'need', label: 'Lightest first' },
  { value: '-size', label: 'Largest file' },
  { value: 'security', label: 'Least safe first' },
  { value: 'name', label: 'Name A-Z' }
]
const UPDATE_POLL_MS = 1500

export default function AdvisorTab({ onStatus }) {
  const { state, dispatch, toastError } = useVault()
  const { view, patch } = useTabView('advisor')
  const epoch = state.dataEpoch
  const section = view.section || 'dashboard'
  const [hwNonce, setHwNonce] = useState(0)
  const [nonce, setNonce] = useState(0)
  const debouncedQuery = useDebounced(view.q, 160)
  const filters = view.filters || {}

  const bump = useCallback(() => setNonce((n) => n + 1), [])

  /* ------------------------------------------------------------ resources */
  const hardware = useResource('advisor:hardware:' + hwNonce + ':' + nonce,
    (s) => api.advisorHardware(hwNonce > 0 ? true : undefined, s), { epoch })
  const settings = useResource('advisor:settings:' + nonce,
    (s) => api.advisorSettings(s), { epoch })
  const update = useResource('advisor:update:' + nonce,
    (s) => api.advisorUpdateStatus(s), { epoch })

  const checked = useMemo(
    () => (settings.data && settings.data.kinds) || [], [settings.data])

  const recommendations = useResource(
    section === 'recommend'
      ? 'advisor:recommend:' + nonce + ':' + (filters.kind || '') : null,
    (s) => api.advisorRecommendations({ kind: filters.kind, per_kind: 6 }, s),
    { epoch }
  )

  const catalogQuery = useMemo(() => ({
    kind: filters.kind, verdict: filters.verdict, source: filters.source,
    q: debouncedQuery || undefined, sort: view.sort,
    runs_only: view.runsOnly || undefined, new_only: view.newOnly || undefined,
    limit: view.limit, offset: view.offset
  }), [filters.kind, filters.verdict, filters.source, debouncedQuery, view.sort,
    view.runsOnly, view.newOnly, view.limit, view.offset])

  const catalog = useResource(
    section === 'catalog'
      ? 'advisor:catalog:' + nonce + ':' + JSON.stringify(catalogQuery) : null,
    (s) => api.advisorCatalog(catalogQuery, s), { epoch }
  )

  const installedSort = INSTALLED_SORTS.some((o) => o.value === view.installedSort)
    ? view.installedSort : '-need'
  const installedQuery = useMemo(() => ({
    kind: filters.kind, verdict: filters.verdict, q: debouncedQuery || undefined,
    sort: installedSort, limit: view.limit, offset: view.offset
  }), [filters.kind, filters.verdict, debouncedQuery, installedSort, view.limit, view.offset])

  const installed = useResource(
    section === 'installed'
      ? 'advisor:installed:' + nonce + ':' + JSON.stringify(installedQuery) : null,
    (s) => api.advisorInstalled(installedQuery, s), { epoch }
  )

  const safe = useResource(section === 'safe' ? 'advisor:safe:' + nonce : null,
    (s) => api.advisorSafeConfig(s), { epoch })
  const sources = useResource(section === 'sources' ? 'advisor:sources:' + nonce : null,
    (s) => api.advisorSources(s), { epoch })

  /* While a refresh runs, watch it; when it ends, reload what depends on it. */
  const running = Boolean(update.data && update.data.running)
  useEffect(() => {
    if (!running) return undefined
    const timer = setInterval(() => {
      api.advisorUpdateStatus().then((next) => {
        if (!next.running) dispatch({ type: 'invalidate' })
        bump()
      }).catch(() => {})
    }, UPDATE_POLL_MS)
    return () => clearInterval(timer)
  }, [running, bump, dispatch])

  /* Self-initiated refresh: when the Advisor opens on stale data, ask once.
     The server applies the same guards as its own startup timer - outbound
     lookups allowed, automatic refresh on, nothing attempted recently - so
     this is a request, not a decision made here. */
  const asked = useRef(false)
  const due = Boolean(update.data && update.data.stale && update.data.online_enabled &&
    update.data.auto_update && !update.data.running)
  useEffect(() => {
    if (!due || asked.current) return
    asked.current = true
    api.advisorUpdate({ auto: true }).then(bump).catch(() => {})
  }, [due, bump])

  /* -------------------------------------------------- status bar reporting */
  const page = section === 'catalog' ? (catalog.data && catalog.data.page)
    : (section === 'installed' ? (installed.data && installed.data.page) : null)
  useEffect(() => {
    onStatus({ page, elapsed: null, mode: null })
  }, [page, onStatus])

  /* -------------------------------------------------------------- actions */
  const openDetail = useCallback((id) => {
    patch({ detailUid: id }, { keepOffset: true })
    dispatch({ type: 'set-panel', key: 'detailsOpen', value: true })
  }, [patch, dispatch])

  const toggleChecked = useCallback((kind) => {
    const all = (settings.data && settings.data.kind_options) || []
    const current = checked.length ? checked : all.map((k) => k.id)
    let next = current.includes(kind)
      ? current.filter((k) => k !== kind) : current.concat(kind)
    // Every type ticked is the same as no restriction, and is stored that way.
    if (next.length === all.length || next.length === 0) next = []
    api.advisorPatchSettings({ kinds: next }).then(bump)
      .catch((err) => toastError(err, 'The selection could not be saved'))
  }, [checked, settings.data, bump, toastError])

  const setFilter = useCallback((field, value) => {
    const next = { ...filters }
    if (value === null || value === undefined || value === '') delete next[field]
    else next[field] = value
    patch({ filters: next })
  }, [filters, patch])

  const startUpdate = useCallback(() => {
    api.advisorUpdate({}).then(bump)
      .catch((err) => toastError(err, 'The refresh could not start'))
  }, [bump, toastError])

  /* The report is assembled from the same payloads the tab shows, fetched
     fresh so it is whole even if only one section has been opened. */
  const [reporting, setReporting] = useState(false)
  const saveReport = useCallback(() => {
    setReporting(true)
    Promise.all([
      api.advisorHardware(), api.advisorRecommendations({ per_kind: 6 }),
      api.advisorSafeConfig()
    ]).then(([hw, rec, safeCfg]) => {
      const text = buildReport({
        hardware: hw, recommendations: rec, safeConfig: safeCfg,
        appVersion: state.info && state.info.version
      })
      saveText('system-advisor-report-' + new Date().toISOString().slice(0, 10) + '.md', text)
    }).catch((err) => toastError(err, 'The report could not be built'))
      .finally(() => setReporting(false))
  }, [state.info, toastError])

  const openSettings = useCallback(() => {
    dispatch({ type: 'open-modal', name: 'settings', props: { initialTab: 'search' } })
  }, [dispatch])

  /* ------------------------------------------------------------ rendering */
  const title = (ADVISOR_SECTIONS.find((s) => s.id === section) || {}).label
  const kindOptions = (settings.data && settings.data.kind_options) || []
  const budget = hardware.data && hardware.data.budget
  const listing = section === 'catalog' || section === 'installed'
  const hasFilters = Boolean(filters.kind || filters.verdict || filters.source ||
    view.q || view.runsOnly || view.newOnly)
  const showTypes = section === 'recommend' || section === 'catalog' || section === 'sources'
  const u = update.data

  let body = null
  if (section === 'dashboard') {
    body = (
      <ResourceDashboard hardware={hardware.data} error={hardware.error}
        refreshing={hardware.loading && hwNonce > 0}
        onRefresh={() => setHwNonce((n) => n + 1)} />
    )
  } else if (section === 'recommend') {
    body = (
      <Recommendations data={recommendations.data} loading={recommendations.loading}
        error={recommendations.error} onOpen={openDetail}
        onShowAll={(kind) => patch({ section: 'catalog', filters: { kind }, runsOnly: true })} />
    )
  } else if (section === 'catalog') {
    body = (
      <CatalogPanel data={catalog.data} loading={catalog.loading} error={catalog.error}
        selectedId={view.detailUid} onOpen={openDetail} budget={budget}
        filtered={hasFilters}
        onClear={() => patch({ filters: {}, q: '', runsOnly: false, newOnly: false })} />
    )
  } else if (section === 'installed') {
    body = (
      <InstalledPanel data={installed.data} loading={installed.loading}
        error={installed.error} selectedId={view.detailUid} onOpen={openDetail}
        budget={budget} />
    )
  } else if (section === 'safe') {
    body = (
      <SafeConfigPanel data={safe.data} loading={safe.loading} error={safe.error}
        settings={settings.data} onChanged={bump} />
    )
  } else {
    body = (
      <SourcesPanel data={sources.data} loading={sources.loading} error={sources.error}
        settings={settings.data} onChanged={bump} onOpenSettings={openSettings} />
    )
  }

  return (
    <>
      <div className="gp-toolbar">
        <span className="gp-toolbar__label">{title}</span>
        {listing ? (
          <div className="gp-toolbar__group">
            {section === 'catalog' ? (
              <>
                <Select bare value={view.sort} options={ADVISOR_SORTS} ariaLabel="Sort the catalog"
                  onChange={(v) => patch({ sort: v })} />
                <Toggle id="advisor-runs-only" label="Runs here"
                  checked={Boolean(view.runsOnly)}
                  onChange={(v) => patch({ runsOnly: v })}
                  title="Hide models that do not fit this machine" />
                <Toggle id="advisor-new-only" label="New"
                  checked={Boolean(view.newOnly)}
                  onChange={(v) => patch({ newOnly: v })}
                  title="Only models first seen in the last week" />
              </>
            ) : (
              <Select bare value={installedSort} options={INSTALLED_SORTS}
                ariaLabel="Sort your models"
                onChange={(v) => patch({ installedSort: v })} />
            )}
          </div>
        ) : null}
        <span className="gp-toolbar__spacer" />
        <Button size="sm" variant="ghost" icon={FileText} label="Report" loading={reporting}
          onClick={saveReport}
          title="Save this machine's profile, recommendations and safe settings as a Markdown file" />
        {u ? (
          <div className="gp-toolbar__group">
            <span className={'gp-u-fs-11 ' + (u.stale ? 'gp-u-warn' : 'gp-u-meta')}
              title={u.online_enabled
                ? 'The catalog refreshes itself when it is over a day old'
                : 'Outbound lookups are off: working from the shipped dataset'}>
              {u.running ? (u.phase_label || 'Refreshing') +
                (u.total ? ' ' + u.done + '/' + u.total : '')
                : 'Catalog ' + (u.catalog_generated_at ? ago(u.catalog_generated_at) : 'empty') +
                  (u.online_enabled ? '' : ' - offline')}
            </span>
            <Button size="sm" icon={RefreshCw} label="Update" loading={u.running}
              disabled={!u.online_enabled || u.running} onClick={startUpdate}
              title={u.online_enabled ? 'Refresh the model catalog from its sources'
                : 'Allow outbound lookups in Settings to refresh the catalog'} />
          </div>
        ) : null}
      </div>

      {showTypes || (listing && hasFilters) ? (
        <div className="gp-facetbar" tabIndex={0} aria-label="Advisor filters">
          {showTypes ? (
            <div className="gp-facetbar__group">
              <span className="gp-perpage__label" title="Saved: narrows recommendations, the catalog and the automatic refresh">
                Types to check
              </span>
              {kindOptions.map((k) => {
                const on = !checked.length || checked.includes(k.id)
                return (
                  <button key={k.id} type="button" aria-pressed={on}
                    className={'gp-chip gp-typechip' + (on ? ' gp-chip--selected' : '')}
                    onClick={() => toggleChecked(k.id)}
                    title={(on ? 'Checking ' : 'Not checking ') + k.label}>
                    <KindIcon kind={k.id} />
                    <span>{k.label}</span>
                  </button>
                )
              })}
            </div>
          ) : null}
          {listing && hasFilters ? (
            <div className="gp-facetbar__group gp-facetbar__group--active">
              {filters.kind ? <Chip selected label={'Type: ' + humanise(filters.kind)}
                onRemove={() => setFilter('kind', null)} /> : null}
              {filters.verdict ? <Chip selected label={'Fit: ' + humanise(filters.verdict)}
                onRemove={() => setFilter('verdict', null)} /> : null}
              {filters.source ? <Chip selected label={'Source: ' + humanise(filters.source)}
                onRemove={() => setFilter('source', null)} /> : null}
              {view.q ? <Chip selected label={'"' + view.q + '"'}
                onRemove={() => patch({ q: '' })} /> : null}
              <Button variant="ghost" size="sm" icon={X} label="Clear"
                onClick={() => patch({ filters: {}, q: '', runsOnly: false, newOnly: false })} />
            </div>
          ) : null}
        </div>
      ) : <div className="gp-facetbar gp-facetbar--empty" />}

      <div className={'gp-main__body gp-adv-scope' + (listing ? ' gp-main__body--flush' : '')}>
        {body}
      </div>
    </>
  )
}
