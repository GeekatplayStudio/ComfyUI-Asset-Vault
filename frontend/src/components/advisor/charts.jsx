import React, { useMemo, useId } from 'react'
import { bytes as fmtBytes, percent } from '../../services/format.js'

/*
 * charts.jsx - the System Advisor's instruments.
 *
 * Four marks, all plain SVG or flex boxes, no chart library:
 *   Gauge      a 270 degree arc for one live percentage
 *   Sparkline  the recent history of one or two series
 *   CoreBars   one bar per processor core
 *   BudgetBar  memory as a part-to-whole bar with the safe limit drawn on it
 *
 * Provenance decides the hue (DESIGN_SYSTEM 2): a reading taken from the
 * machine is amber and shifts to gold and red only as it nears its ceiling;
 * the safe limit and the headroom zone are worked out by the advisor, so they
 * are violet and carry a title that says so.
 *
 * The only values written from here are geometry: SVG path data, dash lengths,
 * and the --gp-adv-v / --gp-adv-x custom properties (DESIGN_SYSTEM 1).
 */

const R = 50
const C = 2 * Math.PI * R
const SWEEP = 0.75                     // 270 of 360 degrees
const ARC = C * SWEEP

function clamp(value, lo, hi) {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  return Math.max(lo, Math.min(hi, value))
}

/** Where a 0-100 value sits on the arc, as an SVG point. */
function arcPoint(pct, radius) {
  const angle = (135 + 270 * (pct / 100)) * (Math.PI / 180)
  return [60 + radius * Math.cos(angle), 60 + radius * Math.sin(angle)]
}

/** warn above `warnAt`, danger above `dangerAt`; measured values stay amber. */
export function loadTone(pct, warnAt, dangerAt) {
  if (pct === null || pct === undefined) return 'idle'
  if (pct >= (dangerAt === undefined ? 92 : dangerAt)) return 'danger'
  if (pct >= (warnAt === undefined ? 80 : warnAt)) return 'warn'
  return 'amber'
}

const TICKS = Array.from({ length: 28 }, (_, i) => i)

export function Gauge(props) {
  const {
    label, value, display, unit, sub, safePct, warnAt, dangerAt, title, icon: Icon
  } = props
  const gradId = useId()
  const pct = clamp(value, 0, 100)
  const tone = loadTone(pct, warnAt, dangerAt)
  const filled = pct === null ? 0 : ARC * (pct / 100)
  const safe = clamp(safePct, 0, 100)
  const safeFrom = safe === null ? null : arcPoint(safe, R - 9)
  const safeTo = safe === null ? null : arcPoint(safe, R + 7)

  const aria = label + ': ' + (pct === null ? 'no reading' : percent(pct)) +
    (safe !== null ? ', safe limit ' + percent(safe) : '')

  return (
    <div className={'gp-gauge gp-gauge--' + tone} title={title}>
      <svg className="gp-gauge__svg" viewBox="0 0 120 120" role="img" aria-label={aria}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" className="gp-gauge__stop-a" />
            <stop offset="100%" className="gp-gauge__stop-b" />
          </linearGradient>
        </defs>
        <g className="gp-gauge__ticks">
          {TICKS.map((i) => {
            const at = (i / 27) * 100
            const [x1, y1] = arcPoint(at, R - 12)
            const [x2, y2] = arcPoint(at, i % 9 === 0 ? R - 18 : R - 15)
            return (
              <line
                key={i} x1={x1} y1={y1} x2={x2} y2={y2}
                className={pct !== null && at <= pct ? 'gp-gauge__tick gp-gauge__tick--on'
                  : 'gp-gauge__tick'}
              />
            )
          })}
        </g>
        <circle
          className="gp-gauge__track" cx="60" cy="60" r={R}
          strokeDasharray={ARC + ' ' + C} transform="rotate(135 60 60)"
        />
        {safe !== null && safe < 100 ? (
          <circle
            className="gp-gauge__headroom" cx="60" cy="60" r={R}
            strokeDasharray={(ARC * (100 - safe) / 100) + ' ' + C}
            transform={'rotate(' + (135 + 270 * (safe / 100)) + ' 60 60)'}
          />
        ) : null}
        <circle
          className="gp-gauge__value" cx="60" cy="60" r={R}
          stroke={'url(#' + gradId + ')'}
          strokeDasharray={filled + ' ' + C} transform="rotate(135 60 60)"
        />
        {safeFrom ? (
          <line
            className="gp-gauge__safe"
            x1={safeFrom[0]} y1={safeFrom[1]} x2={safeTo[0]} y2={safeTo[1]}
          >
            <title>Safe limit - worked out from the headroom setting</title>
          </line>
        ) : null}
      </svg>
      <div className="gp-gauge__read">
        {Icon ? <Icon className="gp-gauge__icon" aria-hidden="true" /> : null}
        <span className="gp-gauge__num">
          {display !== undefined && display !== null
            ? display : (pct === null ? '--' : Math.round(pct))}
          {unit ? <span className="gp-gauge__unit">{unit}</span> : null}
        </span>
        <span className="gp-gauge__label">{label}</span>
      </div>
      {sub ? <div className="gp-gauge__sub">{sub}</div> : null}
    </div>
  )
}

