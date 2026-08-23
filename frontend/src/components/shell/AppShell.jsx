import React, { useState, useRef, useCallback, useEffect, useMemo, Suspense, lazy } from 'react'
import api, { streamUrl } from '../../services/api.js'
import useResizablePanel from '../../hooks/useResizablePanel.js'
import useKeyboardNav from '../../hooks/useKeyboardNav.js'
import useEventSource from '../../hooks/useEventSource.js'
import TopBar from './TopBar.jsx'
import LeftRail from './LeftRail.jsx'
import StatusBar from './StatusBar.jsx'
import DetailsPanel from './DetailsPanel.jsx'
import ErrorBoundary from '../common/ErrorBoundary.jsx'
import Toaster from '../common/Toast.jsx'
import ModelsTab from '../tabs/ModelsTab.jsx'
import NodesTab from '../tabs/NodesTab.jsx'
import WorkflowsTab from '../tabs/WorkflowsTab.jsx'
import OutputsTab from '../tabs/OutputsTab.jsx'
import Lightbox from '../modals/Lightbox.jsx'
import SettingsModal from '../modals/SettingsModal.jsx'
import HealthDrawer from '../modals/HealthDrawer.jsx'
import HashDialog from '../modals/HashDialog.jsx'
import IndexProgress from '../modals/IndexProgress.jsx'
import FirstLaunchWizard from '../modals/FirstLaunchWizard.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { parseUid } from '../../services/format.js'

/* Storage is a secondary workspace with its own panels, dialogs and stream
   handling. Splitting it keeps the first paint - the four asset tabs - inside
   the 400 kB chunk budget vite.config.js sets. */
const StorageTab = lazy(() => import('../tabs/StorageTab.jsx'))

function TabFallback() {
  return (
    <>
      <div className="gp-toolbar">
        <span className="gp-toolbar__label">Storage and maintenance</span>
      </div>
      <div className="gp-facetbar gp-facetbar--empty" />
      <div className="gp-main__body"><SkeletonRows rows={10} /></div>
    </>
  )
}

/*
 * AppShell - the five-region grid from DECISIONS C3, the global shortcut map,
 * and the modal slots.
 *
 * The only appearance values written at runtime are the three geometry custom
 * properties the design system sanctions: the grid tile size and the two panel
 * widths, all on .gp-shell.
 */

const TAB_TO_VIEW = {
  models: 'models', nodes: 'nodes', workflows: 'workflows', outputs: 'outputs',
  storage: 'storage'
}

