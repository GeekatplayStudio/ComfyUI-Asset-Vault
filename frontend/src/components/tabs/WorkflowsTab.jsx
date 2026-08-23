import React, { useMemo, useCallback, useState, useEffect } from 'react'
import { Workflow, AlertTriangle, Search, ShieldAlert } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useAssetTab from '../../hooks/useAssetTab.js'
import AssetGrid from '../grid/AssetGrid.jsx'
import GridToolbar, { SelectionActions } from '../grid/GridToolbar.jsx'
import EmptyState from '../common/EmptyState.jsx'
import Button from '../common/Button.jsx'
import FileOpsModal from '../modals/FileOpsModal.jsx'
import OpenInComfyUIDialog from '../modals/OpenInComfyUIDialog.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { SORTS, GROUPS } from '../../state/actions.js'

/*
 * WorkflowsTab - user workflows, official templates and the example graphs that
 * ship inside custom node packages.
 */
export default function WorkflowsTab({ onStatus, onOpenUid, onLightbox, registerApi }) {
  const { state, dispatch } = useVault()
  const [op, setOp] = useState(null)
  const [openInComfy, setOpenInComfy] = useState(null)

  const open = useCallback((item) => onLightbox(item.uid), [onLightbox])

  const tab = useAssetTab({
    tab: 'workflows',
    scope: 'workflows',
    loader: (query, signal) => api.workflows(query, signal),
    onStatus,
    onOpen: open,
    registerApi
  })

  const {
    view, patch, setFilter, clearFilters, scrollRef, list, items, groups, page,
    selection, selectedItems, groupKeyOf, select, toggleCheck, activeFilters,
    buildFacet, buildBoolFacet, setRequestOp
  } = tab

  const baseFacet = useResource(
    'workflow-base:' + JSON.stringify(view.filters),
    (s) => api.workflows({ ...view.filters, group: 'base_model', limit: 1 }, s),
    { epoch: state.dataEpoch }
  )

  /* The row and card action only ever opens the dialog; the dialog is where the
     plan is shown and where starting ComfyUI is confirmed by its exact path. */
  const openExternal = useCallback((item) => {
    setOpenInComfy({ uid: item.uid, name: item.name || item.title })
  }, [])

  const requestOp = useCallback((kind, uids) => {
    const targets = uids ? items.filter((i) => uids.includes(i.uid)) : selectedItems
    if (!targets.length) return
    setOp({ kind, items: targets })
  }, [items, selectedItems])

  useEffect(() => { setRequestOp(requestOp) }, [requestOp, setRequestOp])

  const facetDefs = useMemo(() => {
    const defs = [buildBoolFacet('runnable', 'State', [
      { value: true, label: 'Runnable' },
      { value: false, label: 'Missing deps' }
    ])]
    const bases = ((baseFacet.data && baseFacet.data.groups) || [])
      .filter((g) => g.key)
      .map((g) => ({ value: g.key, label: g.label || g.key, count: g.count }))
    if (bases.length) defs.push(buildFacet('base_model', 'Base', bases, 8))
    return defs
  }, [baseFacet.data, buildFacet, buildBoolFacet])

  const broken = state.stats ? state.stats.workflows_broken : null

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the workflows"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (view.q || activeFilters.length ? (
    <EmptyState icon={Search} title="Nothing matches"
      text="No workflow matches the current search and filters."
      actions={<Button onClick={() => { patch({ q: '' }); clearFilters() }}>Clear the filters</Button>} />
  ) : (
    <EmptyState icon={Workflow} title="No workflows indexed"
      text="No workflow files were found. Both the user workflow folder and the workflows folder at the ComfyUI root are scanned, along with the example graphs inside custom node packages."
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
        sorts={SORTS.workflows}
        groupOptions={GROUPS.workflows}
        facets={facetDefs}
        activeFilters={activeFilters}
        onSetFilter={setFilter}
        onClearFilters={clearFilters}
        selectionCount={view.selection.length}
        resultLabel={page ? page.total + ' workflows' : null}
        leading={broken ? (
          <Button size="sm" variant="dangerGhost" icon={ShieldAlert}
            label="Needs attention" count={broken}
            aria-pressed={view.filters.runnable === false}
            title={broken + ' workflows reference something that is not on disk'}
            onClick={() => setFilter('runnable', view.filters.runnable === false ? null : false)} />
        ) : null}
        actions={(
          <SelectionActions
            count={view.selection.length}
            onRename={() => requestOp('rename')}
            onMove={() => requestOp('move')}
            onDelete={() => requestOp('delete')}
            onRefresh={list.refresh}
          />
        )}
      />

      <AssetGrid
        scrollRef={scrollRef}
        items={items}
        groups={groups}
        scope="workflows"
        mode={view.view}
        tile={view.tile}
        groupKeyOf={groupKeyOf}
        selection={selection}
        focusUid={view.focusUid}
        onSelect={select}
        onOpen={open}
        onToggleCheck={toggleCheck}
        onRename={(item) => requestOp('rename', [item.uid])}
        onDelete={(item) => requestOp('delete', [item.uid])}
        onOpenExternal={openExternal}
        loading={list.loading}
        empty={empty}
      />

      {openInComfy ? (
        <OpenInComfyUIDialog uid={openInComfy.uid} name={openInComfy.name}
          onClose={() => setOpenInComfy(null)} />
      ) : null}

      {op ? (
        <FileOpsModal op={op.kind} items={op.items} scope="workflows"
          onClose={() => setOp(null)}
          onCompleted={() => patch(
            { selection: [], focusUid: null, detailUid: null }, { keepOffset: true })} />
      ) : null}
    </>
  )
}
