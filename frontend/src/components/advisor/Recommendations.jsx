import React from 'react'
import { Trophy, Rocket, Scale, ArrowRight } from 'lucide-react'
import EmptyState from '../common/EmptyState.jsx'
import Button from '../common/Button.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { bytes as fmtBytes } from '../../services/format.js'
import { ScoreRing } from './charts.jsx'
import {
  Est, FitBadge, NeedBar, KindIcon, gb, SectionHead, SOURCE_LABEL, VERDICT_TONE
} from './parts.jsx'

/*
 * Recommendations - per model type, three answers:
 *
 *   OPTIMUM   the best models that fit with the headroom left free;
 *   MAXIMUM   the heaviest thing that still runs entirely on the GPU;
 *   STRETCH   heavier still - runs only by offloading, and slowly.
 *
 * The whole page is the advisor's opinion, so it sits on the violet panel and
 * every figure on it is marked as an estimate.
 */

const VERDICT_BAR = ['optimal', 'good', 'max', 'offload', 'cpu', 'no']
const VERDICT_NAME = {
  optimal: 'Optimal', good: 'Good fit', max: 'Maximum', offload: 'Offload',
  cpu: 'CPU only', no: 'Too large', base: 'Add-on', unknown: 'Unknown'
}

function VerdictBar({ counts, total }) {
  const sum = VERDICT_BAR.reduce((a, v) => a + (counts[v] || 0), 0) || total || 1
  const text = VERDICT_BAR.filter((v) => counts[v])
    .map((v) => VERDICT_NAME[v] + ' ' + counts[v]).join(', ')
  return (
    <div className="gp-verdictbar" role="img" aria-label={'Fit breakdown: ' + text}>
      {VERDICT_BAR.map((v) => (counts[v] ? (
        <span
          key={v}
          className={'gp-verdictbar__seg gp-verdictbar__seg--' + VERDICT_TONE[v] +
            (v === 'good' || v === 'cpu' ? ' gp-verdictbar__seg--soft' : '')}
          style={{ '--gp-adv-v': (counts[v] / sum) * 100 + '%' }}
          title={VERDICT_NAME[v] + ' - ' + counts[v]}
        />
      ) : null))}
    </div>
  )
}

function PickCard({ row, pick, rank, onOpen, tone }) {
  const fit = row.fit || {}
  const variant = pick || {
    label: row.lead.label, bytes: row.lead.bytes, need_bytes: fit.need_bytes,
    verdict: fit.verdict, verdict_label: fit.label, speed: fit.speed,
    used_pct_of_ceiling: fit.used_pct_of_ceiling
  }
  return (
    <button
      type="button"
      className={'gp-pick' + (tone ? ' gp-pick--' + tone : '')}
      onClick={() => onOpen(row.id)}
      title={row.fit_reason || undefined}
    >
      {rank ? <span className="gp-pick__rank">{rank}</span> : null}
      <span className="gp-pick__body">
        <span className="gp-pick__title">
          <span className="gp-u-truncate">{row.name}</span>
        </span>
        <span className="gp-pick__sub gp-u-truncate">
          {row.publisher} - {SOURCE_LABEL[row.source] || row.source}
        </span>
        <span className="gp-pick__variant gp-u-truncate" title={variant.label}>
          {variant.label}
        </span>
        <span className="gp-pick__meter">
          <NeedBar pct={variant.used_pct_of_ceiling} verdict={variant.verdict} />
        </span>
        <span className="gp-pick__nums">
          <Est>{gb(variant.need_bytes)}</Est>
          <span className="gp-u-meta">file {fmtBytes(variant.bytes)}</span>
        </span>
      </span>
      <span className="gp-pick__side">
        <ScoreRing score={row.security.score} tone={row.security.tone} />
        <FitBadge verdict={variant.verdict} label={variant.verdict_label}
          speed={variant.speed} />
      </span>
    </button>
  )
}

