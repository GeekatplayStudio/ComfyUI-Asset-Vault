import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Gauge, Sparkline, CoreBars, BudgetBar, ScoreRing, loadTone } from './charts.jsx'

describe('loadTone', () => {
  it('keeps a measured value amber until it nears its ceiling', () => {
    expect(loadTone(10)).toBe('amber')
    expect(loadTone(85)).toBe('warn')
    expect(loadTone(97)).toBe('danger')
    expect(loadTone(null)).toBe('idle')
  })

  it('honours custom thresholds', () => {
    expect(loadTone(60, 50, 70)).toBe('warn')
    expect(loadTone(75, 50, 70)).toBe('danger')
  })
})

describe('Gauge', () => {
  it('states the reading and the safe limit in words, not only in an arc', () => {
    render(<Gauge label="VRAM" value={42} unit="%" safePct={85} />)
    expect(screen.getByRole('img', { name: 'VRAM: 42.0%, safe limit 85.0%' })).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
  })

  it('shows a placeholder instead of zero when there is no reading', () => {
    render(<Gauge label="GPU" value={null} unit="%" />)
    expect(screen.getByRole('img', { name: 'GPU: no reading' })).toBeInTheDocument()
    expect(screen.getByText('--')).toBeInTheDocument()
  })

  it('clamps an out-of-range reading rather than drawing past the arc', () => {
    const { container } = render(<Gauge label="CPU" value={180} unit="%" />)
    const arc = container.querySelector('.gp-gauge__value')
    const track = container.querySelector('.gp-gauge__track')
    expect(arc.getAttribute('stroke-dasharray').split(' ')[0])
      .toBe(track.getAttribute('stroke-dasharray').split(' ')[0])
  })
})

describe('Sparkline', () => {
  it('draws nothing for a series with fewer than two points', () => {
    const { container } = render(
      <Sparkline series={[{ key: 'a', values: [5] }]} max={100} />)
    expect(container.querySelector('.gp-spark__line')).toBeNull()
  })

  it('skips missing samples instead of dropping the line to zero', () => {
    const { container } = render(
      <Sparkline series={[{ key: 'a', values: [10, null, 30, 40] }]} max={100} />)
    const d = container.querySelector('.gp-spark__line').getAttribute('d')
    expect(d.match(/[ML]/g)).toHaveLength(3)
  })

  it('marks the safe limit when one is given', () => {
    const { container } = render(
      <Sparkline series={[{ key: 'a', values: [10, 20] }]} max={100} limit={80} />)
    expect(container.querySelector('.gp-spark__limit')).not.toBeNull()
  })
})

describe('CoreBars', () => {
  it('names every core in the accessible summary', () => {
    render(<CoreBars cores={[12, 80]} performance={1} />)
    expect(screen.getByRole('img', { name: 'Per-core load: core 1 12%, core 2 80%' }))
      .toBeInTheDocument()
  })

  it('renders nothing without readings', () => {
    const { container } = render(<CoreBars cores={[]} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('BudgetBar', () => {
  const GIB = 1073741824

  it('draws the headroom zone and the safe limit from the plan', () => {
    const { container } = render(
      <BudgetBar total={24 * GIB} used={6 * GIB} safe={20 * GIB} label="GPU memory" />)
    expect(container.querySelector('.gp-budget__headroom')).not.toBeNull()
    expect(container.querySelector('.gp-budget__safe')).not.toBeNull()
    expect(container.querySelector('.gp-budget__used--over')).toBeNull()
  })

  it('flags use beyond the safe limit', () => {
    const { container } = render(
      <BudgetBar total={24 * GIB} used={23 * GIB} safe={20 * GIB} />)
    expect(container.querySelector('.gp-budget__used--over')).not.toBeNull()
  })

  it('flags a model that needs more than the safe limit', () => {
    const { container } = render(
      <BudgetBar total={24 * GIB} safe={20 * GIB} need={22 * GIB} />)
    expect(container.querySelector('.gp-budget__need--over')).not.toBeNull()
  })

  it('renders nothing when the total is unknown', () => {
    const { container } = render(<BudgetBar total={0} used={1} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('ScoreRing', () => {
  it('says the score is an estimate and not a scan', () => {
    render(<ScoreRing score={91} tone="ok" />)
    expect(screen.getByTitle('Safety estimate 91 of 100 - not a malware scan'))
      .toBeInTheDocument()
  })
})
