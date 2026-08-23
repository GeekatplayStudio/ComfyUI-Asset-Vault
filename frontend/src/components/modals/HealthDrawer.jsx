import React from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { humanise, count as fmtCount } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * HealthDrawer - the /system/health report.
 * Each check keeps its own status word next to its dot, so colour is never the
 * only signal.
 */
export default function HealthDrawer({ onClose, onOpenUid }) {
  const { state } = useVault()
  const health = useResource('health', (s) => api.health(s), { epoch: state.dataEpoch })
  const data = health.data

  const tone = data && data.status === 'ok' ? 'ok'
    : (data && data.status === 'error' ? 'danger' : 'warn')

  return (
    <Modal
      title="System health"
      subtitle={data ? 'Overall status: ' + data.status : 'Checking'}
      onClose={onClose}
      footer={(
        <>
          <Button variant="ghost" icon={RefreshCw} label="Recheck" onClick={health.refresh} />
          <Button variant="primary" onClick={onClose}>Close</Button>
        </>
      )}
    >
      {health.loading && !data ? <SkeletonMeta rows={8} /> : null}
      {health.error ? (
        <EmptyState tone="error" small icon={Activity} title="Health check unavailable"
          text={health.error.message}
          actions={<Button onClick={health.refresh}>Retry</Button>} />
      ) : null}

      {data ? (
        <>
          <div className="gp-u-mb-5">
            <Badge tone={tone} large>{data.status}</Badge>
          </div>
          {data.checks.map((check) => (
            <div className="gp-check-row" key={check.id}>
              <span className={'gp-check-row__dot gp-check-row__dot--' +
                (check.status === 'ok' ? 'ok' : (check.status === 'error' ? 'error' : 'warn'))}
              />
              <span className="gp-check-row__id">{humanise(check.id)}</span>
              <span className="gp-check-row__msg">
                <span className={'gp-u-fs-10 gp-u-caps gp-u-' +
                  (check.status === 'ok' ? 'ok' : (check.status === 'error' ? 'danger' : 'warn'))}
                >
                  {check.status}
                </span>
                {check.message ? <> {check.message}</> : null}
                {check.count ? <> ({fmtCount(check.count)})</> : null}
                {check.items && check.items.length ? (
                  <ul className="gp-confirm__list gp-u-mt-4">
                    {check.items.map((item, i) => (
                      <li key={(item.uid || item.path || item.package || 'item') + ':' + i}>
                        {item.uid ? (
                          <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm"
                            onClick={() => { onClose(); onOpenUid(item.uid) }}>
                            <span className="gp-btn__label">
                              {item.name} - {humanise(item.reason)}
                            </span>
                          </button>
                        ) : (item.path || (item.package + ' -> ' + item.repo_url))}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </span>
            </div>
          ))}
        </>
      ) : null}
    </Modal>
  )
}
