import React, { useCallback, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, ExternalLink, PackageSearch, RefreshCw, ShieldAlert } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import { useVault } from '../../state/VaultContext.jsx'
import { dateTime } from '../../services/format.js'

/* Metadata catalogue only.  There deliberately is no install button here:
   a package becomes installable only from a workflow-specific dependency plan. */
export default function NodeRegistryPanel({ onMode }) {
  const { state, toastError } = useVault()
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [installed, setInstalled] = useState('')
  const [nonce, setNonce] = useState(0)
  const query = useMemo(() => ({ q: q || undefined, source: source || undefined,
    installed: installed === '' ? undefined : installed === 'yes', limit: 200 }), [q, source, installed])
  const registry = useResource('node-registry:' + JSON.stringify(query) + ':' + nonce,
    (signal) => api.nodeRegistry(query, signal), { epoch: state.dataEpoch })
  const [refreshing, setRefreshing] = useState(false)
  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  const refreshOnline = useCallback(async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      await api.nodeRegistryStatus(true)
      refresh()
    } catch (err) {
      toastError(err, 'Registry refresh failed')
    } finally {
      setRefreshing(false)
    }
  }, [refreshing, refresh, toastError])
  const meta = registry.data && registry.data.meta
  const items = registry.data && registry.data.items || []

  return (
    <div className="gp-main__body">
      <div className="gp-toolbar gp-u-wrap">
        <div className="gp-segment" role="group" aria-label="Node view">
          <button type="button" className="gp-segment__item" onClick={() => onMode('packages')}>Packages</button>
          <button type="button" className="gp-segment__item" onClick={() => onMode('classes')}>Classes</button>
          <button type="button" className="gp-segment__item gp-segment__item--active" aria-pressed="true">Registry</button>
        </div>
        <div className="gp-toolbar__label">Node Registry</div>
        <input className="gp-input" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Search package, class, publisher, or repository" aria-label="Search node registry" />
        <select className="gp-select" value={source} onChange={(e) => setSource(e.target.value)} aria-label="Registry source">
          <option value="">All sources</option><option value="comfy_registry">Comfy Registry</option>
          <option value="manager_legacy_map">Manager legacy map</option>
        </select>
        <select className="gp-select" value={installed} onChange={(e) => setInstalled(e.target.value)} aria-label="Installed filter">
          <option value="">Installed and not installed</option><option value="yes">Installed</option><option value="no">Not installed</option>
        </select>
        <Button size="sm" icon={RefreshCw} label="Refresh metadata" onClick={refreshOnline}
          disabled={refreshing} loading={refreshing}
          title="Refreshes package metadata only. It never installs or downloads a node." />
      </div>

      {meta ? (
        <div className={'gp-callout ' + (meta.error ? 'gp-callout--warn' : 'gp-callout--info') + ' gp-u-mb-5'}>
          <span className="gp-callout__icon">{meta.error ? <AlertTriangle /> : <PackageSearch />}</span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">Registry provenance</div>
            {meta.fetched_at ? <>Cached {dateTime(meta.fetched_at)}{meta.fresh ? ' (fresh)' : ' (stale)'}. </> : null}
            {meta.error || 'Search is read-only. Installation is offered only while resolving a workflow’s missing nodes.'}
          </div>
        </div>
      ) : null}

      {registry.loading && !items.length ? <SkeletonMeta rows={8} /> : null}
      {registry.error ? <EmptyState tone="error" icon={AlertTriangle} title="Could not load the registry"
        text={registry.error.message} actions={<Button onClick={refresh}>Retry</Button>} /> : null}
      {!registry.loading && !registry.error && !items.length ? <EmptyState icon={PackageSearch}
        title="No registry packages match" text="Try a different search or refresh the metadata cache." /> : null}
      <div className="gp-list">
        {items.map((item) => <RegistryRow key={item.id} item={item} />)}
      </div>
    </div>
  )
}

function RegistryRow({ item }) {
  const yellow = item.warnings && item.warnings.length
  return <article className="gp-panel gp-u-mb-4">
    <div className="gp-u-row gp-u-gap-3 gp-u-between gp-u-wrap">
      <div><strong>{item.name}</strong><div className="gp-u-fs-10 gp-u-meta">{item.id}</div></div>
      <div className="gp-u-row gp-u-gap-3 gp-u-wrap">
        <Badge tone={item.official ? 'dep-satisfied' : 'warn'}>{item.official ? 'Comfy Registry' : 'legacy mapping'}</Badge>
        <Badge tone={item.installed ? 'dep-satisfied' : 'neutral'}>{item.installed ? 'installed' : 'not installed'}</Badge>
        {item.version ? <Badge tone="mono">v{item.version}</Badge> : null}
      </div>
    </div>
    {item.description ? <p className="gp-u-fs-11 gp-u-mt-4">{item.description}</p> : null}
    <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4 gp-u-fs-10 gp-u-meta">
      {item.publisher ? <span>publisher: {item.publisher}</span> : null}
      {item.compatibility ? <span>ComfyUI: {item.compatibility}</span> : null}
      {item.classes && item.classes.length ? <span>{item.classes.length} mapped class(es)</span> : null}
      {item.dependencies && item.dependencies.length ? <span>{item.dependencies.length} declared dependency item(s)</span> : null}
      {item.repository ? <a className="gp-btn gp-btn--ghost gp-btn--sm" href={item.repository} target="_blank" rel="noopener noreferrer"><ExternalLink className="gp-btn__icon" /><span className="gp-btn__label">Repository</span></a> : null}
    </div>
    {yellow ? <div className="gp-callout gp-callout--warn gp-u-mt-4"><span className="gp-callout__icon"><ShieldAlert /></span><div className="gp-callout__body">{item.warnings.join(' ')}</div></div> : null}
  </article>
}
