import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import MetaRow from './MetaRow.jsx'

describe('MetaRow', () => {
  it('renders object metadata without throwing', () => {
    render(<MetaRow label="Metadata" value={{ architecture: 'SDXL', params: '6.6B' }} />)

    expect(screen.getByText('architecture SDXL · params 6.6B')).toBeInTheDocument()
  })

  it('renders array metadata as a readable value', () => {
    render(<MetaRow label="Tags" value={['portrait', 'cinematic']} />)

    expect(screen.getByText('portrait, cinematic')).toBeInTheDocument()
  })
})
