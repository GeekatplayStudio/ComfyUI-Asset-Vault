import React, { useMemo, useCallback } from 'react'
import {
  Folder, Layers, Star, Clock, Hash, ArrowUp, AlertTriangle, Image as ImageIcon,
  Film, Music, Boxes, FileText, Users, ShieldCheck, CheckCircle2, XCircle,
  Gauge, Copy, Trash2, Package, HardDrive, ShieldQuestion, Coffee, Globe, Youtube
} from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Tree from '../common/Tree.jsx'
import { useVault, useTabView } from '../../state/VaultContext.jsx'
import { STORAGE_SECTIONS } from '../../state/actions.js'
import { humanise } from '../../services/format.js'
import { AUTHOR, SUPPORT_URL, SITE_URL, CHANNELS, copyright } from '../../services/links.js'

/*
 * LeftRail - the album / group tree with live counts. Selecting a node writes
 * one filter into the active tab's view, which is what the next list request
 * is built from.
 */

const ALBUM_ICONS = {
  layers: Layers, clock: Clock, star: Star, hash: Hash, 'arrow-up': ArrowUp,
  folder: Folder, alert: AlertTriangle
}

const MEDIA_ICONS = {
  image: ImageIcon, video: Film, audio: Music, model3d: Boxes, text: FileText
}

/** Build a rail section from a list endpoint's groups[] response. */
function useGroupSection(scope, field, loader, epoch, enabled) {
  const key = enabled ? 'rail:' + scope + ':' + field : null
  return useResource(key, loader, { epoch })
}

function toNodes(groups, iconOf) {
  return (groups || [])
    .filter((g) => g.count > 0)
    .map((g) => ({
      key: String(g.key),
      label: g.label === '' || g.label === null || g.label === undefined
        ? '(none)'
        : g.label,
      count: g.count,
      bytes: g.bytes,
      icon: iconOf ? iconOf(g) : undefined,
      children: (g.children || []).map((c) => ({
        key: String(c.key),
        label: c.label,
        count: c.count,
        bytes: c.bytes,
        icon: iconOf ? iconOf(c) : undefined,
        children: []
      }))
    }))
}

const STORAGE_ICONS = {
  overview: Gauge, cleanup: HardDrive, duplicates: Copy, trash: Trash2, comfyui: Package
}

