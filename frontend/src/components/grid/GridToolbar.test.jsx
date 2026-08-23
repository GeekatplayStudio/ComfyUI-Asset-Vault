import React from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import GridToolbar, { SelectionActions } from './GridToolbar.jsx'

const view = { view: 'grid', tile: 220, sort: 'name', group: 'none' }
const facet = {
  field: 'base_model',
  label: 'Base',
  values: [{ value: 'sdxl', label: 'SDXL', count: 12, selected: false }],
  onToggle: vi.fn()
}

describe('GridToolbar', () => {
  it('keeps every filter group in a focusable filter strip', () => {
    const onClearFilters = vi.fn()
    render(
      <GridToolbar
        view={view}
        patch={vi.fn()}
        sorts={[{ value: 'name', label: 'Name' }]}
        groupOptions={['none']}
        facets={[facet]}
        activeFilters={[{ field: 'category', value: 'checkpoints', label: 'Checkpoints', remove: 'checkpoints' }]}
        onSetFilter={vi.fn()}
        onClearFilters={onClearFilters}
      />
    )

    const strip = screen.getByLabelText('Available filters')
    expect(strip).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('button', { name: /SDXL/i })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onClearFilters).toHaveBeenCalledOnce()
  })
})

describe('SelectionActions', () => {
  it('does not expose destructive actions before an asset is selected', () => {
    render(<SelectionActions count={0} onRename={vi.fn()} onMove={vi.fn()} onDelete={vi.fn()} onRefresh={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Rename' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
  })
})
