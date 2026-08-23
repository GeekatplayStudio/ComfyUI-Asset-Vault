/* actions.js - the reducer vocabulary plus the per-tab view defaults.
   Every value here maps onto a query parameter documented in API_CONTRACT 0.4. */

export const TABS = [
  { id: 'models', label: 'Models', statKey: 'models' },
  { id: 'nodes', label: 'Nodes', statKey: 'node_packages' },
  { id: 'workflows', label: 'Workflows', statKey: 'workflows' },
  { id: 'outputs', label: 'Outputs', statKey: 'outputs' },
  // C10 - Storage and Maintenance. No stat key: this tab counts bytes, not rows,
  // and its own header carries the footprint.
  { id: 'storage', label: 'Storage', statKey: null }
]

export const TAB_IDS = TABS.map((t) => t.id)

/** Sort vocabulary, verbatim from API_CONTRACT 0.4. */
export const SORTS = {
  models: [
    { value: 'name', label: 'Name A-Z' },
    { value: '-name', label: 'Name Z-A' },
    { value: '-size', label: 'Largest first' },
    { value: 'size', label: 'Smallest first' },
    { value: '-modified', label: 'Recently modified' },
    { value: '-created', label: 'Recently added' },
    { value: 'category', label: 'Category' },
    { value: 'base_model', label: 'Base model' },
    { value: 'role', label: 'Role' },
    { value: '-params', label: 'Parameters' },
    { value: '-rating', label: 'Rating' },
    { value: 'hash_state', label: 'Hash state' }
  ],
  node_packages: [
    { value: 'name', label: 'Name A-Z' },
    { value: '-name', label: 'Name Z-A' },
    { value: 'author', label: 'Author' },
    { value: '-classes', label: 'Most classes' },
    { value: '-updated', label: 'Recently updated' },
    { value: '-size', label: 'Largest first' }
  ],
  node_classes: [
    { value: 'display_name', label: 'Display name' },
    { value: '-display_name', label: 'Display name Z-A' },
    { value: 'name', label: 'Class name' },
    { value: 'category', label: 'Category' },
    { value: 'package', label: 'Package' }
  ],
  workflows: [
    { value: '-modified', label: 'Recently modified' },
    { value: 'modified', label: 'Oldest first' },
    { value: 'name', label: 'Name A-Z' },
    { value: '-name', label: 'Name Z-A' },
    { value: '-nodes', label: 'Most nodes' },
    { value: '-missing', label: 'Most missing deps' },
    { value: '-size', label: 'Largest first' }
  ],
  outputs: [
    { value: '-created', label: 'Newest first' },
    { value: 'created', label: 'Oldest first' },
    { value: '-modified', label: 'Recently modified' },
    { value: 'name', label: 'Name A-Z' },
    { value: '-name', label: 'Name Z-A' },
    { value: '-size', label: 'Largest first' },
    { value: '-rating', label: 'Rating' },
    { value: '-width', label: 'Widest' },
    { value: '-height', label: 'Tallest' },
    { value: '-duration', label: 'Longest' }
  ],
  /* API_CONTRACT 18: one query, three first-class orderings (C10.5). */
  storage: [
    { value: 'reclaim', label: 'Reclaim score' },
    { value: 'size', label: 'Largest first' },
    { value: 'age', label: 'Oldest first' },
    { value: 'name', label: 'Name A-Z' }
  ]
}

/** Group vocabulary, verbatim from API_CONTRACT 0.4. */
export const GROUPS = {
  models: ['none', 'category', 'base_model', 'role', 'folder', 'precision', 'root',
    'hash_state', 'integrity', 'first_letter', 'date'],
  node_packages: ['none', 'author', 'official', 'enabled', 'update_state'],
  node_classes: ['none', 'category', 'package'],
  workflows: ['none', 'folder', 'base_model', 'runnable', 'date'],
  outputs: ['none', 'folder', 'date', 'model', 'media_kind', 'album', 'first_letter']
}

