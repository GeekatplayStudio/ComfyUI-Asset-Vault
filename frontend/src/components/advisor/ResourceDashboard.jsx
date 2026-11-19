import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  Cpu, MemoryStick, Zap, CircuitBoard, HardDrive, Thermometer, Fan, Flame,
  ArrowDownToLine, ArrowUpFromLine, RefreshCw, Server, Activity
} from 'lucide-react'
import api from '../../services/api.js'
import Button from '../common/Button.jsx'
import Badge from '../common/Badge.jsx'
import EmptyState from '../common/EmptyState.jsx'
import { SkeletonRows } from '../common/Skeleton.jsx'
import { bytes as fmtBytes, percent } from '../../services/format.js'
import { Gauge, Sparkline, CoreBars, BudgetBar, loadTone } from './charts.jsx'
import { Est, gb, rate, SectionHead } from './parts.jsx'

/*
 * ResourceDashboard - what this machine is doing right now.
 *
 * Order is deliberate, most glanceable first:
 *   1. the machine itself - class, accelerator, the plan in one sentence;
 *   2. four live gauges - processor, memory, GPU, GPU memory - each with the
 *      last three minutes behind it;
 *   3. the memory plan drawn to scale: what is in use, where the safe limit
 *      sits, and the headroom that is never planned for;
 *   4. cores, throughput, volumes, and what is holding the memory.
 *
 * Every reading here is measured, so the dashboard is amber. The two things
 * that are not readings - the safe limit and the headroom zone - are violet.
 *
 * Polling, not a stream: one small request a second while the tab is visible,
 * none at all while the window is hidden.
 */

const POLL_MS = 1000
const HISTORY = 180

function useLive() {
  const [sample, setSample] = useState(null)
  const [history, setHistory] = useState([])
  const [error, setError] = useState(null)
  const first = useRef(true)

  useEffect(() => {
    let disposed = false
    let timer = null
    const controller = new AbortController()

    const tick = () => {
      if (document.hidden) {
        timer = setTimeout(tick, POLL_MS)
        return
      }
      api.advisorLive(first.current ? true : undefined, controller.signal)
        .then((next) => {
          if (disposed) return
          setError(null)
          setSample(next)
          if (first.current && next.history) {
            first.current = false
            setHistory(next.history.slice(-HISTORY))
          } else {
            first.current = false
            const gpu = (next.gpus && next.gpus[0]) || {}
            const point = {
              t: next.t,
              cpu: next.cpu.percent,
              mem: next.memory.percent,
              gpu: gpu.util_percent,
              vram: gpu.vram_total_bytes && gpu.vram_used_bytes !== null &&
                gpu.vram_used_bytes !== undefined
                ? (gpu.vram_used_bytes / gpu.vram_total_bytes) * 100 : null,
              disk_r: next.disk_io.read_bps, disk_w: next.disk_io.write_bps,
              net_rx: next.net_io.rx_bps, net_tx: next.net_io.tx_bps
            }
            setHistory((prev) => (prev.length && prev[prev.length - 1].t === point.t
              ? prev : prev.concat(point).slice(-HISTORY)))
          }
        })
        .catch((err) => {
          if (!disposed && err.name !== 'AbortError') setError(err)
        })
        .finally(() => {
          if (!disposed) timer = setTimeout(tick, POLL_MS)
        })
    }
    tick()
    return () => {
      disposed = true
      controller.abort()
      if (timer) clearTimeout(timer)
    }
  }, [])

  return { sample, history, error }
}

function series(history, key) {
  return history.map((p) => (p[key] === undefined ? null : p[key]))
}

function Stat({ icon: Icon, label, value, sub, tone }) {
  return (
    <div className="gp-inst-stat">
      {Icon ? <Icon className={'gp-inst-stat__icon' + (tone ? ' gp-u-' + tone : '')}
        aria-hidden="true" /> : null}
      <div className="gp-u-minw0">
        <div className="gp-inst-stat__label">{label}</div>
        <div className="gp-inst-stat__value">{value}</div>
        {sub ? <div className="gp-inst-stat__sub">{sub}</div> : null}
      </div>
    </div>
  )
}

