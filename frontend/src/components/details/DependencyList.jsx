import React, { useState } from 'react'
import { Package, Box, ExternalLink, Search } from 'lucide-react'
import Badge, { DepBadge } from '../common/Badge.jsx'
import Button from '../common/Button.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { Section } from './MetaRow.jsx'
import { humanise } from '../../services/format.js'

/*
 * DependencyList - what a workflow needs and what is actually on disk.
 *
 * The registry hint is the whole point of the missing-node report: it names the
 * package to install rather than leaving the user with a bare class name.
 * Fuzzy name matches are inference, so they carry the violet "~" treatment.
 */

function ModelRow({ dep, onOpenUid }) {
  const [open, setOpen] = useState(false)
  const suggestions = dep.suggestions || []
  return (
    <div className="gp-panel gp-panel--inset gp-u-mb-4">
      <div className="gp-u-row gp-u-gap-3 gp-u-between">
        <span className="gp-u-truncate gp-u-fs-11" title={dep.ref_name}>
          <Box size={11} aria-hidden="true" /> {dep.ref_name}
        </span>
        <DepBadge status={dep.status}
          title={'Matched by ' + (dep.match_method || 'no method')} />
      </div>
      <div className="gp-u-row gp-u-gap-3 gp-u-mt-4 gp-u-wrap">
        <Badge tone="role">{humanise(dep.category) || 'unknown folder'}</Badge>
        {dep.via && dep.via.length ? (
          <span className="gp-u-fs-10 gp-u-meta">
            via {dep.via[0].class}.{dep.via[0].input}
          </span>
        ) : null}
        {dep.occurrences > 1
          ? <span className="gp-u-fs-10 gp-u-meta">x{dep.occurrences}</span>
          : null}
      </div>

      {dep.status === 'satisfied' && dep.uid ? (
        <div className="gp-u-mt-4">
          <Button size="sm" variant="ghost" label="Open model"
            onClick={() => onOpenUid(dep.uid)} />
        </div>
      ) : null}

      {dep.status !== 'satisfied' && suggestions.length ? (
        <div className="gp-u-mt-4">
          <Button
            size="sm"
            variant="ghost"
            icon={Search}
            label={open
              ? 'Hide close matches'
              : suggestions.length + ' close match' + (suggestions.length === 1 ? '' : 'es')}
            onClick={() => setOpen((v) => !v)}
          />
          {open ? (
            <div className="gp-list gp-u-mt-4">
              {suggestions.map((s) => (
                <button
                  key={s.uid}
                  type="button"
                  className="gp-row gp-row--inferred gp-focus-inset"
                  onClick={() => onOpenUid(s.uid)}
                  title={'Name similarity ' + s.score.toFixed(2) + ' - this is a guess, not a match'}
                >
                  <span className="gp-row__name">
                    <span className="gp-inferred"
                      title={'Name similarity ' + s.score.toFixed(2)}
                    >
                      {s.name}
                    </span>
                  </span>
                  <span className="gp-row__cell gp-row__cell--num">
                    {(s.score * 100).toFixed(0)}%
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function NodeRow({ dep, onOpenUid }) {
  const hint = dep.registry_hint
  return (
    <div className="gp-panel gp-panel--inset gp-u-mb-4">
      <div className="gp-u-row gp-u-gap-3 gp-u-between">
        <span className="gp-u-truncate gp-u-fs-11" title={dep.class_type}>
          <Package size={11} aria-hidden="true" /> {dep.class_type}
        </span>
        <DepBadge status={dep.status} />
      </div>
      {dep.package ? (
        <div className="gp-u-mt-4">
          <Button size="sm" variant="ghost" label={dep.package.name}
            onClick={() => onOpenUid(dep.package.uid)} />
        </div>
      ) : null}
      {hint ? (
        <div className="gp-callout gp-callout--info gp-u-mt-4">
          <span className="gp-callout__icon"><Package aria-hidden="true" /></span>
          <div className="gp-callout__body">
            <div className="gp-callout__title">Install {hint.package}</div>
            <div className="gp-u-fs-11 gp-u-break-all">{hint.repo_url}</div>
            <div className="gp-callout__actions">
              <a
                className="gp-btn gp-btn--sm"
                href={hint.repo_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                <ExternalLink className="gp-btn__icon" aria-hidden="true" />
                <span className="gp-btn__label">Open repository</span>
              </a>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}

export default function DependencyList({ deps, onOpenUid }) {
  const [filter, setFilter] = useState('missing')
  if (!deps) return null

  const summary = deps.summary || {}
  const models = deps.models || []
  const nodes = deps.nodes || []

  const keep = (d) => filter === 'all' || d.status !== 'satisfied'
  const shownModels = models.filter(keep)
  const shownNodes = nodes.filter(keep)

  return (
    <>
      <div className="gp-u-row gp-u-gap-3 gp-u-mb-5 gp-u-wrap">
        <Badge tone="dep-satisfied">{summary.satisfied || 0} satisfied</Badge>
        <Badge tone="dep-missing">{summary.missing || 0} missing</Badge>
        {summary.ambiguous
          ? <Badge tone="dep-ambiguous">{summary.ambiguous} ambiguous</Badge>
          : null}
        <span className="gp-u-auto-l">
          <span className="gp-segment" role="group" aria-label="Dependency filter">
            <button
              type="button"
              className={'gp-segment__item' + (filter === 'missing' ? ' gp-segment__item--active' : '')}
              aria-pressed={filter === 'missing'}
              onClick={() => setFilter('missing')}
            >
              Unresolved
            </button>
            <button
              type="button"
              className={'gp-segment__item' + (filter === 'all' ? ' gp-segment__item--active' : '')}
              aria-pressed={filter === 'all'}
              onClick={() => setFilter('all')}
            >
              All
            </button>
          </span>
        </span>
      </div>

      {!shownModels.length && !shownNodes.length ? (
        <EmptyState
          small
          icon={Package}
          title={filter === 'missing' ? 'Nothing missing' : 'No dependencies recorded'}
          text={filter === 'missing'
            ? 'Every model and node this workflow references was found on disk.'
            : 'This workflow does not reference any model or custom node.'}
        />
      ) : null}

      {shownNodes.length ? (
        <Section title={'Node packages (' + shownNodes.length + ')'}>
          {shownNodes.map((d, i) => (
            <NodeRow key={d.class_type + ':' + i} dep={d} onOpenUid={onOpenUid} />
          ))}
        </Section>
      ) : null}

      {shownModels.length ? (
        <Section title={'Model files (' + shownModels.length + ')'}>
          {shownModels.map((d, i) => (
            <ModelRow key={d.ref_name + ':' + i} dep={d} onOpenUid={onOpenUid} />
          ))}
        </Section>
      ) : null}
    </>
  )
}