/* C10/C11 progressive disclosure: the storage tab is one workspace with five
   layers, navigated from the rail exactly like an album. Summary first. */
export const STORAGE_SECTIONS = [
  { id: 'overview', label: 'Overview' },
  { id: 'cleanup', label: 'Cleanup candidates' },
  { id: 'duplicates', label: 'Duplicates' },
  { id: 'trash', label: 'Trash' },
  { id: 'comfyui', label: 'ComfyUI install' }
]

export const PAGE_SIZES = [50, 100, 200, 500]

export const TILE_MIN = 120
export const TILE_MAX = 420
export const TILE_DEFAULT = 180

function baseView(scope, sort) {
  return {
    scope,
    q: '',
    smart: false,
    sort,
    group: 'none',
    view: 'grid',
    tile: TILE_DEFAULT,
    limit: 100,
    offset: 0,
    filters: {},
    railKey: null,
    selection: [],
    focusUid: null,
    detailUid: null
  }
}

export const initialViews = {
  models: baseView('models', 'name'),
  nodes: { ...baseView('node_packages', 'name'), mode: 'packages' },
  workflows: baseView('workflows', '-modified'),
  outputs: { ...baseView('outputs', '-created'), view: 'grid', tile: 200 },
  /* Storage is a maintenance workspace, not a picture grid: it always lists,
     and `section` is the progressive-disclosure layer C11 asks for. */
  storage: {
    ...baseView('storage', 'reclaim'),
    view: 'list',
    section: 'overview',
    limit: 100,
    includeProtected: true,
    dupMethod: 'name+size'
  }
}

export const initialState = {
  ready: false,
  bootError: null,
  tab: 'models',
  views: initialViews,
  info: null,
  config: null,
  stats: null,
  indexStatus: null,
  hashStatus: null,
  searchStatus: null,
  modal: null,
  lightbox: null,
  toasts: [],
  railWidth: 264,
  detailsWidth: 340,
  railOpen: true,
  detailsOpen: true,
  dataEpoch: 0
}

let toastSeq = 0

export function reducer(state, action) {
  switch (action.type) {
    case 'boot':
      return {
        ...state,
        ready: true,
        bootError: null,
        info: action.info,
        config: action.config,
        stats: action.stats
      }
    case 'boot-error':
      return { ...state, ready: true, bootError: action.error }
    case 'set-tab': {
      if (!TAB_IDS.includes(action.tab)) return state
      return { ...state, tab: action.tab }
    }
    case 'patch-view': {
      const current = state.views[action.tab]
      if (!current) return state
      const next = { ...current, ...action.patch }
      // Any change to the result set resets paging unless explicitly paged.
      if (action.patch.offset === undefined && action.resetOffset !== false) next.offset = 0
      return { ...state, views: { ...state.views, [action.tab]: next } }
    }
    case 'set-filter': {
      const current = state.views[action.tab]
      if (!current) return state
      const filters = { ...current.filters }
      if (action.value === null || action.value === undefined ||
          (Array.isArray(action.value) && action.value.length === 0)) {
        delete filters[action.field]
      } else {
        filters[action.field] = action.value
      }
      return {
        ...state,
        views: { ...state.views, [action.tab]: { ...current, filters, offset: 0 } }
      }
    }
    case 'clear-filters': {
      const current = state.views[action.tab]
      if (!current) return state
      return {
        ...state,
        views: {
          ...state.views,
          [action.tab]: { ...current, filters: {}, railKey: null, offset: 0 }
        }
      }
    }
    case 'set-config':
      return { ...state, config: action.config }
    case 'set-stats':
      return { ...state, stats: action.stats }
    case 'set-index-status':
      return { ...state, indexStatus: action.status }
    case 'set-hash-status':
      return { ...state, hashStatus: action.status }
    case 'set-search-status':
      return { ...state, searchStatus: action.status }
    case 'open-modal':
      return { ...state, modal: { name: action.name, props: action.props || {} } }
    case 'close-modal':
      return { ...state, modal: null }
    case 'open-lightbox':
      return { ...state, lightbox: action.payload }
    case 'close-lightbox':
      return { ...state, lightbox: null }
    case 'toast': {
      toastSeq += 1
      const toast = { id: 'toast-' + toastSeq, ...action.toast }
      return { ...state, toasts: [...state.toasts, toast] }
    }
    case 'dismiss-toast':
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) }
    case 'set-panel':
      return { ...state, [action.key]: action.value }
    case 'invalidate':
      return { ...state, dataEpoch: state.dataEpoch + 1 }
    default:
      return state
  }
}

