import React, { useCallback, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Download, ShieldAlert } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Modal from '../common/Modal.jsx'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import { useVault } from '../../state/VaultContext.jsx'

/* The plan is intentionally re-read each time this dialog opens.  It is a
   short-lived consent document, not a cached shopping cart. */
export default function ResolveNodesDialog({ workflowId, workflowName, onClose }) {
  const { toast, toastError, invalidate } = useVault()
  const [epoch, setEpoch] = useState(() => Date.now())
  const [chosen, setChosen] = useState([])
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const planRes = useResource('enable-plan:' + workflowId + ':' + epoch,
    (signal) => api.workflowEnablePlan(workflowId, signal), { epoch })
  const plan = planRes.data
  const packages = plan && plan.node_packages || []
  const selectable = useMemo(() => packages.filter((p) => p.status === 'fetchable'), [packages])
  const toggle = useCallback((id) => setChosen((old) => old.includes(id) ? old.filter((x) => x !== id) : [...old, id]), [])
  const refresh = useCallback(() => { setChosen([]); setAck(false); setEpoch(Date.now()) }, [])
  const submit = useCallback(async () => {
    if (!plan || !chosen.length || !ack) return
    setBusy(true)
    try {
      const result = await api.workflowEnableFetch(workflowId, { plan_token: plan.plan_token,
        item_ids: chosen, confirm: true, on_conflict: 'fail' })
      toast({ tone: 'ok', title: 'Missing-node install queued',
        message: result.queued + ' package(s) staged for safe fetch. No installer scripts or Python dependencies will run.' })
      invalidate(); onClose()
    } catch (err) { toastError(err, 'Could not queue the selected node packages') }
    finally { setBusy(false) }
  }, [plan, chosen, ack, workflowId, toast, toastError, invalidate, onClose])

  return <Modal title="Resolve missing nodes" subtitle={workflowName} onClose={onClose}
    footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button icon={Download} variant="primary" loading={busy}
      disabled={!chosen.length || !ack || busy} onClick={submit}>Queue selected packages</Button></>}>
    {planRes.loading && !plan ? <SkeletonMeta rows={8} /> : null}
    {planRes.error ? <div className="gp-callout gp-callout--warn"><span className="gp-callout__icon"><AlertTriangle /></span><div className="gp-callout__body"><div className="gp-callout__title">Could not build a dependency plan</div>{planRes.error.message}<div className="gp-callout__actions"><Button size="sm" onClick={refresh}>Try again</Button></div></div></div> : null}
    {plan ? <>
      <div className="gp-callout gp-callout--info gp-u-mb-5"><span className="gp-callout__icon"><ShieldAlert /></span><div className="gp-callout__body"><div className="gp-callout__title">Review before download</div>
        This plan expires in {Math.ceil(plan.plan_expires_in_ms / 60000)} minutes. {((plan.policy && plan.policy.never_runs) || []).join(' ')}</div></div>
      {!packages.length ? <div className="gp-callout gp-callout--info"><span className="gp-callout__icon"><CheckCircle2 /></span><div className="gp-callout__body">No missing node packages were found for this workflow.</div></div> : null}
      {packages.map((item) => <PackagePlan key={item.item_id} item={item} checked={chosen.includes(item.item_id)} onToggle={toggle} />)}
      {selectable.length ? <label className="gp-check gp-u-mt-5"><input className="gp-check__input" type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span className="gp-check__box"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg></span>
        <span className="gp-check__label">I reviewed the source, destination, and warnings. I understand ComfyUI must restart before new nodes load.</span></label> : null}
    </> : null}
  </Modal>
}

function PackagePlan({ item, checked, onToggle }) {
  const fetchable = item.status === 'fetchable'
  return <div className="gp-panel gp-u-mb-4"><div className="gp-u-row gp-u-gap-3 gp-u-between gp-u-wrap"><div>
    <strong>{item.ref_name}</strong><div className="gp-u-fs-10 gp-u-meta">{(item.class_types || []).join(', ') || 'No mapped classes'}</div></div>
    <Badge tone={fetchable ? 'dep-satisfied' : item.status === 'blocked' ? 'danger' : 'warn'}>{item.status.replaceAll('_', ' ')}</Badge></div>
    {item.repo_url ? <div className="gp-u-fs-10 gp-u-break-all gp-u-mt-4">{item.repo_url}</div> : null}
    {item.revision ? <div className="gp-u-fs-10 gp-u-meta gp-u-mt-4">Pinned commit: {item.revision}</div> : null}
    {item.safety && item.safety.length ? <div className="gp-u-mt-4 gp-u-fs-10">{item.safety.map((s) => <div key={s.code} className={s.level === 'red' ? 'gp-u-danger' : 'gp-u-muted'}>{s.level.toUpperCase()}: {s.message}</div>)}</div> : null}
    {item.reason ? <div className="gp-callout gp-callout--warn gp-u-mt-4"><span className="gp-callout__icon"><AlertTriangle /></span><div className="gp-callout__body">{item.reason}</div></div> : null}
    {item.destination ? <div className="gp-u-fs-10 gp-u-meta gp-u-mt-4">Destination: {item.destination.abs_path}</div> : null}
    {fetchable ? <label className="gp-check gp-u-mt-4"><input className="gp-check__input" type="checkbox" checked={checked} onChange={() => onToggle(item.item_id)} />
      <span className="gp-check__box"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.2 4.6 9 10 3.2" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg></span><span className="gp-check__label">Select this exact package</span></label> : null}
  </div>
}
