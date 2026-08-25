import React, { useMemo, useCallback, useEffect } from 'react'
import { Package, Puzzle, AlertTriangle, Search, RefreshCw } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useAssetTab from '../../hooks/useAssetTab.js'
import AssetGrid from '../grid/AssetGrid.jsx'
import GridToolbar from '../grid/GridToolbar.jsx'
import EmptyState from '../common/EmptyState.jsx'
import Button from '../common/Button.jsx'
import { useVault, useTabView } from '../../state/VaultContext.jsx'
import { SORTS, GROUPS } from '../../state/actions.js'
import { count as fmtCount } from '../../services/format.js'
import NodeRegistryPanel from './NodeRegistryPanel.jsx'

/*
 * NodesTab - two views over the same domain: the installed packages, and the
 * flat catalogue of every node class they register (1,866 on this install).
 *
 * File operations are deliberately absent: a node package is a source folder
 * that ComfyUI owns, not an asset the vault renames or deletes on your behalf.
 */
export default function NodesTab({ onStatus, onOpenUid, registerApi }) {
  const { state, dispatch, toast, toastError, invalidate } = useVault()
  const { view: rawView } = useTabView('nodes')
  const registry = rawView.mode === 'registry'
  const classes = rawView.mode === 'classes'
  const scope = classes ? 'node_classes' : 'node_packages'

  const open = useCallback(() => {}, [])

  const tab = useAssetTab({
    tab: 'nodes',
    scope,
    cacheKey: scope,
    loader: (query, signal) => (classes
      ? api.nodeClasses(query, signal)
      : api.nodePackages(query, signal)),
    onStatus,
    onOpen: open,
    registerApi
  })

  const {
    view, patch, setFilter, clearFilters, scrollRef, list, items, groups, page,
    selection, groupKeyOf, select, toggleCheck, activeFilters,
    buildFacet, buildBoolFacet, setRequestOp
  } = tab

  const categoryFacet = useResource(
    classes ? 'class-categories:' + JSON.stringify(view.filters) : null,
    (s) => api.nodeClasses({ ...view.filters, group: 'category', limit: 1 }, s),
    { epoch: state.dataEpoch }
  )

  useEffect(() => {
    setRequestOp(() => {
      toast({
        tone: 'info',
        title: 'Not available for node packages',
        message: 'Node packages are source folders that ComfyUI manages, so the vault does not rename, move or delete them.'
      })
    })
  }, [setRequestOp, toast])

  const setMode = useCallback((mode) => {
    patch({
      mode,
      scope: mode === 'classes' ? 'node_classes' : 'node_packages',
      sort: mode === 'classes' ? 'display_name' : 'name',
      group: 'none',
      filters: {},
      railKey: null,
      selection: [],
      focusUid: null
    })
  }, [patch])

  const checkUpdates = useCallback(async () => {
    try {
      const res = await api.checkPackageUpdates(null)
      toast({
        tone: 'ok',
        title: 'Update check started',
        message: fmtCount(res.queued) + ' package(s) queued'
      })
      // The checks drain on the server; poll until none are pending, then
      // refresh so the update badges reflect what was found.
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        const status = await api.packageUpdateStatus()
        if (!status.pending) {
          invalidate()
          toast({
            tone: status.with_update ? 'info' : 'ok',
            title: 'Update check finished',
            message: status.with_update
              ? fmtCount(status.with_update) + ' package(s) have a newer commit'
              : 'Everything is at its remote tip'
          })
          return
        }
      }
    } catch (err) {
      toastError(err, 'Update check unavailable')
    }
  }, [toast, toastError, invalidate])

  const facetDefs = useMemo(() => {
    if (classes) {
      const cats = ((categoryFacet.data && categoryFacet.data.groups) || [])
        .filter((g) => g.key)
        .map((g) => ({ value: g.key, label: g.label || g.key, count: g.count }))
      return cats.length ? [buildFacet('category', 'Category', cats, 10)] : []
    }
    return [
      buildBoolFacet('official', 'Source', [
        { value: true, label: 'Official' },
        { value: false, label: 'Custom' }
      ]),
      buildBoolFacet('enabled', 'State', [{ value: false, label: 'Disabled' }])
    ]
  }, [classes, categoryFacet.data, buildFacet, buildBoolFacet])

  if (registry) {
    return <NodeRegistryPanel onMode={setMode} />
  }

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the node list"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (view.q || activeFilters.length ? (
    <EmptyState icon={Search} title="Nothing matches"
      text="No node matches the current search and filters."
      actions={<Button onClick={() => { patch({ q: '' }); clearFilters() }}>Clear the filters</Button>} />
  ) : (
    <EmptyState icon={classes ? Puzzle : Package}
      title={classes ? 'No node classes indexed' : 'No node packages indexed'}
      text="Custom packages are read from the custom_nodes folder, and the official classes come from the ComfyUI install itself."
      actions={(
        <Button variant="primary"
          onClick={() => dispatch({ type: 'open-modal', name: 'index', props: { autoStart: true } })}
        >
          Run a scan
        </Button>
      )} />
  ))

  return (
    <>
      <GridToolbar
        view={view}
        patch={patch}
        sorts={classes ? SORTS.node_classes : SORTS.node_packages}
        groupOptions={classes ? GROUPS.node_classes : GROUPS.node_packages}
        facets={facetDefs}
        activeFilters={activeFilters}
        onSetFilter={setFilter}
        onClearFilters={clearFilters}
        selectionCount={view.selection.length}
        resultLabel={page ? page.total + (classes ? ' classes' : ' packages') : null}
        leading={(
          <div className="gp-segment" role="group" aria-label="Node view">
            <button type="button"
              className={'gp-segment__item' + (!classes ? ' gp-segment__item--active' : '')}
              aria-pressed={!classes} onClick={() => setMode('packages')}>
              Packages
            </button>
            <button type="button"
              className={'gp-segment__item' + (classes ? ' gp-segment__item--active' : '')}
              aria-pressed={classes} onClick={() => setMode('classes')}>
              Classes
            </button>
            <button type="button"
              className="gp-segment__item"
              aria-pressed={false} onClick={() => setMode('registry')}>
              Registry
            </button>
          </div>
        )}
        actions={(
          <>
            <Button size="sm" icon={RefreshCw} label="Check updates"
              title="Ask each package's repository whether a newer commit exists"
              onClick={checkUpdates} />
            <Button size="sm" variant="ghost" iconOnly icon={RefreshCw}
              aria-label="Reload this view" title="Reload this view"
              onClick={list.refresh} />
          </>
        )}
      />

      <AssetGrid
        scrollRef={scrollRef}
        items={items}
        groups={groups}
        scope={scope}
        mode={view.view}
        tile={view.tile}
        groupKeyOf={groupKeyOf}
        selection={selection}
        focusUid={view.focusUid}
        onSelect={select}
        onOpen={select}
        onToggleCheck={toggleCheck}
        loading={list.loading}
        empty={empty}
      />
    </>
  )
}