function buildPath(values, max, width, height, pad) {
  const usable = values.filter((v) => v !== null && v !== undefined)
  if (usable.length < 2) return null
  const top = max > 0 ? max : 1
  const step = width / (values.length - 1)
  let line = ''
  let started = false
  let firstX = 0
  let lastX = 0
  values.forEach((raw, i) => {
    if (raw === null || raw === undefined) return
    const x = i * step
    const y = pad + (height - pad * 2) * (1 - Math.min(1, raw / top))
    line += (started ? ' L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2)
    if (!started) firstX = x
    lastX = x
    started = true
  })
  const area = line + ' L' + lastX.toFixed(2) + ' ' + height + ' L' +
    firstX.toFixed(2) + ' ' + height + ' Z'
  return { line, area }
}

/**
 * Sparkline - `series` is [{ key, values, tone }]. `max` fixes the scale (100
 * for percentages); without it the tallest sample sets it.
 */
export function Sparkline(props) {
  const { series, max, height = 36, label, limit, className } = props
  const gradBase = useId()
  const width = 100
  const top = useMemo(() => {
    if (max) return max
    let peak = 0
    for (const s of series) {
      for (const v of s.values) if (v !== null && v !== undefined && v > peak) peak = v
    }
    return peak || 1
  }, [series, max])

  const paths = series.map((s) => ({
    ...s, path: buildPath(s.values, top, width, height, 2)
  }))
  const limitY = limit !== undefined && limit !== null && top > 0
    ? 2 + (height - 4) * (1 - Math.min(1, limit / top)) : null

  return (
    <svg
      className={'gp-spark' + (className ? ' ' + className : '')}
      viewBox={'0 0 ' + width + ' ' + height}
      preserveAspectRatio="none" role="img"
      aria-label={label || 'Recent history'}
    >
      <defs>
        {paths.map((s) => (
          <linearGradient key={s.key} id={gradBase + s.key} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className={'gp-spark__stop gp-spark__stop--' + (s.tone || 'amber')} />
            <stop offset="100%" className="gp-spark__stop gp-spark__stop--end" />
          </linearGradient>
        ))}
      </defs>
      <line className="gp-spark__grid" x1="0" x2={width} y1={height / 2} y2={height / 2} />
      {limitY !== null ? (
        <line className="gp-spark__limit" x1="0" x2={width} y1={limitY} y2={limitY}>
          <title>Safe limit - worked out from the headroom setting</title>
        </line>
      ) : null}
      {paths.map((s) => (s.path ? (
        <g key={s.key}>
          <path d={s.path.area} fill={'url(#' + gradBase + s.key + ')'} />
          <path d={s.path.line} className={'gp-spark__line gp-spark__line--' + (s.tone || 'amber')} />
        </g>
      ) : null))}
    </svg>
  )
}

/** One bar per core. The row is a picture; the average is stated beside it. */
export function CoreBars({ cores, performance }) {
  const list = cores || []
  if (!list.length) return null
  const summary = 'Per-core load: ' + list.map((v, i) => 'core ' + (i + 1) + ' ' +
    Math.round(v) + '%').join(', ')
  return (
    <div className="gp-cores" role="img" aria-label={summary}>
      {list.map((value, i) => (
        <span
          key={i}
          className={'gp-cores__bar gp-cores__bar--' + loadTone(value, 75, 92) +
            (performance && i < performance ? ' gp-cores__bar--perf' : '')}
          style={{ '--gp-adv-v': Math.max(3, Math.min(100, value)) + '%' }}
          title={'Core ' + (i + 1) + (performance
            ? (i < performance ? ' (performance)' : ' (efficiency)') : '') +
            ' - ' + Math.round(value) + '%'}
        />
      ))}
    </div>
  )
}

/**
 * BudgetBar - memory as part-to-whole, with the plan drawn on it.
 *
 *   |== in use ==|........free........|////headroom////|
 *                                     ^ safe limit
 *
 * `used` and `total` are measured (amber). `safe` is the advisor's planning
 * limit (violet marker), and the stretch from it to `total` is the headroom
 * the advisor never plans to spend (violet hatch).
 */
export function BudgetBar(props) {
  const { total, used, safe, label, usedLabel, safeLabel, need, needLabel } = props
  if (!total) return null
  const usedPct = used === null || used === undefined ? 0
    : Math.max(0, Math.min(100, (used / total) * 100))
  const safePct = safe === null || safe === undefined ? null
    : Math.max(0, Math.min(100, (safe / total) * 100))
  const needPct = need === null || need === undefined ? null
    : Math.max(0, Math.min(100, (need / total) * 100))
  const over = safePct !== null && usedPct > safePct
  const aria = (label ? label + '. ' : '') + (usedLabel || 'In use') + ' ' +
    fmtBytes(used || 0) + ' of ' + fmtBytes(total) +
    (safe ? ', safe limit ' + fmtBytes(safe) : '') +
    (need ? ', ' + (needLabel || 'needed') + ' ' + fmtBytes(need) : '')

  return (
    <div className="gp-budget" role="img" aria-label={aria}>
      <div className="gp-budget__track">
        {safePct !== null && safePct < 100 ? (
          <span
            className="gp-budget__headroom"
            style={{ '--gp-adv-x': safePct + '%' }}
            title={'Headroom the advisor never plans to use - ' +
              fmtBytes(total - safe)}
          />
        ) : null}
        {used !== null && used !== undefined ? (
          <span
            className={'gp-budget__used' + (over ? ' gp-budget__used--over' : '')}
            style={{ '--gp-adv-v': usedPct + '%' }}
            title={(usedLabel || 'In use') + ' - ' + fmtBytes(used)}
          />
        ) : null}
        {needPct !== null ? (
          <span
            className={'gp-budget__need' + (safePct !== null && needPct > safePct
              ? ' gp-budget__need--over' : '')}
            style={{ '--gp-adv-v': needPct + '%' }}
            title={(needLabel || 'This model needs about') + ' ' + fmtBytes(need) +
              ' - an estimate'}
          />
        ) : null}
        {safePct !== null ? (
          <span
            className="gp-budget__safe"
            style={{ '--gp-adv-x': safePct + '%' }}
            title={(safeLabel || 'Safe limit') + ' ' + fmtBytes(safe) +
              ' - worked out from the headroom setting'}
          />
        ) : null}
      </div>
    </div>
  )
}

/** A small ring carrying a 0-100 score. Inferred, so the value is violet. */
export function ScoreRing({ score, tone, size, title }) {
  const r = 15
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(100, score || 0))
  return (
    <span
      className={'gp-ring gp-ring--' + (tone || 'neutral') + (size === 'lg' ? ' gp-ring--lg' : '')}
      title={title || 'Safety estimate ' + pct + ' of 100 - not a malware scan'}
    >
      <svg viewBox="0 0 36 36" aria-hidden="true">
        <circle className="gp-ring__track" cx="18" cy="18" r={r} />
        <circle
          className="gp-ring__value" cx="18" cy="18" r={r}
          strokeDasharray={(c * pct / 100) + ' ' + c} transform="rotate(-90 18 18)"
        />
      </svg>
      <span className="gp-ring__num">{pct}</span>
    </span>
  )
}