export default function AppShell() {
  const { state, dispatch, toast, refreshStats } = useVault()
  const [status, setStatus] = useState(null)
  const [resizing, setResizing] = useState(false)
  const tabApi = useRef({})
  const searchRef = useRef(null)

  const tab = state.tab
  const viewKey = TAB_TO_VIEW[tab] || 'models'
  const view = state.views[viewKey]

  /* ------------------------------------------------------- live job status */
  const indexEvents = useMemo(() => ['phase', 'progress', 'done', 'heartbeat'], [])
  useEventSource(streamUrl('index'), {
    enabled: true,
    events: indexEvents,
    onEvent: (name, payload) => {
      if (name === 'poll') {
        dispatch({ type: 'set-index-status', status: payload })
      } else if (name === 'phase' || name === 'progress') {
        dispatch({
          type: 'set-index-status',
          status: {
            active: true,
            job: {
              phase: payload.phase,
              items_done: payload.done || 0,
              items_total: payload.total || 0,
              eta_ms: payload.eta_ms,
              rate_per_sec: payload.rate,
              current: payload.current
            }
          }
        })
      } else if (name === 'done') {
        dispatch({ type: 'set-index-status', status: { active: false, job: null } })
        dispatch({ type: 'invalidate' })
        refreshStats()
      }
    },
    poll: () => api.indexStatus()
  })

  useEffect(() => {
    let alive = true
    api.searchStatus()
      .then((s) => { if (alive) dispatch({ type: 'set-search-status', status: s }) })
      .catch(() => {})
    api.hashStatus()
      .then((s) => { if (alive) dispatch({ type: 'set-hash-status', status: s }) })
      .catch(() => {})
    return () => { alive = false }
  }, [dispatch, state.dataEpoch])

  /* ------------------------------------------------------------- resizers */
  const setRailWidth = useCallback(
    (value) => dispatch({ type: 'set-panel', key: 'railWidth', value }), [dispatch]
  )
  const setDetailsWidth = useCallback(
    (value) => dispatch({ type: 'set-panel', key: 'detailsWidth', value }), [dispatch]
  )
  const leftResizer = useResizablePanel({
    value: state.railWidth, onChange: setRailWidth, min: 200, max: 420, side: 'left',
    onDragChange: setResizing
  })
  const rightResizer = useResizablePanel({
    value: state.detailsWidth, onChange: setDetailsWidth, min: 280, max: 520, side: 'right',
    onDragChange: setResizing
  })

  /* -------------------------------------------------------------- actions */
  const patchView = useCallback((patch, opts) => {
    dispatch({
      type: 'patch-view',
      tab: viewKey,
      patch,
      resetOffset: opts && opts.keepOffset ? false : undefined
    })
  }, [dispatch, viewKey])

  const openUid = useCallback((uid) => {
    const { kind } = parseUid(uid)
    const targetTab = kind === 'model' ? 'models'
      : (kind === 'workflow' ? 'workflows'
        : (kind === 'output' ? 'outputs' : 'nodes'))
    dispatch({ type: 'set-tab', tab: targetTab })
    if (kind === 'node_class') {
      dispatch({ type: 'patch-view', tab: 'nodes', patch: { mode: 'classes', scope: 'node_classes', sort: 'display_name' } })
    } else if (kind === 'node_package') {
      dispatch({ type: 'patch-view', tab: 'nodes', patch: { mode: 'packages', scope: 'node_packages', sort: 'name' } })
    }
    dispatch({
      type: 'patch-view',
      tab: TAB_TO_VIEW[targetTab],
      patch: { detailUid: uid, focusUid: uid },
      resetOffset: false
    })
    dispatch({ type: 'set-panel', key: 'detailsOpen', value: true })
  }, [dispatch])

  const openLightbox = useCallback((uid, items) => {
    dispatch({
      type: 'open-lightbox',
      payload: { uid, items: items || tabApi.current.items || [] }
    })
  }, [dispatch])

  const closeLightbox = useCallback(() => dispatch({ type: 'close-lightbox' }), [dispatch])

  const openModal = useCallback((name, props) => {
    dispatch({ type: 'open-modal', name, props })
  }, [dispatch])

  const closeModal = useCallback(() => dispatch({ type: 'close-modal' }), [dispatch])

  const registerApi = useCallback((apiObject) => {
    tabApi.current = apiObject
  }, [])

  const onStatus = useCallback((next) => setStatus(next), [])

  /* ------------------------------------------------------------ shortcuts */
  useKeyboardNav({
    onFocusSearch: () => { if (searchRef.current) searchRef.current.focus() },
    onEscape: () => {
      if (state.lightbox) { closeLightbox(); return }
      if (state.modal) { closeModal(); return }
      if (tabApi.current.clearSelection) tabApi.current.clearSelection()
    },
    onSelectAll: () => { if (tabApi.current.selectAll) tabApi.current.selectAll() },
    onDelete: () => { if (tabApi.current.requestOp) tabApi.current.requestOp('delete') },
    onOpen: () => { if (tabApi.current.openFocused) tabApi.current.openFocused() },
    onMove: (delta) => { if (tabApi.current.move) tabApi.current.move(delta) },
    onReindex: () => openModal('index', { autoStart: false })
  }, !state.lightbox)

  /* ------------------------------------------------------------ rendering */
  const smartStatus = state.searchStatus && state.searchStatus.semantic
  const smartAvailable = Boolean(smartStatus && smartStatus.available)
  const smartReason = smartStatus ? smartStatus.reason : 'unknown'

  const shellClasses = ['gp-shell']
  if (!state.railOpen) shellClasses.push('gp-shell--no-rail')
  if (!state.detailsOpen) shellClasses.push('gp-shell--no-details')
  if (resizing) shellClasses.push('gp-shell--resizing')

  const tabProps = {
    onStatus,
    onOpenUid: openUid,
    onLightbox: openLightbox,
    registerApi
  }

  let main = null
  if (tab === 'models') main = <ModelsTab {...tabProps} />
  else if (tab === 'nodes') main = <NodesTab {...tabProps} />
  else if (tab === 'workflows') main = <WorkflowsTab {...tabProps} />
  else if (tab === 'outputs') main = <OutputsTab {...tabProps} />
  else {
    main = (
      <Suspense fallback={<TabFallback />}>
        <StorageTab {...tabProps} />
      </Suspense>
    )
  }

  return (
    <div
      className={shellClasses.join(' ')}
      style={{
        '--gp-grid-size': view.view === 'list' ? '100%' : (view.tile || 180) + 'px',
        '--gp-rail-w': state.railWidth + 'px',
        '--gp-details-w': state.detailsWidth + 'px'
      }}
    >
      <TopBar
        tab={tab}
        onTab={(next) => dispatch({ type: 'set-tab', tab: next })}
        stats={state.stats}
        query={view.q}
        onQuery={(q) => patchView({ q })}
        onPickSuggestion={(parsed, item) => { if (item.uid) openUid(item.uid) }}
        smart={view.smart}
        onSmart={(v) => patchView({ smart: v })}
        smartAvailable={smartAvailable}
        smartReason={smartReason}
        searchRef={searchRef}
        indexing={Boolean(state.indexStatus && state.indexStatus.active)}
        onSettings={() => openModal('settings')}
        onHealth={() => openModal('health')}
        onReindex={() => openModal('index', { autoStart: false })}
        onHash={() => openModal('hash', { presetUids: view.selection })}
      />

      <ErrorBoundary title="The groups rail hit an error" small>
        <LeftRail />
      </ErrorBoundary>

      <div
        className={'gp-resizer gp-resizer--left' + (resizing ? ' gp-resizer--dragging' : '')}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the groups rail"
        tabIndex={0}
        onPointerDown={leftResizer.onPointerDown}
        onKeyDown={leftResizer.onKeyDown}
      />

      <main className="gp-main">
        <ErrorBoundary title="This tab hit an error">
          {main}
        </ErrorBoundary>
      </main>

      <div
        className={'gp-resizer gp-resizer--right' + (resizing ? ' gp-resizer--dragging' : '')}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the details panel"
        tabIndex={0}
        onPointerDown={rightResizer.onPointerDown}
        onKeyDown={rightResizer.onKeyDown}
      />

      <DetailsPanel
        uid={view.detailUid}
        onOpenUid={openUid}
        onLightbox={openLightbox}
        onRename={(uid) => tabApi.current.requestOp && tabApi.current.requestOp('rename', [uid])}
        onMove={(uid) => tabApi.current.requestOp && tabApi.current.requestOp('move', [uid])}
        onDelete={(uid) => tabApi.current.requestOp && tabApi.current.requestOp('delete', [uid])}
        onFilterPackage={(pkg) => {
          dispatch({
            type: 'patch-view',
            tab: 'nodes',
            patch: {
              mode: 'classes',
              scope: 'node_classes',
              sort: 'display_name',
              filters: { package_id: pkg.id },
              railKey: null
            }
          })
        }}
      />

      <StatusBar
        page={status && status.page}
        elapsed={status && status.elapsed}
        mode={status && status.mode}
        selectionCount={view.selection.length}
        view={view}
        patch={patchView}
        indexStatus={state.indexStatus}
        hashStatus={state.hashStatus}
        stats={state.stats}
        railOpen={state.railOpen}
        detailsOpen={state.detailsOpen}
        onToggleRail={() => dispatch({ type: 'set-panel', key: 'railOpen', value: !state.railOpen })}
        onToggleDetails={() => dispatch({ type: 'set-panel', key: 'detailsOpen', value: !state.detailsOpen })}
      />

      {state.lightbox ? (
        <Lightbox
          uid={state.lightbox.uid}
          items={state.lightbox.items}
          onClose={closeLightbox}
          onNavigate={(uid) => dispatch({ type: 'open-lightbox', payload: { uid, items: state.lightbox.items } })}
          onOpenUid={openUid}
        />
      ) : null}

      {state.modal && state.modal.name === 'settings' ? (
        <SettingsModal
          onClose={closeModal}
          onReindex={(mode) => openModal('index', { autoStart: true, mode })}
          onWizard={() => openModal('wizard')}
          {...state.modal.props}
        />
      ) : null}

      {state.modal && state.modal.name === 'health' ? (
        <HealthDrawer onClose={closeModal} onOpenUid={openUid} />
      ) : null}

      {state.modal && state.modal.name === 'hash' ? (
        <HashDialog onClose={closeModal} {...state.modal.props} />
      ) : null}

      {state.modal && state.modal.name === 'index' ? (
        <IndexProgress onClose={closeModal} {...state.modal.props} />
      ) : null}

      {state.modal && state.modal.name === 'wizard' ? (
        <div className="gp-overlay">
          <FirstLaunchWizard
            initialPath={state.config && state.config.comfyui_path}
            onCancel={closeModal}
            onDone={(res) => {
              closeModal()
              if (res && res.scan_started) openModal('index', { autoStart: false })
              else toast({ tone: 'ok', title: 'Setup saved' })
            }}
          />
        </div>
      ) : null}

      <Toaster
        toasts={state.toasts}
        onDismiss={(id) => dispatch({ type: 'dismiss-toast', id })}
      />
    </div>
  )
}
