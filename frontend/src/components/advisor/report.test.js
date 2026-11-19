import { describe, expect, it } from 'vitest'
import { buildReport } from './report.js'

const GIB = 1073741824

const hardware = {
  os: { name: 'Windows', version: '11', arch: 'AMD64' },
  cpu: { model: 'Test CPU', physical_cores: 8, logical_cores: 16, features: ['AVX2'] },
  memory: { total_bytes: 64 * GIB },
  accelerator: { name: 'Test GPU 24G', backend: 'cuda', vram_total_bytes: 24 * GIB },
  tier: { label: 'Enthusiast' },
  gpus: [], volumes: [], notes: ['A note.'],
  budget: { vram_total: 24 * GIB, vram_reserve: GIB, vram_safe: 19 * GIB,
    vram_max: 23 * GIB, vram_headroom_pct: 15, ram_total: 64 * GIB,
    ram_safe: 40 * GIB, ram_headroom_pct: 25 }
}

function row(name) {
  return {
    name, publisher: 'acme', url: 'https://example.test/' + name,
    lead: { label: 'fp8 | scaled', bytes: 6 * GIB },
    fit: { label: 'Optimal', speed: 'fast', need_bytes: 8 * GIB },
    security: { score: 91, label: 'Safe' },
    maximum: { label: 'bf16', bytes: 12 * GIB, need_bytes: 15 * GIB,
      verdict_label: 'Maximum', speed: 'moderate' }
  }
}

const recommendations = {
  runnable: 3, catalog_total: 5, totals: { optimal: 2, good: 1, no: 2 },
  budget: hardware.budget,
  groups: [{ kind: 'image', label: 'Image', addon: false, optimum: [row('alpha')],
    maximum: [row('beta')], stretch: [] }]
}

const safeConfig = {
  summary: 'Plan against 19.0 GB of the 24 GB GPU.',
  comfyui: { command_line: '--reserve-vram 5.0',
    flags: [{ flag: '--reserve-vram', value: '5.0', why: 'Keeps 5 GB free.' }], env: [] },
  ollama: { env: [{ name: 'OLLAMA_NUM_PARALLEL', value: '1', why: 'One at a time.' }] },
  llama_cpp: { command_line: '--ctx-size 8192' },
  rules: ['Close other GPU applications.']
}

describe('buildReport', () => {
  const text = buildReport({ hardware, recommendations, safeConfig, appVersion: '2.4.0' })

  it('states up front what is measured and what is estimated', () => {
    expect(text).toMatch(/Hardware figures are measured/)
    expect(text).toMatch(/not a benchmark and not a malware scan/)
  })

  it('covers the machine, the plan, the picks and the safe settings', () => {
    for (const heading of ['## This machine', '## Memory plan', '## Recommendations',
      '### Image', '## Safe settings', '### ComfyUI', '### Ollama', '### llama.cpp']) {
      expect(text).toContain(heading)
    }
    expect(text).toContain('Test GPU 24G')
    expect(text).toContain('--reserve-vram 5.0')
    expect(text).toContain('https://example.test/alpha')
  })

  it('marks every requirement as an estimate', () => {
    expect(text).toContain('~8.0 GB')
    expect(text).toContain('~15.0 GB')
  })

  it('never lets a value break the table it sits in', () => {
    expect(text).toContain('fp8 / scaled')
    expect(text).not.toContain('fp8 | scaled')
  })

  it('survives missing sections', () => {
    const bare = buildReport({ hardware: null, recommendations: null, safeConfig: null })
    expect(bare).toContain('# System Advisor report')
    expect(bare).not.toContain('## Recommendations')
  })
})
