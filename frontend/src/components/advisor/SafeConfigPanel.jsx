import React, { useCallback, useEffect, useState } from 'react'
import { Copy, Download, ShieldCheck, SlidersHorizontal } from 'lucide-react'
import api from '../../services/api.js'
import { useVault } from '../../state/VaultContext.jsx'
import Button from '../common/Button.jsx'
import Slider from '../common/Slider.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { BudgetBar } from './charts.jsx'
import { Est, gb, SectionHead } from './parts.jsx'

/*
 * SafeConfigPanel - the headroom, and the settings that enforce it.
 *
 * Two sliders are the whole configuration: how much GPU memory and how much
 * system memory the advisor must never plan to use. Everything below them is
 * derived from those two numbers and is text for the owner to copy - nothing
 * here writes a launcher or changes a setting on the machine.
 */

function copy(text, toast, what) {
  const done = () => toast({ tone: 'ok', title: 'Copied', message: what })
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => {})
  }
}

function save(name, content) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/plain' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function Rows({ items, nameKey }) {
  return (
    <ul className="gp-safe-list">
      {items.map((row) => (
        <li key={row[nameKey] + (row.value || '')} className="gp-safe-list__row">
          <code className="gp-safe-list__code">
            {row[nameKey]}{row.value !== null && row.value !== undefined
              ? (nameKey === 'name' ? '=' : ' ') + row.value : ''}
          </code>
          <span className="gp-safe-list__why">{row.why}</span>
        </li>
      ))}
    </ul>
  )
}