/* ------------------------------------------------------------------ grouping */

const MONTH_FMT = new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric' })

/** Match the server's date buckets so a group header lines up with groups[]. */
export function dateBucket(ts) {
  if (!ts) return 'Older'
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 86400000
  const weekday = (now.getDay() + 6) % 7
  const weekStart = today - weekday * 86400000
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime()
  if (ts >= today) return 'Today'
  if (ts >= yesterday) return 'Yesterday'
  if (ts >= weekStart) return 'This week'
  if (ts >= monthStart) return 'This month'
  return MONTH_FMT.format(new Date(ts))
}

function folderOf(item) {
  const rel = item.rel_path || ''
  const parts = String(rel).split(/[\\/]/)
  parts.pop()
  return parts.join('/')
}

/**
 * Build the accessor that maps one item onto its group key.
 * Returns null when grouping is off, which switches the grid to a single run.
 */
export function makeGroupKey(scope, group) {
  if (!group || group === 'none') return null
  if (scope === 'models') {
    switch (group) {
      case 'category': return (i) => i.category
      case 'base_model': return (i) => (i.base_model && i.base_model.family) || 'Unknown'
      case 'role': return (i) => i.role
      case 'folder': return folderOf
      case 'precision': return (i) => i.precision || 'unknown'
      case 'root': return (i) => i.root_id
      case 'hash_state': return (i) => (i.hash && i.hash.state) || 'unhashed'
      case 'integrity': return (i) => (typeof i.integrity === 'string'
        ? i.integrity : (i.integrity && i.integrity.status)) || 'ok'
      case 'first_letter': return (i) => (i.name || '').charAt(0).toUpperCase()
      case 'date': return (i) => dateBucket(i.modified_at)
      default: return null
    }
  }
  if (scope === 'node_packages') {
    switch (group) {
      case 'author': return (i) => i.author || ''
      case 'official': return (i) => (i.is_official ? 'Official' : 'Custom')
      case 'enabled': return (i) => (i.enabled ? 'Enabled' : 'Disabled')
      case 'update_state': return (i) => (i.update && i.update.state) || 'none'
      default: return null
    }
  }
  if (scope === 'node_classes') {
    switch (group) {
      case 'category': return (i) => i.category || ''
      case 'package': return (i) => (i.package && i.package.name) || ''
      default: return null
    }
  }
  if (scope === 'workflows') {
    switch (group) {
      case 'folder': return (i) => i.folder || ''
      case 'base_model': return (i) => i.base_model || 'Unknown'
      case 'runnable': return (i) => (i.is_runnable ? 'Runnable' : 'Missing dependencies')
      case 'date': return (i) => dateBucket(i.modified_at)
      default: return null
    }
  }
  if (scope === 'outputs') {
    switch (group) {
      case 'folder': return (i) => i.folder || ''
      case 'date': return (i) => dateBucket(i.created_at)
      case 'model': return (i) => i.model_name || 'Unknown model'
      case 'media_kind': return (i) => i.media_kind
      case 'album': return (i) => (i.album_id === null || i.album_id === undefined
        ? 'No album' : String(i.album_id))
      case 'first_letter': return (i) => (i.filename || '').charAt(0).toUpperCase()
      default: return null
    }
  }
  return null
}
