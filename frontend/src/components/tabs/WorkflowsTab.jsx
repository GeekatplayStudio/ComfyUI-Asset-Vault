import React, { useMemo, useCallback, useState, useEffect } from 'react'
import { Workflow, AlertTriangle, Search, ShieldAlert, LayoutTemplate } from 'lucide-react'
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
 *
 * With no Source chip picked the list is the owner's own workflows plus the
 * bundled examples, exactly as before templates were indexed. ComfyUI ships
 * close to 700 official templates; mixed in by default they would bury the
 * owner's work, so they are one click away instead.
 */
const ORIGIN_LABELS = {
  user: 'Mine',
  bundled: 'Bundled with nodes',
  official_template: 'ComfyUI templates'
}
const TEMPLATES = ['official_template']
export default function WorkflowsTab({ onStatus, onOpenUid, onLightbox, registerApi }) {
  const { state, dispatch, toast } = useVault()
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
  const originsRes = useResource('workflow-origins',
    (s) => api.comfyWorkflowOrigins(s), { epoch: state.dataEpoch })
  const originCounts = useMemo(() => {
    const out = {}
    const rows = (originsRes.data && (originsRes.data.groups || originsRes.data.items ||
      originsRes.data)) || []
    for (const g of Array.isArray(rows) ? rows : []) {
      out[g.origin] = (out[g.origin] || 0) + (g.count || 0)
    }
    return out
  }, [originsRes.data])

  const origin = view.filters.origin
  const showingTemplates = Array.isArray(origin)
    ? origin.length === 1 && origin[0] === 'official_template'
    : origin === 'official_template'

  /* The row and card action only ever opens the dialog; the dialog is where the
     plan is shown and where starting ComfyUI is confirmed by its exact path. */
  const openExternal = useCallback((item) => {
    setOpenInComfy({ uid: item.uid, name: item.name || item.title })
  }, [])

  const requestOp = useCallback((kind, uids) => {
    const picked = uids ? items.filter((i) => uids.includes(i.uid)) : selectedItems
    /* Official templates are ComfyUI's own files: the server refuses to touch
       them, and the UI says so up front rather than after a failed request. */
    const targets = picked.filter((i) => !i.read_only)
    if (picked.length && targets.length < picked.length) {
      toast({
        tone: 'info',
        title: targets.length ? 'Templates left out' : 'Official templates are read-only',
        message: 'ComfyUI\'s own templates are never renamed, moved or deleted by the vault. ' +
          'Open one in ComfyUI and save a copy to make it yours.'
      })
    }
    if (!targets.length) return
    setOp({ kind, items: targets })
  }, [items, selectedItems, toast])

  useEffect(() => { setRequestOp(requestOp) }, [requestOp, setRequestOp])

  const facetDefs = useMemo(() => {
    const defs = [
      buildFacet('origin', 'Source', Object.keys(ORIGIN_LABELS).map((key) => ({
        value: key, label: ORIGIN_LABELS[key], count: originCounts[key]
      })), 3),
      buildBoolFacet('ready', 'Ready to run', [
        { value: true, label: 'Nothing missing' },
        { value: false, label: 'Something missing' }
      ]),
      buildBoolFacet('runnable', 'Nodes', [
        { value: true, label: 'All nodes installed' },
        { value: false, label: 'Missing nodes' }
      ]),
      buildBoolFacet('has_outputs', 'Outputs', [
        { value: true, label: 'Has outputs' }
      ])
    ]
    const bases = ((baseFacet.data && baseFacet.data.groups) || [])
      .filter((g) => g.key)
      .map((g) => ({ value: g.key, label: g.label || g.key, count: g.count }))
    if (bases.length) defs.push(buildFacet('base_model', 'Base', bases, 8))
    return defs
  }, [baseFacet.data, buildFacet, buildBoolFacet, originCounts])

  const broken = state.stats ? state.stats.workflows_broken : null
  const templateCount = originCounts.official_template ||
    (state.stats ? state.stats.templates : 0) || 0

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the workflows"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (showingTemplates && !view.q && activeFilters.length === 1 ? (
    <EmptyState icon={LayoutTemplate} title="No ComfyUI templates indexed"
      text="ComfyUI's official templates are read from the comfyui_workflow_templates packages inside the Python that runs ComfyUI. None were found - either they are not installed, or no scan has run since this version of the vault was installed."
      actions={(
        <Button variant="primary"
          onClick={() => dispatch({ type: 'open-modal', name: 'index', props: { autoStart: true } })}
        >
          Run a scan
        </Button>
      )} />
  ) : view.q || activeFilters.length ? (
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
        resultLabel={page ? page.total + (showingTemplates ? ' templates' : ' workflows') : null}
        leading={(
          <>
            {templateCount ? (
              <Button size="sm" variant={showingTemplates ? 'primary' : 'ghost'}
                icon={LayoutTemplate} label="ComfyUI templates" count={templateCount}
                aria-pressed={showingTemplates}
                title={showingTemplates
                  ? 'Back to your own workflows'
                  : 'ComfyUI\'s official templates, linked to the models you have and the images made from them'}
                onClick={() => setFilter('origin', showingTemplates ? null : TEMPLATES)} />
            ) : null}
            {broken && !showingTemplates ? (
              <Button size="sm" variant="dangerGhost" icon={ShieldAlert}
                label="Needs attention" count={broken}
                aria-pressed={view.filters.runnable === false}
                title={broken + ' workflows reference something that is not on disk'}
                onClick={() => setFilter('runnable', view.filters.runnable === false ? null : false)} />
            ) : null}
          </>
        )}
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
