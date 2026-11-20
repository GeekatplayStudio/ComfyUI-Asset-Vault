import { describe, it, expect } from 'vitest'
import { presentAsset, taskBadge } from './AssetCard.jsx'
import {
  makeGroupKey, GROUPS, GROUP_LABELS, SORTS, WORKFLOW_TASKS, TASK_LABELS, RUNS_ON_LABELS
} from '../../state/actions.js'

const wf = {
  uid: 'workflow:3', name: 'upscale_4x', origin: 'user', folder: '',
  counts: { nodes: 4, missing_nodes: 0, missing_models: 0 },
  tasks: ['upscale', 'image_edit'], primary_task: 'upscale',
  primary_task_label: 'Upscale', runs_on: 'local'
}

describe('what a workflow does', () => {
  it('shows the main job on the card, first', () => {
    const p = presentAsset(wf, 'workflows')
    expect(p.badges[0].key).toBe('task')
    expect(p.badges[0].node.props.children).toBe('Upscale')
    expect(p.meta[0]).toBe('Upscale')
    expect(p.badges.map((b) => b.key)).not.toContain('api')
  })

  it('marks a paid partner-API workflow', () => {
    const p = presentAsset({ ...wf, runs_on: 'api' }, 'workflows')
    expect(p.badges.map((b) => b.key)).toContain('api')
  })

  it('shows the task on template cards too', () => {
    const p = presentAsset({
      ...wf, origin: 'official_template', template: { category: 'Image Tools', open_source: true }
    }, 'workflows')
    expect(p.badges.map((b) => b.key)).toContain('task')
    expect(p.meta[0]).toBe('Upscale')
  })

  it('has no badge before the workflow is classified', () => {
    expect(taskBadge({ ...wf, primary_task_label: null })).toBe(null)
  })

  it('groups and sorts by task', () => {
    expect(GROUPS.workflows).toContain('task')
    expect(GROUP_LABELS.task).toBe('What it does')
    expect(SORTS.workflows.map((s) => s.value)).toContain('task')
    expect(makeGroupKey('workflows', 'task')(wf)).toBe('upscale')
    expect(makeGroupKey('workflows', 'task')({})).toBe('')
  })

  it('knows every task key and both places a workflow can run', () => {
    expect(WORKFLOW_TASKS).toHaveLength(15)
    expect(TASK_LABELS.remove_background).toBe('Remove background')
    expect(TASK_LABELS.three_d).toBe('3D')
    expect(Object.keys(RUNS_ON_LABELS)).toEqual(['local', 'api'])
  })
})
