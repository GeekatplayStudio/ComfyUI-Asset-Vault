import React, { useMemo, useCallback, useState, useEffect } from 'react'
import { Image as ImageIcon, AlertTriangle, Search, Star } from 'lucide-react'
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
 * OutputsTab - every generated image, video, audio clip and 3D asset.
 * Clicking a tile opens the full size preview; the grid itself never loads a
 * full resolution file.
 */
export default function OutputsTab({ onStatus, onOpenUid, onLightbox, registerApi }) {
  const { state, dispatch } = useVault()
  const [op, setOp] = useState(null)

  const open = useCallback((item) => onLightbox(item.uid), [onLightbox])

  const tab = useAssetTab({
    tab: 'outputs',
    scope: 'outputs',
    loader: (query, signal) => api.outputs(query, signal),
    onStatus,
    onOpen: open,
    registerApi
  })

  const {
    view, patch, setFilter, clearFilters, scrollRef, list, items, groups, page,
    selection, selectedItems, groupKeyOf, select, toggleCheck, activeFilters,
    buildFacet, buildBoolFacet, setRequestOp
  } = tab

  const kindFacet = useResource(
    'output-kinds:' + JSON.stringify(view.filters),
    (s) => api.outputs({ ...view.filters, group: 'media_kind', limit: 1 }, s),
    { epoch: state.dataEpoch }
  )

  const requestOp = useCallback((kind, uids) => {
    const targets = uids ? items.filter((i) => uids.includes(i.uid)) : selectedItems
    if (!targets.length) return
    setOp({ kind, items: targets })
  }, [items, selectedItems])

  useEffect(() => { setRequestOp(requestOp) }, [requestOp, setRequestOp])

  const favouriteSelection = useCallback(async () => {
    if (!view.selection.length) return
    await api.bulkOutputs(view.selection, { favorite: true })
    list.refresh()
  }, [view.selection, list])

  const facetDefs = useMemo(() => {
    const kinds = ((kindFacet.data && kindFacet.data.groups) || [])
      .map((g) => ({ value: g.key, label: g.label || g.key, count: g.count }))
    const defs = []
    if (kinds.length) defs.push(buildFacet('media_kind', 'Media', kinds, 8))
    defs.push(buildBoolFacet('favorite', 'Marked', [{ value: true, label: 'Favourites' }]))
    return defs
  }, [kindFacet.data, buildFacet, buildBoolFacet])

  const empty = list.error ? (
    <EmptyState tone="error" icon={AlertTriangle} title="Could not load the outputs"
      text={list.error.message}
      actions={<Button variant="primary" onClick={list.refresh}>Try again</Button>} />
  ) : (view.q || activeFilters.length ? (
    <EmptyState icon={Search} title="Nothing matches"
      text="No output matches the current search and filters."
      actions={<Button onClick={() => { patch({ q: '' }); clearFilters() }}>Clear the filters</Button>} />
  ) : (
    <EmptyState icon={ImageIcon} title="No outputs indexed"
      text="Nothing has been read from the output folder yet. Run a scan to fill the vault."
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
        sorts={SORTS.outputs}
        groupOptions={GROUPS.outputs}
        facets={facetDefs}
        activeFilters={activeFilters}
        onSetFilter={setFilter}
        onClearFilters={clearFilters}
        selectionCount={view.selection.length}
        resultLabel={page ? page.total + ' outputs' : null}
        actions={(
          <SelectionActions
            count={view.selection.length}
            onRename={() => requestOp('rename')}
            onMove={() => requestOp('move')}
            onDelete={() => requestOp('delete')}
            onRefresh={list.refresh}
            extra={(
              <Button size="sm" icon={Star} label="Favourite"
                disabled={!view.selection.length}
                title={view.selection.length
                  ? 'Mark ' + view.selection.length + ' output(s) as favourites'
                  : 'Select outputs to mark them'}
                onClick={favouriteSelection} />
            )}
          />
        )}
      />

      <AssetGrid
        scrollRef={scrollRef}
        items={items}
        groups={groups}
        scope="outputs"
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
        <FileOpsModal op={op.kind} items={op.items} scope="outputs"
          onClose={() => setOp(null)}
          onCompleted={() => patch(
            { selection: [], focusUid: null, detailUid: null }, { keepOffset: true })} />
      ) : null}
    </>
  )
}
