import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StarRating, ColorLabelPicker, NotesEditor } from './Annotations.jsx'

afterEach(cleanup)

/*
 * The rail's "My rating" filter and the Rating sort key on user_rating, so
 * the one control that can produce a rating has to behave: set, clear, and
 * never fire a phantom value.
 */

describe('StarRating', () => {
  it('sets the clicked value', () => {
    const onChange = vi.fn()
    render(<StarRating value={0} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: '4 stars' }))
    expect(onChange).toHaveBeenCalledWith(4)
  })

  it('clears when the current value is clicked again', () => {
    const onChange = vi.fn()
    render(<StarRating value={3} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: '3 stars' }))
    expect(onChange).toHaveBeenCalledWith(0)
  })

  it('reflects the stored rating as the checked radio', () => {
    render(<StarRating value={2} onChange={vi.fn()} />)
    expect(screen.getByRole('radio', { name: '2 stars' })).toBeChecked()
    expect(screen.getByRole('radio', { name: '5 stars' })).not.toBeChecked()
  })
})

describe('ColorLabelPicker', () => {
  it('sets a label and clears it on a second click', () => {
    const onChange = vi.fn()
    const { rerender } = render(<ColorLabelPicker value={null} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'amber label' }))
    expect(onChange).toHaveBeenCalledWith('amber')

    rerender(<ColorLabelPicker value="amber" onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'amber label' }))
    expect(onChange).toHaveBeenLastCalledWith(null)
  })
})

describe('NotesEditor', () => {
  it('saves on blur only when the text changed', () => {
    const onSave = vi.fn()
    render(<NotesEditor value="old" onSave={onSave} />)
    const box = screen.getByLabelText('Your notes')

    fireEvent.blur(box)
    expect(onSave).not.toHaveBeenCalled()

    fireEvent.change(box, { target: { value: 'trigger word: neonpunk' } })
    fireEvent.blur(box)
    expect(onSave).toHaveBeenCalledWith('trigger word: neonpunk')
  })
})