function Hero({ hardware, onRefresh, refreshing }) {
  const acc = hardware.accelerator || {}
  const cpu = hardware.cpu || {}
  const tier = hardware.tier || {}
  const budget = hardware.budget || {}
  const cores = cpu.performance_cores
    ? cpu.performance_cores + 'P + ' + (cpu.efficiency_cores || 0) + 'E cores'
    : (cpu.physical_cores || '?') + ' cores / ' + (cpu.logical_cores || '?') + ' threads'
  const vramText = acc.backend === 'cpu' ? 'No usable GPU'
    : (acc.unified ? 'unified, ' : '') + gb(acc.vram_total_bytes)

  return (
    <section className="gp-inst-hero" aria-label="This machine">
      <div className="gp-inst-hero__main">
        <div className="gp-inst-hero__eyebrow">
          <span className="gp-inst-live" aria-hidden="true" />
          This machine
          <span className="gp-inst-hero__os">{hardware.os.name} {hardware.os.version}</span>
        </div>
        <h2 className="gp-inst-hero__title">
          <Est title="A plain-language class for this machine, from its GPU memory">
            {tier.label || 'Unclassified'}
          </Est>
        </h2>
        <p className="gp-inst-hero__text">{tier.summary}</p>
        <div className="gp-inst-hero__chips">
          <Badge mono title="Accelerator the advisor plans against">
            {acc.name || 'CPU only'}
          </Badge>
          <Badge mono tone="brand" title={acc.vram_is_estimate
            ? 'GPU share of unified memory - macOS publishes no fixed figure'
            : 'Dedicated GPU memory, read from the driver'}>
            {acc.vram_is_estimate ? <Est>{vramText}</Est> : vramText}
          </Badge>
          <Badge mono>{String(acc.backend || 'cpu').toUpperCase()}</Badge>
          {acc.gpu_count > 1 ? <Badge mono>{acc.gpu_count} GPUs</Badge> : null}
        </div>
      </div>

      <div className="gp-inst-hero__specs">
        <Stat icon={Cpu} label="Processor" value={cpu.model}
          sub={cores + ((cpu.features || []).length ? ' - ' + cpu.features.join(', ') : '')} />
        <Stat icon={MemoryStick} label="System memory"
          value={gb(hardware.memory.total_bytes, 0)}
          sub={<>plan against <Est>{gb(budget.ram_safe)}</Est></>} />
        <Stat icon={Zap} label="GPU memory plan"
          value={acc.backend === 'cpu' ? 'none' : <Est>{gb(budget.vram_safe)}</Est>}
          sub={acc.backend === 'cpu' ? 'models run from system memory'
            : budget.vram_headroom_pct + '% headroom of ' + gb(budget.vram_total)} />
      </div>

      <Button
        className="gp-inst-hero__refresh" variant="ghost" size="sm" icon={RefreshCw}
        label="Re-scan hardware" loading={refreshing} onClick={onRefresh}
        title="Probe the processor, memory, GPUs and drives again"
      />
    </section>
  )
}

function GaugeCard({ children, history, tone, limit, label, footer }) {
  return (
    <div className="gp-inst-card">
      {children}
      <Sparkline
        series={[{ key: 'a', values: history, tone: tone || 'amber' }]}
        max={100} limit={limit} label={label + ', last three minutes'}
      />
      {footer ? <div className="gp-inst-card__foot">{footer}</div> : null}
    </div>
  )
}