export default function LeftRail() {
  const { state } = useVault()
  const tab = state.tab
  const { view, patch, setFilter } = useTabView(tab)
  const epoch = state.dataEpoch

  /* ---------------------------------------------------------------- models */
  const modelFolders = useGroupSection('models', 'folder',
    (s) => api.modelGroups({ group: 'folder' }, s), epoch, tab === 'models')
  const modelBase = useGroupSection('models', 'base_model',
    (s) => api.models({ group: 'base_model', limit: 1 }, s), epoch, tab === 'models')

  /* ----------------------------------------------------------------- nodes */
  const nodeAuthors = useGroupSection('nodes', 'author',
    (s) => api.nodePackages({ group: 'author', limit: 1 }, s), epoch, tab === 'nodes')
  const classCategories = useGroupSection('nodes', 'category',
    (s) => api.nodeClasses({ group: 'category', limit: 1 }, s), epoch, tab === 'nodes')

  /* ------------------------------------------------------------- workflows */
  const workflowFolders = useGroupSection('workflows', 'folder',
    (s) => api.workflows({ group: 'folder', limit: 1 }, s), epoch, tab === 'workflows')
  const workflowBase = useGroupSection('workflows', 'base_model',
    (s) => api.workflows({ group: 'base_model', limit: 1 }, s), epoch, tab === 'workflows')

  /* --------------------------------------------------------------- outputs */
  const outputFolders = useGroupSection('outputs', 'folder',
    (s) => api.outputs({ group: 'folder', limit: 1 }, s), epoch, tab === 'outputs')
  const outputKinds = useGroupSection('outputs', 'media_kind',
    (s) => api.outputs({ group: 'media_kind', limit: 1 }, s), epoch, tab === 'outputs')
  const outputDates = useGroupSection('outputs', 'date',
    (s) => api.outputs({ group: 'date', limit: 1 }, s), epoch, tab === 'outputs')

  /* --------------------------------------------------------------- storage */
  const storageSummary = useResource(
    tab === 'storage' ? 'rail:storage:summary' : null,
    (s) => api.storageSummary(null, s),
    { epoch }
  )

  /* ---------------------------------------------------------------- albums */
  const albumScope = tab === 'models' ? 'models' : (tab === 'outputs' ? 'outputs' : 'all')
  const albums = useResource(
    tab === 'storage' ? null : 'rail:albums:' + albumScope,
    (s) => api.albums(albumScope, s),
    { epoch }
  )

  const select = useCallback((field, node, extra) => {
    const railKey = field + ':' + node.key
    if (view.railKey === railKey) {
      patch({ railKey: null })
      setFilter(field, null)
      return
    }
    patch({ railKey })
    setFilter(field, node.key === '' ? null : node.key)
    if (extra) extra(node)
  }, [view.railKey, patch, setFilter])

  const selectAlbum = useCallback((node) => {
    const railKey = 'album:' + node.key
    if (view.railKey === railKey) {
      patch({ railKey: null, filters: {} })
      return
    }
    patch({ railKey, filters: node.query || {} })
  }, [view.railKey, patch])

  const albumNodes = useMemo(() => {
    const nodes = (albums.data && albums.data.nodes) || []
    const walk = (list) => list.map((a) => ({
      key: String(a.id),
      label: a.name,
      count: a.item_count || null,
      icon: ALBUM_ICONS[a.icon] || Layers,
      query: a.query,
      title: a.editable ? a.name : a.name + ' (built in)',
      children: walk(a.children || [])
    }))
    return walk(nodes)
  }, [albums.data])

  const storageNodes = useMemo(() => STORAGE_SECTIONS.map((sct) => ({
    key: sct.id,
    label: sct.label,
    icon: STORAGE_ICONS[sct.id],
    children: []
  })), [])

  /* One node per reclaim group that actually has members, carrying its own byte
     total - the rail is where "which pile is biggest" gets answered. */
  const reclaimNodes = useMemo(() => {
    const groups = (storageSummary.data && storageSummary.data.reclaim
      && storageSummary.data.reclaim.groups) || []
    const byReason = new Map()
    for (const g of groups) {
      if (!g.reason || !g.count) continue
      const hit = byReason.get(g.reason)
      if (hit) {
        hit.count += g.count
        hit.bytes += g.bytes || 0
      } else {
        byReason.set(g.reason, {
          key: g.reason,
          label: humanise(g.reason),
          count: g.count,
          bytes: g.bytes || 0,
          icon: g.confidence === 'inferred' ? ShieldQuestion : undefined,
          title: g.label + ' - ' + g.confidence,
          children: []
        })
      }
    }
    return Array.from(byReason.values()).sort((a, b) => b.bytes - a.bytes)
  }, [storageSummary.data])

  const sections = []

  if (tab === 'models') {
    sections.push({
      id: 'folders',
      title: 'Folders',
      nodes: toNodes(modelFolders.data && modelFolders.data.nodes, () => Folder),
      field: 'category',
      loading: modelFolders.loading
    })
    sections.push({
      id: 'base',
      title: 'Base model',
      nodes: toNodes(modelBase.data && modelBase.data.groups),
      field: 'base_model',
      loading: modelBase.loading
    })
  } else if (tab === 'nodes') {
    sections.push({
      id: 'source',
      title: 'Source',
      nodes: [
        { key: 'true', label: 'Official ComfyUI', icon: ShieldCheck, children: [] },
        { key: 'false', label: 'Custom packages', icon: Users, children: [] }
      ],
      field: 'official',
      loading: false
    })
    sections.push({
      id: 'authors',
      title: 'Authors',
      nodes: toNodes(nodeAuthors.data && nodeAuthors.data.groups, () => Users),
      field: 'author',
      loading: nodeAuthors.loading
    })
    if (view.mode === 'classes') {
      sections.push({
        id: 'categories',
        title: 'Class categories',
        nodes: toNodes(classCategories.data && classCategories.data.groups),
        field: 'category',
        loading: classCategories.loading
      })
    }
  } else if (tab === 'workflows') {
    sections.push({
      id: 'state',
      title: 'State',
      nodes: [
        { key: 'true', label: 'Runnable', icon: CheckCircle2, children: [] },
        { key: 'false', label: 'Missing dependencies', icon: XCircle, children: [] }
      ],
      field: 'runnable',
      loading: false
    })
    sections.push({
      id: 'folders',
      title: 'Folders',
      nodes: toNodes(workflowFolders.data && workflowFolders.data.groups, () => Folder),
      field: 'folder',
      loading: workflowFolders.loading
    })
    sections.push({
      id: 'base',
      title: 'Base model',
      nodes: toNodes(workflowBase.data && workflowBase.data.groups),
      field: 'base_model',
      loading: workflowBase.loading
    })
  } else if (tab === 'outputs') {
    sections.push({
      id: 'kind',
      title: 'Media',
      nodes: toNodes(outputKinds.data && outputKinds.data.groups,
        (g) => MEDIA_ICONS[g.key] || FileText),
      field: 'media_kind',
      loading: outputKinds.loading
    })
    sections.push({
      id: 'folders',
      title: 'Folders',
      nodes: toNodes(outputFolders.data && outputFolders.data.groups, () => Folder),
      field: 'folder',
      loading: outputFolders.loading
    })
    sections.push({
      id: 'dates',
      title: 'When',
      nodes: toNodes(outputDates.data && outputDates.data.groups, () => Clock),
      field: '__date__',
      loading: outputDates.loading
    })
  }

  const railTitle = tab === 'storage' ? 'Storage' : humanise(tab)
  const railSuffix = tab === 'storage' ? ' - sections' : ' - groups'

  return (
    <aside className="gp-rail">
      <div className="gp-rail__header">
        <span className="gp-rail__title">{railTitle}{railSuffix}</span>
        <button
          type="button"
          className="gp-btn gp-btn--ghost gp-btn--sm"
          onClick={() => { patch({ railKey: null, filters: {} }) }}
          title="Show everything in this tab"
        >
          <span className="gp-btn__label">All</span>
        </button>
      </div>

      <div className="gp-rail__body">
        {tab === 'storage' ? (
          <>
            <div className="gp-rail__section">
              <div className="gp-rail__section-head">View</div>
              <Tree
                label="Storage sections"
                nodes={storageNodes}
                selectedKey={view.section || 'overview'}
                onSelect={(node) => patch({ section: node.key, selection: [], offset: 0 })}
              />
            </div>
            {reclaimNodes.length ? (
              <div className="gp-rail__section">
                <div className="gp-rail__section-head">Reclaimable</div>
                <Tree
                  label="Reclaim groups"
                  nodes={reclaimNodes}
                  selectedKey={view.railKey && view.railKey.startsWith('reason:')
                    ? view.railKey.slice(7) : null}
                  onSelect={(node) => {
                    const railKey = 'reason:' + node.key
                    if (view.railKey === railKey) {
                      patch({ railKey: null, filters: {}, section: 'cleanup' })
                      return
                    }
                    patch({
                      railKey,
                      filters: { reason: [node.key] },
                      section: 'cleanup',
                      selection: [],
                      offset: 0
                    })
                  }}
                />
              </div>
            ) : null}
          </>
        ) : null}

        {albumNodes.length ? (
          <div className="gp-rail__section">
            <div className="gp-rail__section-head">Albums</div>
            <Tree
              label="Albums"
              nodes={albumNodes}
              selectedKey={view.railKey && view.railKey.startsWith('album:')
                ? view.railKey.slice(6) : null}
              onSelect={selectAlbum}
            />
          </div>
        ) : null}

        {sections.map((section) => (
          section.nodes.length ? (
            <div className="gp-rail__section" key={section.id}>
              <div className="gp-rail__section-head">{section.title}</div>
              <Tree
                label={section.title}
                nodes={section.nodes}
                selectedKey={view.railKey && view.railKey.startsWith(section.field + ':')
                  ? view.railKey.slice(section.field.length + 1) : null}
                onSelect={(node) => {
                  if (section.field === '__date__') {
                    // Date buckets are a grouping, not a filter: switch the
                    // grouping instead of narrowing the result set.
                    patch({ group: 'date', railKey: '__date__:' + node.key })
                    return
                  }
                  select(section.field, node)
                }}
              />
            </div>
          ) : null
        ))}

        {!sections.length && !albumNodes.length && tab !== 'storage' ? (
          <p className="gp-u-fs-11 gp-u-meta gp-u-p-5">
            No groups yet. Run a scan to populate the vault.
          </p>
        ) : null}
      </div>

      <div className="gp-rail__footer">
        <div className="gp-rail__footer-top">
          <span className="gp-rail__footer-name">{AUTHOR}</span>
          <span className="gp-rail__footer-ver">
            v{(state.info && state.info.version) || '2.1.0'}
          </span>
        </div>

        <nav className="gp-rail__links" aria-label="Geekatplay links">
          <a className="gp-rail__link" href={SUPPORT_URL}
            target="_blank" rel="noopener noreferrer"
            title="Support the author on Gumroad">
            <Coffee className="gp-rail__link-icon" aria-hidden="true" />
            <span>Buy me a coffee</span>
          </a>
          <a className="gp-rail__link" href={SITE_URL}
            target="_blank" rel="noopener noreferrer"
            title={AUTHOR + ' — portfolio and courses'}>
            <Globe className="gp-rail__link-icon" aria-hidden="true" />
            <span>vladimirchopine.com</span>
          </a>
          {CHANNELS.map((c) => (
            <a key={c.url} className="gp-rail__link" href={c.url}
              target="_blank" rel="noopener noreferrer"
              title={c.label + ' on YouTube'}>
              <Youtube className="gp-rail__link-icon" aria-hidden="true" />
              <span>{c.label}</span>
            </a>
          ))}
        </nav>

        <p className="gp-rail__copy">
          {copyright()}
          <br />
          <span className="gp-rail__copy-sub">
            {AUTHOR} — all rights reserved
          </span>
        </p>
      </div>
    </aside>
  )
}
