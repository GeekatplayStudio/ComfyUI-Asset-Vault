import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'

/*
 * The details panel has to load through the real data hook. 2.5.0 shipped a
 * local `const api = ...` (a paid-API flag) that shadowed the imported api
 * module, so the loader closure read an uninitialised binding and the panel
 * died with "Cannot access 'C' before initialization" for every workflow.
 * A test that mocked the hook would never have seen it; this one does not.
 */

const calls = { workflow: 0 }
const template = {
  uid: 'workflow:945', id: 945, name: 'wan2.1_fun_inp', title: 'Wan 2.1 Inpainting',
  origin: 'official_template', read_only: true, folder: 'Video',
  counts: { nodes: 19, missing_nodes: 0, missing_models: 0 },
  template: { category: 'Video', open_source: true, tags: ['Video'] },
  tasks: ['image_to_video', 'image_edit'], primary_task: 'image_to_video',
  primary_task_label: 'Image to video', runs_on: 'local',
  outputs_recent: [], copies: [], actions: {}
}

vi.mock('../../services/api.js', () => {
  const api = {
    workflow: vi.fn(async () => { calls.workflow += 1; return template }),
    workflowDependencies: vi.fn(async () => ({ models: [], nodes: [], summary: {} })),
    reveal: vi.fn(async () => ({}))
  }
  return {
    default: api,
    api,
    isAbort: () => false,
    thumbnailUrl: (uid) => '/thumb/' + uid,
    downloadUrl: (uid) => '/download/' + uid,
    streamUrl: (name) => '/stream/' + name
  }
})

vi.mock('../../state/VaultContext.jsx', () => ({
  useVault: () => ({
    state: { dataEpoch: 0 },
    dispatch: vi.fn(),
    toast: vi.fn(),
    toastError: vi.fn()
  })
}))

// Imported after the mocks so it binds to them.
const { default: WorkflowDetails } = await import('./WorkflowDetails.jsx')

describe('WorkflowDetails', () => {
  beforeEach(() => { calls.workflow = 0 })

  it('loads and renders an official template without crashing', async () => {
    render(<WorkflowDetails id={945} onOpenUid={vi.fn()} onLightbox={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('Wan 2.1 Inpainting')).toBeInTheDocument())
    expect(calls.workflow).toBe(1)
    expect(screen.getByRole('button', { name: /Open in ComfyUI/ })).toBeInTheDocument()
  })

  it('lists what the workflow does, main job first', async () => {
    render(<WorkflowDetails id={945} onOpenUid={vi.fn()} onLightbox={vi.fn()} />)
    const main = await screen.findByRole('button', { name: 'Image to video' })
    expect(main).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Image edit' })).toBeInTheDocument()
  })
})
