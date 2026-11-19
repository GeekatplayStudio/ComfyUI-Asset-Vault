import React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import CatalogPanel from './CatalogPanel.jsx'
import { FitBadge, Est, gb, compactCount, paramsText } from './parts.jsx'

const GIB = 1073741824

function row(patch) {
  return {
    id: 'hf:acme/model', name: 'Acme Model', publisher: 'acme', kind: 'image',
    source: 'huggingface', family: 'acme', license: 'apache-2.0', gated: false,
    downloads: 125000, updated_at: '2026-11-01T00:00:00Z', params: 12e9,
    url: 'https://huggingface.co/acme/model', curated: false, is_new: false,
    variant_count: 3, description: 'A test model.',
    lead: { id: 'a', label: 'acme_fp8', precision: 'fp8', format: 'safetensors',
      bytes: 6 * GIB },
    fit: { verdict: 'optimal', label: 'Optimal', runs: true, speed: 'fast',
      need_bytes: 8 * GIB, used_pct_of_ceiling: 35, used_pct_of_safe: 41 },
    fit_reason: 'Fits with room to spare.',
    security: { score: 92, grade: 'A', label: 'Safe', tone: 'ok' },
    ...patch
  }
}

const budget = {
  backend: 'cuda', vram_total: 24 * GIB, vram_safe: 19 * GIB, vram_max: 23 * GIB,
  pooled_safe: 19 * GIB, pooled_max: 23 * GIB, ram_total: 64 * GIB, ram_safe: 40 * GIB
}

function page(items) {
  return { items, page: { total: items.length, returned: items.length, has_more: false } }
}

describe('CatalogPanel', () => {
  it('lists a model with its estimate, verdict and source link', () => {
    render(<CatalogPanel data={page([row()])} budget={budget} onOpen={() => {}} />)
    expect(screen.getByText('Acme Model')).toBeInTheDocument()
    expect(screen.getByText('8.0 GB')).toHaveClass('gp-inferred')
    expect(screen.getByText('Optimal')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Open Acme Model on Hugging Face' })
    expect(link).toHaveAttribute('href', 'https://huggingface.co/acme/model')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('opens the record on click and from the keyboard', () => {
    const onOpen = vi.fn()
    render(<CatalogPanel data={page([row()])} budget={budget} onOpen={onOpen} />)
    const tr = screen.getByText('Acme Model').closest('tr')
    fireEvent.click(tr)
    fireEvent.keyDown(tr, { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledTimes(2)
    expect(onOpen).toHaveBeenCalledWith('hf:acme/model')
  })

  it('following the source link does not also open the record', () => {
    const onOpen = vi.fn()
    render(<CatalogPanel data={page([row()])} budget={budget} onOpen={onOpen} />)
    fireEvent.click(screen.getByRole('link', { name: 'Open Acme Model on Hugging Face' }))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('marks new and gated models', () => {
    render(<CatalogPanel data={page([row({ is_new: true, gated: true })])}
      budget={budget} onOpen={() => {}} />)
    expect(screen.getByText('new')).toBeInTheDocument()
    expect(screen.getByLabelText('Gated')).toBeInTheDocument()
  })

  it('explains an empty filtered result and offers a way out', () => {
    const onClear = vi.fn()
    render(<CatalogPanel data={page([])} filtered onClear={onClear} onOpen={() => {}} />)
    expect(screen.getByText('Nothing matches these filters')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onClear).toHaveBeenCalled()
  })

  it('reports a load failure instead of an empty table', () => {
    render(<CatalogPanel error={new Error('boom')} onOpen={() => {}} />)
    expect(screen.getByText('The catalog could not be loaded')).toBeInTheDocument()
    expect(screen.getByText('boom')).toBeInTheDocument()
  })
})

describe('advisor parts', () => {
  it('a verdict carries its explanation and says it is an estimate', () => {
    render(<FitBadge verdict="max" label="Maximum" speed="moderate" />)
    const badge = screen.getByText('Maximum').closest('.gp-fit')
    expect(badge).toHaveClass('gp-fit--warn')
    expect(badge.getAttribute('title')).toMatch(/safety headroom/)
    expect(badge.getAttribute('title')).toMatch(/Estimated/)
    expect(screen.getByText('Moderate')).toBeInTheDocument()
  })

  it('an estimate is rendered through the inferred convention with a source', () => {
    render(<Est>12 GB</Est>)
    const node = screen.getByText('12 GB')
    expect(node).toHaveClass('gp-inferred')
    expect(node.getAttribute('title')).toBeTruthy()
  })

  it('formats memory, counts and parameter sizes', () => {
    expect(gb(24 * GIB)).toBe('24.0 GB')
    expect(gb(24 * GIB, 0)).toBe('24 GB')
    expect(gb(null)).toBe('--')
    expect(compactCount(1250)).toBe('1.3k')
    expect(compactCount(12500000)).toBe('13M')
    expect(compactCount(null)).toBeNull()
    expect(paramsText(12e9)).toBe('12B')
    expect(paramsText(82e6)).toBe('82M')
  })
})
