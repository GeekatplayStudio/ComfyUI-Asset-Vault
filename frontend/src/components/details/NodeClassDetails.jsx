import React from 'react'
import { AlertTriangle, Workflow } from 'lucide-react'
import api from '../../services/api.js'
import useResource from '../../hooks/useResource.js'
import Button from '../common/Button.jsx'
import Badge, { ConfidenceBadge } from '../common/Badge.jsx'
import { SkeletonMeta } from '../common/Skeleton.jsx'
import EmptyState from '../common/EmptyState.jsx'
import MetaRow, { Section, DetailsFallback } from './MetaRow.jsx'
import { count as fmtCount } from '../../services/format.js'
import { useVault } from '../../state/VaultContext.jsx'

/*
 * NodeClassDetails - the signature of one node class: what it takes in, what it
 * emits, where in the package source it was found, and which workflows use it.
 */

function Signature({ title, entries, empty }) {
  const keys = Object.keys(entries || {})
  if (!keys.length) return <p className="gp-u-fs-11 gp-u-meta">{empty}</p>
  return (
    <table className="gp-table gp-table--compact">
      <thead>
        <tr>
          <th>{title}</th>
          <th className="gp-table__num">Type</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => (
          <tr key={title + ':' + k}>
            <td>{k}</td>
            <td className="gp-table__num">
              <Badge tone="mono">{String(entries[k])}</Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function NodeClassDetails({ id, onOpenUid }) {
  const { state } = useVault()
  const detail = useResource('node_class:' + id, (s) => api.nodeClass(id, s), { epoch: state.dataEpoch })
  const cls = detail.data

  if (detail.loading && !cls) {
    return (
      <DetailsFallback eyebrow="Node class" title="Loading class">
        <SkeletonMeta rows={8} />
      </DetailsFallback>
    )
  }
  if (detail.error) {
    return (
      <DetailsFallback eyebrow="Node class" title="Unavailable">
        <EmptyState tone="error" small icon={AlertTriangle}
          title="Could not load this node class" text={detail.error.message}
          actions={<Button onClick={detail.refresh}>Retry</Button>} />
      </DetailsFallback>
    )
  }
  if (!cls) return null

  const inputs = cls.inputs || {}
  const outputs = cls.outputs || {}
  const flags = cls.flags || {}
  const source = cls.source || {}
  const inferred = cls.confidence === 'inferred'

  return (
    <>
      <div className={'gp-details__header' + (inferred ? ' gp-details__header--inferred' : '')}>
        <div className="gp-details__eyebrow">{cls.category || 'uncategorised'}</div>
        <h2 className="gp-details__title">{cls.display_name || cls.node_id}</h2>
        <div className="gp-u-row gp-u-gap-3 gp-u-wrap gp-u-mt-4">
          <Badge tone="mono">{cls.class_name}</Badge>
          <ConfidenceBadge confidence={cls.confidence}
            title={'Recovered by strategy ' + (source.strategy || 'unknown')} />
          {flags.deprecated ? <Badge tone="warn">deprecated</Badge> : null}
          {flags.experimental ? <Badge tone="info">experimental</Badge> : null}
          {flags.api_node ? <Badge tone="neutral">api node</Badge> : null}
          {cls.output_node ? <Badge tone="ok">output node</Badge> : null}
        </div>
      </div>

      <div className="gp-details__body">
        {cls.package ? (
          <div className="gp-u-mb-6">
            <Button size="sm" label={cls.package.name}
              onClick={() => onOpenUid(cls.package.uid)} />
          </div>
        ) : null}

        {cls.description ? (
          <Section title="Description">
            <p className="gp-u-fs-12 gp-u-muted">{cls.description}</p>
          </Section>
        ) : null}

        <Section title="Required inputs">
          <Signature title="Input" entries={inputs.required}
            empty="This node takes no required inputs." />
        </Section>

        {inputs.optional && Object.keys(inputs.optional).length ? (
          <Section title="Optional inputs">
            <Signature title="Input" entries={inputs.optional} empty="" />
          </Section>
        ) : null}

        <Section title="Outputs">
          {(outputs.types || []).length ? (
            <table className="gp-table gp-table--compact">
              <thead>
                <tr><th>Name</th><th className="gp-table__num">Type</th></tr>
              </thead>
              <tbody>
                {(outputs.types || []).map((t, i) => (
                  <tr key={t + ':' + i}>
                    <td>
                      {outputs.names && outputs.names[i]
                        ? outputs.names[i]
                        : <span className="gp-u-meta">slot {i} - unnamed</span>}
                    </td>
                    <td className="gp-table__num"><Badge tone="mono">{t}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="gp-u-fs-11 gp-u-meta">This node emits nothing.</p>}
        </Section>

        <Section title="Where it is defined">
          <div className="gp-meta">
            <MetaRow label="node id" value={cls.node_id} />
            <MetaRow label="class" value={cls.class_name} />
            <MetaRow label="strategy" value={source.strategy}
              inferred={inferred}
              inferredTitle="Recovered by reading the package source rather than a declared node map" />
            <MetaRow label="file" value={source.file} wrap />
            <MetaRow label="line" value={source.lineno} num />
          </div>
        </Section>

        <Section
          title="Used in workflows"
          aside={<span className="gp-u-fs-10 gp-u-meta">
            {fmtCount((cls.counts && cls.counts.workflows) || 0)}
          </span>}
        >
          {(cls.workflows_using || []).length ? (
            <div className="gp-list">
              {cls.workflows_using.map((w) => (
                <button key={w.uid} type="button" className="gp-row gp-focus-inset"
                  onClick={() => onOpenUid(w.uid)} title={w.name}>
                  <Workflow className="gp-row__thumb gp-u-p-0" aria-hidden="true" />
                  <span className="gp-row__name">{w.name}</span>
                  <span className="gp-row__cell gp-row__cell--num">
                    {w.occurrences ? 'x' + w.occurrences : ''}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyState small icon={Workflow} title="Not used yet"
              text="No indexed workflow contains this node." />
          )}
        </Section>
      </div>
    </>
  )
}