export default function SafeConfigPanel({ data, loading, error, settings, onChanged }) {
  const { toast, toastError } = useVault()
  const [vram, setVram] = useState(null)
  const [ram, setRam] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (settings) {
      setVram(settings.vram_headroom_pct)
      setRam(settings.ram_headroom_pct)
    }
  }, [settings])

  const dirty = settings && (vram !== settings.vram_headroom_pct ||
    ram !== settings.ram_headroom_pct)

  const apply = useCallback(() => {
    setSaving(true)
    api.advisorPatchSettings({ vram_headroom_pct: vram, ram_headroom_pct: ram })
      .then(() => {
        toast({ tone: 'ok', title: 'Headroom saved',
          message: 'Every verdict was recalculated.' })
        onChanged()
      })
      .catch((err) => toastError(err, 'The headroom could not be saved'))
      .finally(() => setSaving(false))
  }, [vram, ram, toast, toastError, onChanged])

  if (error) {
    return <EmptyState tone="error" title="The safe settings could not be built"
      text={error.message} />
  }
  if ((loading && !data) || !settings || vram === null) return <SkeletonRows rows={12} />
  if (!data) return null
  const b = data.budget
  const cpuOnly = b.backend === 'cpu'
  const limits = settings.limits || { min_headroom_pct: 5, max_headroom_pct: 50 }

  return (
    <div className="gp-adv">
      <section className="gp-inst-panel gp-inst-panel--vio">
        <SectionHead label="Headroom" sub="memory the advisor never plans to use"
          aside={<Button variant={dirty ? 'primary' : 'default'} size="sm"
            icon={ShieldCheck} label="Apply" disabled={!dirty} loading={saving}
            onClick={apply} />} />
        <p className="gp-safe-lead">
          <SlidersHorizontal aria-hidden="true" />
          <Est title="Worked out from the two headroom values below">{data.summary}</Est>
        </p>
        <div className="gp-safe-sliders">
          <label className="gp-safe-slider" htmlFor="adv-vram-headroom">
            <span className="gp-safe-slider__name">
              {b.unified ? 'GPU share of unified memory' : 'GPU memory'}
            </span>
            <Slider id="adv-vram-headroom" value={vram} min={limits.min_headroom_pct}
              max={limits.max_headroom_pct} step={1} disabled={cpuOnly}
              ariaLabel="GPU memory headroom, percent" valueLabel={vram + '%'}
              onChange={setVram} />
            {!cpuOnly ? (
              <BudgetBar total={b.vram_total} safe={b.vram_safe} label="GPU memory plan" />
            ) : null}
            <span className="gp-safe-slider__note">
              {cpuOnly ? 'No usable GPU on this machine.'
                : <>plan <Est>{gb(b.vram_safe)}</Est> of {gb(b.vram_total)}
                  {b.vram_reserve ? <> - {gb(b.vram_reserve)} is left to the system</> : null}</>}
            </span>
          </label>
          <label className="gp-safe-slider" htmlFor="adv-ram-headroom">
            <span className="gp-safe-slider__name">System memory</span>
            <Slider id="adv-ram-headroom" value={ram} min={limits.min_headroom_pct}
              max={limits.max_headroom_pct} step={1}
              ariaLabel="System memory headroom, percent" valueLabel={ram + '%'}
              onChange={setRam} />
            <BudgetBar total={b.ram_total} safe={b.ram_safe + b.ram_reserve}
              label="System memory plan" />
            <span className="gp-safe-slider__note">
              plan <Est>{gb(b.ram_safe)}</Est> for models, {gb(b.ram_reserve)} for the system,
              of {gb(b.ram_total, 0)}
            </span>
          </label>
        </div>
        {dirty ? (
          <p className="gp-inst-note">
            The plan above still shows the saved values. Apply to recalculate
            every verdict with the new headroom.
          </p>
        ) : null}
      </section>

      <section className="gp-inst-panel gp-inst-panel--flat">
        <SectionHead label="ComfyUI launch arguments"
          aside={data.comfyui.command_line ? (
            <Button variant="ghost" size="sm" icon={Copy} label="Copy"
              onClick={() => copy(data.comfyui.command_line, toast, data.comfyui.command_line)} />
          ) : null} />
        {data.comfyui.command_line
          ? <code className="gp-code gp-safe-cmd">{data.comfyui.command_line}</code>
          : <p className="gp-inst-note">No extra arguments are needed on this machine.</p>}
        {data.comfyui.flags.length ? <Rows items={data.comfyui.flags} nameKey="flag" /> : null}
        {data.comfyui.env.length ? (
          <>
            <div className="gp-inst-sub">Environment</div>
            <Rows items={data.comfyui.env} nameKey="name" />
          </>
        ) : null}
        {data.comfyui.notes.map((n) => <p key={n} className="gp-inst-note">{n}</p>)}
      </section>

      <div className="gp-inst-row gp-inst-row--two">
        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="Ollama" />
          <Rows items={data.ollama.env} nameKey="name" />
        </section>
        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="llama.cpp"
            aside={<Button variant="ghost" size="sm" icon={Copy} label="Copy"
              onClick={() => copy(data.llama_cpp.command_line, toast,
                data.llama_cpp.command_line)} />} />
          <code className="gp-code gp-safe-cmd">{data.llama_cpp.command_line}</code>
          <Rows items={data.llama_cpp.args} nameKey="flag" />
        </section>
      </div>

      <section className="gp-inst-panel gp-inst-panel--flat">
        <SectionHead label="Files to keep" sub="saved by your browser, nothing is written for you" />
        <div className="gp-safe-files">
          {data.files.map((f) => (
            <div key={f.name} className="gp-safe-file">
              <div className="gp-safe-file__head">
                <span className="gp-u-fw-600 gp-u-num">{f.name}</span>
                <span className="gp-u-auto-l" />
                <Button variant="ghost" size="sm" icon={Copy} label="Copy"
                  onClick={() => copy(f.content, toast, f.name)} />
                <Button size="sm" icon={Download} label="Save"
                  onClick={() => save(f.name, f.content)} />
              </div>
              <pre className="gp-code gp-safe-pre">{f.content}</pre>
            </div>
          ))}
        </div>
      </section>

      <section className="gp-inst-panel gp-inst-panel--flat">
        <SectionHead label="Rules of thumb" />
        <ul className="gp-advd-reasons">
          {data.rules.map((r) => <li key={r}>{r}</li>)}
        </ul>
      </section>
    </div>
  )
}