function Group({ group, onOpen, onShowAll }) {
  const empty = !group.optimum.length && !group.maximum.length && !group.stretch.length
  return (
    <section className="gp-inst-panel gp-inst-panel--vio gp-recgroup">
      <div className="gp-recgroup__head">
        <KindIcon kind={group.kind} className="gp-recgroup__icon" />
        <h3 className="gp-recgroup__title">{group.label}</h3>
        <span className="gp-recgroup__count">{group.total} known</span>
        <span className="gp-u-auto-l" />
        <Button variant="ghost" size="sm" icon={ArrowRight} label="All of them"
          onClick={() => onShowAll(group.kind)} />
      </div>
      {!group.addon ? <VerdictBar counts={group.counts} total={group.total} /> : null}

      {empty ? (
        <p className="gp-inst-note">
          Nothing of this type fits this machine. The catalog still lists them,
          with what each would need.
        </p>
      ) : null}

      {group.optimum.length ? (
        <div className="gp-recgroup__lane">
          <div className="gp-recgroup__lane-head">
            <Trophy aria-hidden="true" />
            {group.addon ? 'Most used' : 'Optimum'}
            <span className="gp-recgroup__lane-sub">
              {group.addon ? 'costs its base model, plus a little'
                : 'fits with the headroom left free'}
            </span>
          </div>
          <div className="gp-recgroup__grid">
            {group.optimum.map((row, i) => (
              <PickCard key={row.id} row={row} rank={i + 1} onOpen={onOpen}
                tone={i === 0 ? 'top' : null} />
            ))}
          </div>
        </div>
      ) : null}

      {group.maximum.length ? (
        <div className="gp-recgroup__lane">
          <div className="gp-recgroup__lane-head">
            <Rocket aria-hidden="true" />Maximum
            <span className="gp-recgroup__lane-sub">the heaviest that still runs on the GPU</span>
          </div>
          <div className="gp-recgroup__grid">
            {group.maximum.map((row) => (
              <PickCard key={'m' + row.id} row={row} pick={row.maximum} onOpen={onOpen} />
            ))}
          </div>
        </div>
      ) : null}

      {group.stretch.length ? (
        <div className="gp-recgroup__lane">
          <div className="gp-recgroup__lane-head">
            <Scale aria-hidden="true" />Stretch
            <span className="gp-recgroup__lane-sub">runs only by offloading - slow</span>
          </div>
          <div className="gp-recgroup__grid">
            {group.stretch.map((row) => (
              <PickCard key={'s' + row.id} row={row} pick={row.stretch} onOpen={onOpen} />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}

export default function Recommendations({ data, loading, error, onOpen, onShowAll }) {
  if (error) {
    return <EmptyState tone="error" title="Recommendations could not be worked out"
      text={error.message} />
  }
  if (loading && !data) return <SkeletonRows rows={12} />
  if (!data) return null
  const budget = data.budget || {}
  const acc = data.accelerator || {}
  const totals = data.totals || {}
  const comfortable = (totals.optimal || 0) + (totals.good || 0)

  return (
    <div className="gp-adv">
      <section className="gp-rechero">
        <div>
          <div className="gp-inst-hero__eyebrow">For this machine</div>
          <h2 className="gp-rechero__title">
            <Est title="Counted from the catalog against this machine's memory plan">
              {data.runnable}
            </Est>
            {' of '}{data.catalog_total} models run here
          </h2>
          <p className="gp-inst-hero__text">
            {comfortable} fit comfortably, {totals.max || 0} only at the limit and{' '}
            {(totals.offload || 0) + (totals.cpu || 0)} with offloading.
            {' '}Planned against <Est>{gb(acc.backend === 'cpu' ? budget.ram_safe
              : budget.vram_safe)}</Est>
            {acc.backend === 'cpu' ? ' of system memory' : ' of ' + gb(budget.vram_total) +
              ' GPU memory'}, keeping {acc.backend === 'cpu' ? budget.ram_headroom_pct
              : budget.vram_headroom_pct}% free.
          </p>
        </div>
        <div className="gp-rechero__totals">
          {VERDICT_BAR.filter((v) => totals[v]).map((v) => (
            <div className="gp-rechero__total" key={v}>
              <span className={'gp-fit__dot gp-fit--' + VERDICT_TONE[v]} />
              <span className="gp-rechero__num">{totals[v]}</span>
              <span className="gp-rechero__label">{VERDICT_NAME[v]}</span>
            </div>
          ))}
        </div>
      </section>

      <SectionHead label="By model type" count={data.groups.length}
        sub="estimates, not benchmarks" />
      {data.groups.map((group) => (
        <Group key={group.kind} group={group} onOpen={onOpen} onShowAll={onShowAll} />
      ))}
      {!data.groups.length ? (
        <EmptyState title="No model types selected"
          text="Pick at least one type to check in the bar above." />
      ) : null}
    </div>
  )
}