export default function ResourceDashboard({ hardware, onRefresh, refreshing, error }) {
  const { sample, history, error: liveError } = useLive()

  const cpuSeries = useMemo(() => series(history, 'cpu'), [history])
  const memSeries = useMemo(() => series(history, 'mem'), [history])
  const gpuSeries = useMemo(() => series(history, 'gpu'), [history])
  const vramSeries = useMemo(() => series(history, 'vram'), [history])
  const ioSeries = useMemo(() => [
    { key: 'r', values: series(history, 'disk_r'), tone: 'amber' },
    { key: 'w', values: series(history, 'disk_w'), tone: 'info' }
  ], [history])
  const netSeries = useMemo(() => [
    { key: 'rx', values: series(history, 'net_rx'), tone: 'amber' },
    { key: 'tx', values: series(history, 'net_tx'), tone: 'info' }
  ], [history])

  if (error && !hardware) {
    return (
      <EmptyState tone="error" title="The hardware profile could not be read"
        text={error.message}
        actions={<Button variant="primary" onClick={onRefresh}>Try again</Button>} />
    )
  }
  if (!hardware) return <SkeletonRows rows={10} />

  const acc = hardware.accelerator || {}
  const budget = hardware.budget || {}
  const cpu = (sample && sample.cpu) || {}
  const mem = (sample && sample.memory) || {}
  const gpus = (sample && sample.gpus) || []
  const gpu = gpus[0] || null
  const hasGpu = acc.backend !== 'cpu'
  const vramPct = gpu && gpu.vram_total_bytes && gpu.vram_used_bytes !== null &&
    gpu.vram_used_bytes !== undefined
    ? (gpu.vram_used_bytes / gpu.vram_total_bytes) * 100 : null
  const vramSafePct = budget.vram_total ? (budget.vram_safe / budget.vram_total) * 100 : null
  const ramPlan = budget.ram_safe + budget.ram_reserve
  const ramSafePct = budget.ram_total ? (ramPlan / budget.ram_total) * 100 : null
  const swapPct = mem.swap_total_bytes
    ? ((mem.swap_used_bytes || 0) / mem.swap_total_bytes) * 100 : null
  const volumes = (sample && sample.volumes) || hardware.volumes || []
  const top = (sample && sample.top_processes) || []
  const gpuProcs = (gpu && gpu.processes) || []
  const thermals = gpu && (gpu.temp_c !== null || gpu.power_w !== null || gpu.fan_percent !== null)

  return (
    <div className="gp-inst">
      <Hero hardware={hardware} onRefresh={onRefresh} refreshing={refreshing} />

      {liveError ? (
        <p className="gp-u-fs-11 gp-u-danger gp-u-mb-5">
          Live readings paused: {liveError.message}
        </p>
      ) : null}

      <div className="gp-inst-gauges">
        <GaugeCard history={cpuSeries} label="Processor load"
          footer={<>
            {cpu.freq_mhz ? <span>{(cpu.freq_mhz / 1000).toFixed(2)} GHz</span> : null}
            {cpu.load_1m !== null && cpu.load_1m !== undefined
              ? <span>load {cpu.load_1m}</span> : null}
            {cpu.temp_c ? <span>{cpu.temp_c} &deg;C</span> : null}
          </>}>
          <Gauge label="Processor" icon={Cpu} value={cpu.percent} unit="%"
            warnAt={85} dangerAt={96} />
        </GaugeCard>

        <GaugeCard history={memSeries} label="System memory" limit={ramSafePct}
          footer={<>
            <span>{gb(mem.used_bytes)} of {gb(mem.total_bytes, 0)}</span>
            {swapPct !== null ? (
              <span className={swapPct > 50 ? 'gp-u-warn' : undefined}>
                swap {gb(mem.swap_used_bytes)}
              </span>
            ) : null}
          </>}>
          <Gauge label="Memory" icon={MemoryStick} value={mem.percent} unit="%"
            safePct={ramSafePct} warnAt={ramSafePct || 80} dangerAt={94} />
        </GaugeCard>

        <GaugeCard history={gpuSeries} label="GPU load"
          footer={hasGpu ? (
            <span className="gp-u-truncate">{acc.name}</span>
          ) : <span>no usable GPU</span>}>
          <Gauge label="GPU" icon={CircuitBoard}
            value={hasGpu && gpu ? gpu.util_percent : null} unit="%"
            warnAt={101} dangerAt={101}
            title={hasGpu && gpu && gpu.util_percent === null
              ? 'This driver does not publish a utilisation figure' : undefined} />
        </GaugeCard>

        <GaugeCard history={vramSeries} label="GPU memory" limit={vramSafePct}
          footer={hasGpu && gpu ? (
            <>
              <span>
                {gpu.vram_used_bytes !== null && gpu.vram_used_bytes !== undefined
                  ? gb(gpu.vram_used_bytes) : '--'} of {acc.vram_is_estimate
                  ? <Est>{gb(gpu.vram_total_bytes)}</Est> : gb(gpu.vram_total_bytes)}
              </span>
              <span>safe <Est>{gb(budget.vram_safe)}</Est></span>
            </>
          ) : <span>planned from system memory</span>}>
          <Gauge label={acc.unified ? 'GPU share' : 'VRAM'} icon={Zap}
            value={hasGpu ? vramPct : null} unit="%"
            safePct={hasGpu ? vramSafePct : null}
            warnAt={vramSafePct || 85} dangerAt={96} />
        </GaugeCard>
      </div>

      <div className="gp-inst-row">
        <section className="gp-inst-panel gp-inst-panel--wide">
          <SectionHead label="Memory plan" sub="drawn to scale"
            aside={<span className="gp-inst-legend">
              <span className="gp-inst-legend__item"><i className="gp-inst-legend__sw gp-inst-legend__sw--used" />in use</span>
              <span className="gp-inst-legend__item"><i className="gp-inst-legend__sw gp-inst-legend__sw--safe" />safe limit</span>
              <span className="gp-inst-legend__item"><i className="gp-inst-legend__sw gp-inst-legend__sw--head" />headroom, never planned</span>
            </span>} />
          {hasGpu ? (
            <div className="gp-inst-plan">
              <div className="gp-inst-plan__head">
                <span className="gp-inst-plan__name">
                  {acc.unified ? 'GPU share of unified memory' : 'GPU memory'}
                </span>
                <span className="gp-inst-plan__nums">
                  {gpu && gpu.vram_used_bytes !== null && gpu.vram_used_bytes !== undefined
                    ? gb(gpu.vram_used_bytes) + ' in use - ' : ''}
                  plan <Est>{gb(budget.vram_safe)}</Est> of {gb(budget.vram_total)}
                </span>
              </div>
              <BudgetBar total={budget.vram_total}
                used={gpu ? gpu.vram_used_bytes : null} safe={budget.vram_safe}
                label="GPU memory" />
            </div>
          ) : null}
          <div className="gp-inst-plan">
            <div className="gp-inst-plan__head">
              <span className="gp-inst-plan__name">System memory</span>
              <span className="gp-inst-plan__nums">
                {mem.used_bytes !== null && mem.used_bytes !== undefined
                  ? gb(mem.used_bytes) + ' in use - ' : ''}
                plan <Est>{gb(ramPlan)}</Est> of {gb(budget.ram_total, 0)}
              </span>
            </div>
            <BudgetBar total={budget.ram_total} used={mem.used_bytes} safe={ramPlan}
              label="System memory" />
          </div>
          <p className="gp-inst-note">
            The advisor never plans to fill memory. The hatched stretch is kept
            free for the desktop, a larger image, one more adapter - change it
            under Safe settings.
          </p>
        </section>

        <section className="gp-inst-panel">
          <SectionHead label="Processor cores"
            sub={cpu.percent !== null && cpu.percent !== undefined
              ? percent(cpu.percent) + ' average' : null} />
          {cpu.per_core && cpu.per_core.length ? (
            <CoreBars cores={cpu.per_core}
              performance={hardware.cpu.performance_cores} />
          ) : (
            <p className="gp-u-fs-11 gp-u-meta">
              Per-core load needs the psutil package. Run the dependency
              installer to enable it.
            </p>
          )}
          {thermals ? (
            <div className="gp-inst-thermals">
              {gpu.temp_c !== null && gpu.temp_c !== undefined ? (
                <Stat icon={Thermometer} label="GPU temperature"
                  tone={loadTone(gpu.temp_c, 78, 88) === 'amber' ? null
                    : loadTone(gpu.temp_c, 78, 88)}
                  value={Math.round(gpu.temp_c) + ' °C'} />
              ) : null}
              {gpu.power_w !== null && gpu.power_w !== undefined ? (
                <Stat icon={Flame} label="GPU power" value={Math.round(gpu.power_w) + ' W'} />
              ) : null}
              {gpu.fan_percent !== null && gpu.fan_percent !== undefined ? (
                <Stat icon={Fan} label="Fan" value={Math.round(gpu.fan_percent) + '%'} />
              ) : null}
            </div>
          ) : null}
        </section>
      </div>

      <div className="gp-inst-row">
        <section className="gp-inst-panel">
          <SectionHead label="Disk throughput" />
          <div className="gp-inst-io">
            <Stat icon={ArrowDownToLine} label="Read"
              value={rate(sample && sample.disk_io.read_bps)} />
            <Stat icon={ArrowUpFromLine} label="Write" tone="info"
              value={rate(sample && sample.disk_io.write_bps)} />
          </div>
          <Sparkline series={ioSeries} height={48} className="gp-spark--tall"
            label="Disk read and write, last three minutes" />
        </section>

        <section className="gp-inst-panel">
          <SectionHead label="Network" />
          <div className="gp-inst-io">
            <Stat icon={ArrowDownToLine} label="Down"
              value={rate(sample && sample.net_io.rx_bps)} />
            <Stat icon={ArrowUpFromLine} label="Up" tone="info"
              value={rate(sample && sample.net_io.tx_bps)} />
          </div>
          <Sparkline series={netSeries} height={48} className="gp-spark--tall"
            label="Network down and up, last three minutes" />
        </section>

        <section className="gp-inst-panel">
          <SectionHead label="Holding the memory" count={top.length || null} />
          {top.length ? (
            <ul className="gp-inst-procs">
              {top.map((p) => (
                <li key={p.pid} className="gp-inst-procs__row">
                  <span className="gp-u-truncate" title={p.name}>{p.name}</span>
                  <span className="gp-inst-procs__bar">
                    <span style={{ '--gp-adv-v': Math.min(100,
                      (p.rss_bytes / (top[0].rss_bytes || 1)) * 100) + '%' }} />
                  </span>
                  <span className="gp-u-num">{fmtBytes(p.rss_bytes)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="gp-u-fs-11 gp-u-meta">No process list available.</p>
          )}
          {gpuProcs.length ? (
            <>
              <div className="gp-inst-sub">On the GPU</div>
              <ul className="gp-inst-procs">
                {gpuProcs.map((p) => (
                  <li key={'g' + p.pid} className="gp-inst-procs__row">
                    <span className="gp-u-truncate" title={p.name}>{p.name}</span>
                    <span className="gp-inst-procs__bar">
                      <span style={{ '--gp-adv-v': Math.min(100,
                        ((p.vram_bytes || 0) / (gpuProcs[0].vram_bytes || 1)) * 100) + '%' }} />
                    </span>
                    <span className="gp-u-num">{fmtBytes(p.vram_bytes || 0)}</span>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
      </div>

      <section className="gp-inst-panel gp-inst-panel--flat">
        <SectionHead label="Volumes" count={volumes.length} />
        <div className="gp-inst-volumes">
          {volumes.map((v) => {
            const pct = v.percent !== undefined && v.percent !== null ? v.percent
              : (v.total_bytes ? (v.used_bytes / v.total_bytes) * 100 : 0)
            const tone = loadTone(pct, 85, 94)
            return (
              <div className="gp-inst-volume" key={v.mount}>
                <div className="gp-inst-volume__head">
                  <HardDrive className="gp-inst-stat__icon" aria-hidden="true" />
                  <span className="gp-u-fw-600 gp-u-num gp-u-truncate" title={v.mount}>
                    {v.mount}
                  </span>
                  {(v.roles || []).map((r) => <Badge key={r} tone="brand">{r}</Badge>)}
                  {v.kind && v.kind !== 'unknown'
                    ? <Badge mono>{String(v.kind).toUpperCase()}</Badge> : null}
                  <span className={'gp-inst-volume__pct gp-u-num' +
                    (tone !== 'amber' ? ' gp-u-' + tone : '')}>
                    {percent(pct)}
                  </span>
                </div>
                <div className={'gp-inst-bar gp-inst-bar--' + tone}>
                  <span style={{ '--gp-adv-v': Math.min(100, pct) + '%' }} />
                </div>
                <div className="gp-u-fs-11 gp-u-meta gp-u-num">
                  <span className="gp-u-fg gp-u-fw-600">{fmtBytes(v.free_bytes)}</span>
                  {' free of '}{fmtBytes(v.total_bytes)}
                </div>
              </div>
            )
          })}
        </div>
      </section>

      {(hardware.gpus || []).length > 1 || (hardware.notes || []).length ? (
        <section className="gp-inst-panel gp-inst-panel--flat">
          <SectionHead label="Detected hardware" />
          {(hardware.gpus || []).length > 1 ? (
            <ul className="gp-inst-gpus">
              {hardware.gpus.map((g) => (
                <li key={g.index + g.name} className="gp-inst-gpus__row">
                  <Server className="gp-inst-stat__icon" aria-hidden="true" />
                  <span className="gp-u-truncate">{g.name}</span>
                  <Badge mono>{g.integrated ? 'integrated' : String(g.backend).toUpperCase()}</Badge>
                  <span className="gp-u-num gp-u-auto-l">
                    {g.vram_total_bytes ? gb(g.vram_total_bytes) : 'shared memory'}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
          {(hardware.notes || []).map((note) => (
            <p key={note} className="gp-inst-note">
              <Activity className="gp-inst-note__icon" aria-hidden="true" />{note}
            </p>
          ))}
        </section>
      ) : null}
    </div>
  )
}
