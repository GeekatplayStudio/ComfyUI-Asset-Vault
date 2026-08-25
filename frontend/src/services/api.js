/*
 * api.js - the ONLY place in the client that calls fetch().
 *
 * Responsibilities:
 *   - prefix every path with /api/v1
 *   - inject X-Vault-Request on every mutating verb (API_CONTRACT 0)
 *   - normalise the documented error envelope into a thrown ApiError
 *   - forward an AbortSignal so callers can cancel in-flight work
 *
 * Every route used here appears verbatim in docs/API_CONTRACT.md.
 */

export const API_BASE = '/api/v1'

const MUTATING = new Set(['POST', 'PATCH', 'PUT', 'DELETE'])

export class ApiError extends Error {
  constructor({ code, message, fieldErrors, requestId, status, details, retryable }) {
    super(message || code || 'Request failed')
    this.name = 'ApiError'
    this.code = code || 'INTERNAL'
    this.fieldErrors = fieldErrors || []
    this.requestId = requestId || null
    this.status = status || 0
    this.details = details || null
    this.retryable = Boolean(retryable)
  }

  /** Message for a specific form field, or null. */
  fieldError(field) {
    const hit = this.fieldErrors.find((f) => f.field === field)
    return hit ? hit.message : null
  }
}

export function isAbort(err) {
  return Boolean(err) && (err.name === 'AbortError' || err.code === 'ABORTED')
}

function buildQuery(params) {
  if (!params) return ''
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const v of value) {
        if (v === undefined || v === null || v === '') continue
        sp.append(key, String(v))
      }
    } else if (typeof value === 'boolean') {
      sp.append(key, value ? 'true' : 'false')
    } else {
      sp.append(key, String(value))
    }
  }
  const q = sp.toString()
  return q ? '?' + q : ''
}

async function request(method, path, options) {
  const { query, body, signal } = options || {}
  const headers = { Accept: 'application/json' }
  if (MUTATING.has(method)) headers['X-Vault-Request'] = '1'
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res
  try {
    res = await fetch(API_BASE + path + buildQuery(query), {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body)
    })
  } catch (err) {
    if (isAbort(err)) throw err
    throw new ApiError({
      code: 'NETWORK',
      message: 'The vault service is not reachable. Is the backend running on port 8127?',
      retryable: true
    })
  }

  if (res.status === 204) return null

  const ctype = res.headers.get('content-type') || ''
  const payload = ctype.indexOf('application/json') >= 0
    ? await res.json().catch(() => null)
    : null

  if (!res.ok) {
    const envelope = (payload && payload.error) || {}
    throw new ApiError({
      code: envelope.code,
      message: envelope.message || 'Request failed (' + res.status + ')',
      fieldErrors: envelope.field_errors,
      requestId: envelope.request_id,
      details: envelope.details,
      retryable: envelope.retryable,
      status: res.status
    })
  }
  return payload
}

const get = (path, query, signal) => request('GET', path, { query, signal })
const post = (path, body, query, signal) => request('POST', path, { body, query, signal })
const patch = (path, body, signal) => request('PATCH', path, { body, signal })
const del = (path, body, signal) => request('DELETE', path, { body, signal })

