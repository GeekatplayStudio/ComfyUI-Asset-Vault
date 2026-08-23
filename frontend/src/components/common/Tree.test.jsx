import React from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import Tree from './Tree.jsx'

describe('Tree', () => {
  it('selects the clicked sidebar category and exposes the current selection', () => {
    const onSelect = vi.fn()
    render(
      <Tree
        label="Base models"
        selectedKey="sdxl"
        onSelect={onSelect}
        nodes={[{ key: 'sdxl', label: 'SDXL', count: 12, children: [] }]}
      />
    )

    const item = screen.getByRole('treeitem', { name: /SDXL/i })
    expect(item).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(item)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ key: 'sdxl' }))
  })
})
