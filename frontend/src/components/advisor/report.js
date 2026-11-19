/*
 * report.js - the System Advisor's findings as one Markdown file.
 *
 * Built in the browser from the same three payloads the tab already shows
 * (hardware, recommendations, safe settings), so the report can never say
 * something the screen does not. Nothing is uploaded: the text is handed to
 * the browser's own save dialog.
 *
 * Markdown rather than HTML or PDF on purpose - it opens anywhere, diffs
 * cleanly between two machines, and pastes straight into a support thread.
 */

const GIB = 1073741824

function gb(value, digits) {
  if (value === null || value === undefined) return '-'
  return (value / GIB).toFixed(digits === undefined ? 1 : digits) + ' GB'
}

/* A cell must not break the table it sits in. */
function cell(value) {
  if (value === null || value === undefined || value === '') return '-'
  return String(value).replace(/\|/g, '/').replace(/\s+/g, ' ').trim()
}

function table(head, rows) {
  if (!rows.length) return ['_None._', '']
  return [
    '| ' + head.join(' | ') + ' |',
    '| ' + head.map(() => '---').join(' | ') + ' |',
    ...rows.map((r) => '| ' + r.map(cell).join(' | ') + ' |'),
    ''
  ]
}

function pickRow(row, pick) {
  const v = pick || {
    label: row.lead.label, need_bytes: row.fit.need_bytes, bytes: row.lead.bytes,
    verdict_label: row.fit.label, speed: row.fit.speed
  }
  return [
    row.name, row.publisher, v.label, '~' + gb(v.need_bytes), gb(v.bytes),
    v.verdict_label, String(v.speed || '').replace('_', ' '),
    row.security.score + ' (' + row.security.label + ')', row.url
  ]
}

const PICK_HEAD = ['Model', 'Publisher', 'Variant', 'Needs', 'File', 'Fit', 'Speed',
  'Safety', 'Link']

export function buildReport({ hardware, recommendations, safeConfig, appVersion }) {
  const out = []
  const hw = hardware || {}
  const acc = hw.accelerator || {}
  const cpu = hw.cpu || {}
  const b = hw.budget || (recommendations && recommendations.budget) || {}
  const when = new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC'

  out.push('# System Advisor report', '')
  out.push('Generated ' + when + (appVersion ? ' by Geekatplay ComfyUI Asset Vault ' +
    appVersion : '') + '.', '')
  out.push('> Hardware figures are measured. Requirements, verdicts, speed classes and ' +
    'safety scores are estimates for planning - not a benchmark and not a malware scan.', '')

  out.push('## This machine', '')
  out.push(...table(['Item', 'Value'], [
    ['Class', (hw.tier && hw.tier.label) ? hw.tier.label + ' (estimate)' : null],
    ['Operating system', hw.os ? hw.os.name + ' ' + hw.os.version + ' (' + hw.os.arch + ')' : null],
    ['Processor', cpu.model],
    ['Cores', cpu.performance_cores
      ? cpu.performance_cores + ' performance + ' + (cpu.efficiency_cores || 0) + ' efficiency'
      : (cpu.physical_cores || '?') + ' cores / ' + (cpu.logical_cores || '?') + ' threads'],
    ['CPU features', (cpu.features || []).join(', ')],
    ['System memory', gb(hw.memory && hw.memory.total_bytes, 0)],
    ['Accelerator', acc.name],
    ['Backend', acc.backend],
    ['GPU memory', acc.backend === 'cpu' ? 'none'
      : gb(acc.vram_total_bytes) + (acc.unified ? ' (share of unified memory, estimate)' : '')]
  ]))

  if ((hw.gpus || []).length > 1) {
    out.push('### Graphics adapters', '')
    out.push(...table(['Adapter', 'Backend', 'Memory'], hw.gpus.map((g) => [
      g.name, g.integrated ? 'integrated' : g.backend,
      g.vram_total_bytes ? gb(g.vram_total_bytes) : 'shared'
    ])))
  }

  if ((hw.volumes || []).length) {
    out.push('### Volumes', '')
    out.push(...table(['Mount', 'Kind', 'Free', 'Total', 'Holds'], hw.volumes.map((v) => [
      v.mount, v.kind, gb(v.free_bytes), gb(v.total_bytes, 0), (v.roles || []).join(', ')
    ])))
  }

  out.push('## Memory plan', '')
  out.push(...table(['Budget', 'Value'], [
    ['GPU memory installed', gb(b.vram_total)],
    ['Left to the system', gb(b.vram_reserve)],
    ['Headroom kept free', (b.vram_headroom_pct || 0) + '%'],
    ['Safe GPU budget', '~' + gb(b.vram_safe)],
    ['Absolute GPU ceiling', '~' + gb(b.vram_max)],
    ['System memory installed', gb(b.ram_total, 0)],
    ['System memory headroom', (b.ram_headroom_pct || 0) + '%'],
    ['Safe system-memory budget', '~' + gb(b.ram_safe)]
  ]))
  ;(hw.notes || []).forEach((n) => out.push('- ' + n))
  if ((hw.notes || []).length) out.push('')

  if (recommendations) {
    const t = recommendations.totals || {}
    out.push('## Recommendations', '')
    out.push(recommendations.runnable + ' of ' + recommendations.catalog_total +
      ' known models run on this machine: ' + ((t.optimal || 0) + (t.good || 0)) +
      ' comfortably, ' + (t.max || 0) + ' at the limit, ' +
      ((t.offload || 0) + (t.cpu || 0)) + ' with offloading, ' + (t.no || 0) +
      ' too large.', '')
    for (const g of recommendations.groups || []) {
      out.push('### ' + g.label, '')
      out.push('**' + (g.addon ? 'Most used' : 'Optimum') + '** - ' +
        (g.addon ? 'an add-on costs its base model plus a little of its own.'
          : 'fits with the headroom left free.'), '')
      out.push(...table(PICK_HEAD, g.optimum.map((r) => pickRow(r))))
      if ((g.maximum || []).length) {
        out.push('**Maximum** - the heaviest that still runs entirely on the GPU.', '')
        out.push(...table(PICK_HEAD, g.maximum.map((r) => pickRow(r, r.maximum))))
      }
      if ((g.stretch || []).length) {
        out.push('**Stretch** - runs only by offloading; slow.', '')
        out.push(...table(PICK_HEAD, g.stretch.map((r) => pickRow(r, r.stretch))))
      }
    }
  }

  if (safeConfig) {
    out.push('## Safe settings', '')
    out.push(safeConfig.summary, '')
    out.push('### ComfyUI', '')
    out.push('```', safeConfig.comfyui.command_line || '(no extra arguments needed)', '```', '')
    out.push(...table(['Setting', 'Why'], [
      ...safeConfig.comfyui.flags.map((f) => [f.flag + (f.value ? ' ' + f.value : ''), f.why]),
      ...safeConfig.comfyui.env.map((e) => [e.name + '=' + e.value, e.why])
    ]))
    out.push('### Ollama', '')
    out.push(...table(['Setting', 'Why'],
      safeConfig.ollama.env.map((e) => [e.name + '=' + e.value, e.why])))
    out.push('### llama.cpp', '')
    out.push('```', safeConfig.llama_cpp.command_line, '```', '')
    out.push('### Rules of thumb', '')
    safeConfig.rules.forEach((r) => out.push('- ' + r))
    out.push('')
  }

  return out.join('\n')
}

export function saveText(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type: type || 'text/markdown' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