export const api = {
  /* ------------------------------------------------------------- system */
  ping: (signal) => get('/ping', null, signal),
  info: (signal) => get('/system/info', null, signal),
  config: (signal) => get('/system/config', null, signal),
  updateConfig: (body, signal) => patch('/system/config', body, signal),
  validatePath: (path, signal) => post('/system/validate-path', { path }, null, signal),
  completeWizard: (body, signal) => post('/system/wizard/complete', body, null, signal),
  stats: (signal) => get('/system/stats', null, signal),
  health: (signal) => get('/system/health', null, signal),
  roots: (signal) => get('/system/roots', null, signal),
  addRoot: (body, signal) => post('/system/roots', body, null, signal),
  removeRoot: (id, signal) => del('/system/roots/' + id, undefined, signal),
  thumbsGc: (maxMb, signal) => post('/system/thumbs/gc', { max_mb: maxMb }, null, signal),
  testOllama: (url, signal) => post('/system/ollama/test', { url }, null, signal),

  /* ----------------------------------------------------------- indexing */
  indexStart: (body, signal) => post('/index/start', body, null, signal),
  indexCancel: (jobId, signal) =>
    post('/index/cancel', { job_id: jobId === undefined ? null : jobId }, null, signal),
  indexStatus: (signal) => get('/index/status', null, signal),
  indexJobs: (query, signal) => get('/index/jobs', query, signal),
  indexErrors: (query, signal) => get('/index/errors', query, signal),

  /* ------------------------------------------------------------- models */
  models: (query, signal) => get('/models', query, signal),
  modelFacets: (query, signal) => get('/models/facets', query, signal),
  modelGroups: (query, signal) => get('/models/groups', query, signal),
  model: (id, signal) => get('/models/' + id, null, signal),
  modelUsage: (id, query, signal) => get('/models/' + id + '/usage', query, signal),
  modelRefreshMetadata: (id, force, signal) =>
    post('/models/' + id + '/refresh-metadata', { force: Boolean(force) }, null, signal),
  patchModel: (id, body, signal) => patch('/models/' + id, body, signal),
  bulkModels: (uids, body, signal) => post('/models/bulk', { uids, patch: body }, null, signal),

  /* -------------------------------------------------------------- nodes */
  nodePackages: (query, signal) => get('/node-packages', query, signal),
  nodePackage: (id, signal) => get('/node-packages/' + id, null, signal),
  nodePackageClasses: (id, query, signal) => get('/node-packages/' + id + '/classes', query, signal),
  nodeClasses: (query, signal) => get('/node-classes', query, signal),
  nodeClass: (id, signal) => get('/node-classes/' + id, null, signal),
  checkPackageUpdate: (id, signal) => post('/node-packages/' + id + '/check-update', {}, null, signal),
  checkPackageUpdates: (ids, signal) =>
    post('/node-packages/check-updates', { ids: ids === undefined ? null : ids }, null, signal),
  packageUpdateStatus: (signal) => get('/node-packages/update-status', null, signal),
  nodeRegistry: (query, signal) => get('/node-registry', query, signal),
  nodeRegistryStatus: (refresh, signal) => get('/node-registry/status', { refresh: Boolean(refresh) }, signal),

  /* ---------------------------------------------------------- workflows */
  workflows: (query, signal) => get('/workflows', query, signal),
  workflow: (id, signal) => get('/workflows/' + id, null, signal),
  workflowGraph: (id, format, signal) => get('/workflows/' + id + '/graph', { format }, signal),
  workflowDependencies: (id, signal) => get('/workflows/' + id + '/dependencies', null, signal),
  workflowEnablePlan: (id, signal) => get('/workflows/' + id + '/enable/plan', null, signal),
  workflowEnableFetch: (id, body, signal) => post('/workflows/' + id + '/enable/fetch', body, null, signal),

  /* ------------------------------------------------------------ outputs */
  outputs: (query, signal) => get('/outputs', query, signal),
  /** Capped, decoded excerpt of a text file (binary is reported as such). */
  textPreview: (uid, signal) => get('/files/text', { uid }, signal),
  /** Hand back a poster frame the browser rendered for a 3D model. */
  putRenderedThumbnail: (uid, png, signal) =>
    post('/files/thumbnail', { png }, { uid }, signal),
  output: (id, signal) => get('/outputs/' + id, null, signal),
  outputGraph: (id, signal) => get('/outputs/' + id + '/graph', null, signal),
  extractWorkflow: (id, body, signal) =>
    post('/outputs/' + id + '/extract-workflow', body, null, signal),
  patchOutput: (id, body, signal) => patch('/outputs/' + id, body, signal),
  bulkOutputs: (uids, body, signal) => post('/outputs/bulk', { uids, patch: body }, null, signal),

  /* ------------------------------------------------------------- search */
  search: (query, signal) => get('/search', query, signal),
  suggest: (q, limit, signal) => get('/search/suggest', { q, limit }, signal),
  searchStatus: (signal) => get('/search/status', null, signal),
  searchRebuild: (body, signal) => post('/search/rebuild', body, null, signal),

  /* --------------------------------------------------------- embeddings */
  embeddingsStatus: (signal) => get('/embeddings/status', null, signal),
  embeddingsEnable: (source, signal) =>
    post('/embeddings/enable', { source: source || 'auto' }, null, signal),
  embeddingsDisable: (purge, signal) =>
    post('/embeddings/disable', {}, { purge: Boolean(purge) }, signal),
  embeddingsRebuild: (body, signal) => post('/embeddings/rebuild', body, null, signal),

  /* ------------------------------------------------------------ hashing */
  hashEnqueue: (body, signal) => post('/hash/enqueue', body, null, signal),
  hashCancel: (body, signal) =>
    post('/hash/cancel', body || { batch_id: null, uids: null }, null, signal),
  hashStatus: (signal) => get('/hash/status', null, signal),
  hashSettings: (body, signal) => post('/hash/settings', body, null, signal),

  /* --------------------------------------------------------- file operations */
  rename: (body, signal) => post('/fileops/rename', body, null, signal),
  move: (body, signal) => post('/fileops/move', body, null, signal),
  remove: (body, signal) => post('/fileops/delete', body, null, signal),
  trash: (query, signal) => get('/fileops/trash', query, signal),
  trashRestore: (body, signal) => post('/fileops/trash/restore', body, null, signal),
  trashEmpty: (body, signal) => post('/fileops/trash/empty', body, null, signal),
  createFolder: (body, signal) => post('/fileops/create-folder', body, null, signal),
  reveal: (uid, signal) => request('GET', '/files/reveal', { query: { uid }, signal }),

  /* -------------------------------------------------------- albums and tags */
  albums: (scope, signal) => get('/albums', { scope }, signal),
  createAlbum: (body, signal) => post('/albums', body, null, signal),
  patchAlbum: (id, body, signal) => patch('/albums/' + id, body, signal),
  deleteAlbum: (id, signal) => del('/albums/' + id, undefined, signal),
  addAlbumItems: (id, uids, signal) => post('/albums/' + id + '/items', { uids }, null, signal),
  removeAlbumItems: (id, uids, signal) => del('/albums/' + id + '/items', { uids }, signal),
  tags: (query, signal) => get('/tags', query, signal),
  assignTags: (body, signal) => post('/tags/assign', body, null, signal),

  /* ------------------------------------------------- storage & maintenance */
  storageSummary: (query, signal) => get('/storage/summary', query, signal),
  storageCandidates: (query, signal) => get('/storage/candidates', query, signal),
  storageDuplicates: (query, signal) => get('/storage/duplicates', query, signal),
  storageRoots: (signal) => get('/storage/roots', null, signal),
  storageEstimate: (uids, signal) => post('/storage/estimate', { uids }, null, signal),
  storageCleanup: (body, signal) => post('/storage/cleanup', body, null, signal),

  /* ------------------------------- ComfyUI version, updater and templates */
  comfyInfo: (signal) => get('/comfyui/info', null, signal),
  comfyLatest: (force, signal) => get('/comfyui/latest', { force }, signal),
  comfyUpdatePlan: (updater, signal) => get('/comfyui/update/plan', { updater }, signal),
  comfyUpdateRun: (body, signal) => post('/comfyui/update/run', body, null, signal),
  comfyUpdateStatus: (signal) => get('/comfyui/update/status', null, signal),
  comfyOpenWorkflowPlan: (uid, launcher, signal) =>
    get('/comfyui/open-workflow/plan', { uid, launcher }, signal),
  comfyOpenWorkflow: (body, signal) => post('/comfyui/open-workflow', body, null, signal),
  comfyLaunchStatus: (signal) => get('/comfyui/launch/status', null, signal),
  comfyTemplates: (query, signal) => get('/comfyui/templates', query, signal),
  comfyWorkflowOrigins: (signal) => get('/comfyui/workflow-origins', null, signal),
  comfyPathPolicy: (signal) => get('/comfyui/path-policy', null, signal),

  /* ------------------------------------------- MCP activity log (read only) */
  /* The audit trail of everything an external MCP client changed. There is no
     write, edit or purge call here because the API has none: the log is
     append-only by design (DECISIONS C5 rail 3). */
  mcpAudit: (query, signal) => get('/mcp/audit', query, signal),

  /* --------------------------------------------------------- local assistant */
  aiStatus: (signal) => get('/ai/status', null, signal),
  aiDescribe: (body, signal) => post('/ai/describe', body, null, signal)
}

/* --------------------------------------------------------------- media URLs */

/** Slider tile size -> allowed thumbnail tier (API_CONTRACT 10). */
export function thumbTier(tile) {
  if (tile <= 180) return 160
  if (tile <= 360) return 320
  return 640
}

/* Renderer version, mirrored from the backend's THUMB_VERSION.  Thumbnails are
   served `immutable`, so the URL - not just the ETag - has to change when the
   rendering does, or a client keeps its stale copy for a year.  Bump both. */
export const THUMB_VERSION = 2

export function thumbnailUrl(uid, size) {
  return API_BASE + '/files/thumbnail?uid=' + encodeURIComponent(uid) +
    '&size=' + size + '&v=' + THUMB_VERSION
}

export function rawUrl(uid) {
  return API_BASE + '/files/raw?uid=' + encodeURIComponent(uid)
}

export function downloadUrl(uid) {
  return API_BASE + '/files/download?uid=' + encodeURIComponent(uid)
}

export function streamUrl(name, query) {
  return API_BASE + '/' + name + '/stream' + buildQuery(query)
}

export default api
