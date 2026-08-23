import React, { useMemo, useCallback, useState, useEffect } from 'react'
import { Box, AlertTriangle, Hash, Search } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import useAssetTab from '../../hooks/useAssetTab.js'
import AssetGrid from '../grid/AssetGrid.jsx'
import GridToolbar, { SelectionActions } from '../grid/GridToolbar.jsx'
import EmptyState from '../common/EmptyState.jsx'
import Button from '../common/Button.jsx'
import FileOpsModal from '../modals/FileOpsModal.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { SORTS, GROUPS } from '../../state/actions.js'

/*
 * ModelsTab - checkpoints, LoRAs, CLIP, VAE, ControlNet and everything else
 * under models/.
 */
export default function ModelsTab({ onStatus, onOpenUid, onLightbox, registerApi }) {
  const { state, dispatch } = useVault()
  const [op, setOp] = useState(null)

  const open = useCallback((item) => onLightbox(item.uid), [onLightbox])

  const tab = useAssetTab({
    tab: 'models',
    scope: 'models',
    loader: (query, signal) => api.models(query, signal),
    onStatus,
    onOpen: open,
    registerApi
  })

  const {
    view, patch, setFilter, clearFilters, scrollRef, list, items, groups, page,
    selection, selectedItems, groupKeyOf, select, toggleCheck, activeFilters,
    buildFacet, setRequestOp
  } = tab

  const facets = useResource(
    'model-facets:' + JSON.stringify(view.filters),
    (s) => api.modelFacets(view.filters, s),
    { epoch: state.dataEpoch }
  )

  const requestOp = useCallback((kind, uids) => {
    const targets = uids ? items.filter((i) => uids.includes(i.uid)) : selectedItems
    if (!targets.length) return
    setOp({ kind, items: targets })
  }, [items, selectedItems])

  useEffect(() => { setRequestOp(requestOp) }, [requestOp, setRequestOp])

  const facetDefs = useMemo(() => {
    const data = facets.data
    if (!data) return []
    return [
      buildFacet('category', 'Category', data.category, 8),
      buildFacet('base_model', 'Base', data.base_model, 8),
      buildFacet('precision', 'Precision', data.precision, 6),
      buildFacet('hash_state', 'Hash', data.hash_state, 6)
    ].filter((f) => f.values.length)
  }, [facets.data, buildFacet])

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the model list"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (view.q || activeFilters.length ? (
    <EmptyState icon={Search} title="Nothing matches"
      text="No model matches the current search and filters."
      actions={<Button onClick={() => { patch({ q: '' }); clearFilters() }}>Clear the filters</Button>} />
  ) : (
    <EmptyState icon={Box} title="No models indexed"
      text="Nothing has been read from the models folder yet. Run a scan to fill the vault."
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
        sorts={SORTS.models}
        groupOptions={GROUPS.models}
        facets={facetDefs}
        activeFilters={activeFilters}
        onSetFilter={setFilter}
        onClearFilters={clearFilters}
        selectionCount={view.selection.length}
        resultLabel={page ? page.total + ' models' : null}
        actions={(
          <SelectionActions
            count={view.selection.length}
            onRename={() => requestOp('rename')}
            onMove={() => requestOp('move')}
            onDelete={() => requestOp('delete')}
            onRefresh={list.refresh}
            extra={(
              <Button size="sm" icon={Hash} label="Hash"
                disabled={!view.selection.length}
                title={view.selection.length
                  ? 'Hash the ' + view.selection.length + ' selected file(s)'
                  : 'Select files to hash them'}
                onClick={() => dispatch({
                  type: 'open-modal', name: 'hash', props: { presetUids: view.selection }
                })} />
            )}
          />
        )}
      />

      <AssetGrid
        scrollRef={scrollRef}
        items={items}
        groups={groups}
        scope="models"
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
        loading={list.loading}
        empty={empty}
      />

      {op ? (
        <FileOpsModal op={op.kind} items={op.items} scope="models"
          onClose={() => setOp(null)}
          onCompleted={() => patch(
            { selection: [], focusUid: null, detailUid: null }, { keepOffset: true })} />
      ) : null}
    </>
  )
}
