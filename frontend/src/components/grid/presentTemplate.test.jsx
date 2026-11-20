import { describe, it, expect } from 'vitest'
import { presentTemplate, presentAsset } from './AssetCard.jsx'
import { isHttps } from '../details/DependencyList.jsx'

const base = {
  uid: 'workflow:7', name: 'image_probe', title: 'Probe: Text to Image',
  origin: 'official_template', read_only: true, counts_outputs: 3,
  counts: { nodes: 9, missing_nodes: 0, missing_models: 2 },
  template: { category: 'Image', open_source: true, required_size: 2e9 }
}

describe('official template cards', () => {
  it('reads "needs N models", never the red broken state', () => {
    const p = presentTemplate(base)
    expect(p.title).toBe('Probe: Text to Image')
    expect(p.subtitle).toBe('Image')
    expect(p.error).toBe(false)
    expect(p.corner[0].node.props.children).toBe('needs 2 models')
    expect(p.corner[0].node.props.tone).toBe('warn')
  })

  it('is used for workflows whose origin is official_template', () => {
    expect(presentAsset(base, 'workflows').face.type).toBe('template')
    expect(presentAsset({ ...base, origin: 'user' }, 'workflows').face.type).toBe('workflow')
  })

  it('says API for a partner-API template with nothing missing', () => {
    const p = presentTemplate({
      ...base,
      counts: { nodes: 4, missing_nodes: 0, missing_models: 0 },
      template: { category: 'Image', open_source: false }
    })
    expect(p.corner[0].node.props.children).toBe('API')
    expect(p.badges.map((b) => b.key)).toContain('api')
  })
})

describe('download links', () => {
  it('only treats https URLs as links', () => {
    expect(isHttps('https://huggingface.co/x/resolve/main/m.safetensors')).toBe(true)
    expect(isHttps('http://example.com/m')).toBe(false)
    expect(isHttps('javascript:alert(1)')).toBe(false)
    expect(isHttps(null)).toBe(false)
  })
})
