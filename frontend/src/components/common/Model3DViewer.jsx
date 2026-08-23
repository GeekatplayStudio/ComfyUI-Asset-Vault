import React, { useEffect, useRef, useState } from 'react'
import { AlertTriangle, RotateCw } from 'lucide-react'
import api, { rawUrl } from '../../services/api.js'

/*
 * A 3D preview -- orbit, zoom, auto-rotate. Not an editor.
 *
 * Deliberately plain three.js rather than react-three-fiber + drei: this is one
 * canvas with one model in it, and the reconciler and helper library would cost
 * more than they save here. `three` itself is imported dynamically, so nothing
 * about it reaches the main bundle until someone actually opens a 3D asset.
 */

/** Extensions we can actually put on screen. */
const LOADABLE = new Set(['.glb', '.gltf', '.fbx'])

export function is3D(mediaKind) {
  return mediaKind === 'model3d'
}

export function canPreview3D(ext) {
  return LOADABLE.has(String(ext || '').toLowerCase())
}

/** Past this, decoding in a browser tab is slow enough to warn about first. */
const HEAVY_BYTES = 60 * 1024 * 1024

export default function Model3DViewer({ uid, ext, sizeBytes, className = '',
  captureThumbnail = false }) {
  const hostRef = useRef(null)
  const [status, setStatus] = useState('idle')   // idle | loading | ready | error
  const [error, setError] = useState(null)
  const [spin, setSpin] = useState(true)
  const spinRef = useRef(true)
  const [confirmed, setConfirmed] = useState(false)

  useEffect(() => { spinRef.current = spin }, [spin])

  const heavy = Number(sizeBytes) > HEAVY_BYTES
  const supported = canPreview3D(ext)
  const start = supported && (!heavy || confirmed)

  useEffect(() => {
    if (!start) return undefined
    const host = hostRef.current
    if (!host) return undefined

    let disposed = false
    let cleanup = () => {}
    setStatus('loading')
    setError(null)

    ;(async () => {
      try {
        const THREE = await import('three')
        const { OrbitControls } =
          await import('three/examples/jsm/controls/OrbitControls.js')

        const lower = String(ext).toLowerCase()
        let loader
        if (lower === '.fbx') {
          const { FBXLoader } = await import('three/examples/jsm/loaders/FBXLoader.js')
          loader = new FBXLoader()
        } else {
          const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js')
          loader = new GLTFLoader()
        }
        if (disposed) return

        const width = host.clientWidth || 480
        const height = host.clientHeight || 320

        const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
        renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
        renderer.setSize(width, height, false)
        renderer.domElement.style.width = '100%'
        renderer.domElement.style.height = '100%'
        renderer.domElement.style.display = 'block'
        host.appendChild(renderer.domElement)

        const scene = new THREE.Scene()
        const camera = new THREE.PerspectiveCamera(50, width / height, 0.01, 5000)
        camera.position.set(0, 0, 5)

        scene.add(new THREE.AmbientLight(0xffffff, 0.9))
        const key = new THREE.DirectionalLight(0xffffff, 2.0)
        key.position.set(5, 6, 5)
        scene.add(key)
        const rim = new THREE.DirectionalLight(0xffffff, 0.7)
        rim.position.set(-5, 2, -4)
        scene.add(rim)

        const controls = new OrbitControls(camera, renderer.domElement)
        controls.enableDamping = true
        controls.autoRotateSpeed = 2.4

        const loaded = await loader.loadAsync(rawUrl(uid))
        if (disposed) { renderer.dispose(); return }
        const object = loaded.scene || loaded.scenes?.[0] || loaded

        // Frame the model: centre it on the origin and pull the camera back far
        // enough that its bounding sphere fits the vertical field of view.
        const box = new THREE.Box3().setFromObject(object)
        const size = box.getSize(new THREE.Vector3())
        const centre = box.getCenter(new THREE.Vector3())
        object.position.sub(centre)
        scene.add(object)

        const radius = Math.max(size.x, size.y, size.z) || 1
        const dist = (radius / 2) / Math.tan((camera.fov * Math.PI) / 360)
        camera.position.set(0, radius * 0.15, dist * 2.1)
        camera.near = Math.max(dist / 1000, 0.01)
        camera.far = dist * 100
        camera.updateProjectionMatrix()
        controls.minDistance = dist * 0.4
        controls.maxDistance = dist * 8
        controls.target.set(0, 0, 0)
        controls.update()

        const onResize = () => {
          const w = host.clientWidth || width
          const h = host.clientHeight || height
          renderer.setSize(w, h, false)
          camera.aspect = w / h
          camera.updateProjectionMatrix()
        }
        const ro = typeof ResizeObserver !== 'undefined'
          ? new ResizeObserver(onResize) : null
        if (ro) ro.observe(host)

        let frame = 0
        const tick = () => {
          frame = requestAnimationFrame(tick)
          controls.autoRotate = spinRef.current
          controls.update()
          renderer.render(scene, camera)
        }
        tick()
        setStatus('ready')

        /* Hand a poster frame back to the vault, once.
           There is no server-side GL stack, and the browser has already paid
           the cost of loading this model in order to show it -- so the grid
           gets a real thumbnail out of work that is already done, instead of
           the app growing a headless renderer to draw a picture. */
        if (captureThumbnail) {
          setTimeout(() => {
            if (disposed) return
            try {
              /* Render into a WebGLRenderTarget on the EXISTING context and
                 read the pixels back, rather than spinning up a second
                 WebGLRenderer.  A browser caps how many live WebGL contexts a
                 page may hold, and asking for another one is refused on a page
                 that has already opened a few -- silently, which is how this
                 first went wrong.  Reusing the context also means the main
                 renderer does not need preserveDrawingBuffer. */
              const S = 512
              const rt = new THREE.WebGLRenderTarget(S, S)

              const cam = new THREE.PerspectiveCamera(50, 1, 0.01, 5000)
              const fit = (radius / 2) / Math.tan((cam.fov * Math.PI) / 360)
              cam.position.set(radius * 0.55, radius * 0.42, fit * 1.35)
              cam.near = Math.max(fit / 1000, 0.01)
              cam.far = fit * 100
              cam.lookAt(0, 0, 0)
              cam.updateProjectionMatrix()

              const prevBg = scene.background
              scene.background = new THREE.Color(0x14141a)
              renderer.setRenderTarget(rt)
              renderer.render(scene, cam)
              const buf = new Uint8Array(S * S * 4)
              renderer.readRenderTargetPixels(rt, 0, 0, S, S, buf)
              renderer.setRenderTarget(null)
              scene.background = prevBg
              rt.dispose()

              // readRenderTargetPixels hands back rows bottom-up; the 2D
              // canvas wants them top-down.
              const flipped = new Uint8ClampedArray(S * S * 4)
              const stride = S * 4
              for (let y = 0; y < S; y++) {
                flipped.set(buf.subarray(y * stride, y * stride + stride),
                            (S - 1 - y) * stride)
              }
              const cv = document.createElement('canvas')
              cv.width = S; cv.height = S
              cv.getContext('2d').putImageData(new ImageData(flipped, S, S), 0, 0)
              const png = cv.toDataURL('image/png')
              if (png && png.length > 512) {
                api.putRenderedThumbnail(uid, png).catch(() => {
                  /* a poster is a nicety; never surface a failure for it */
                })
              }
            } catch { /* no poster this time; the placeholder stands */ }
          }, 700)
        }

        cleanup = () => {
          cancelAnimationFrame(frame)
          if (ro) ro.disconnect()
          controls.dispose()
          scene.traverse((n) => {
            if (n.geometry) n.geometry.dispose()
            const mats = Array.isArray(n.material) ? n.material : (n.material ? [n.material] : [])
            for (const m of mats) {
              for (const k of Object.keys(m)) {
                const v = m[k]
                if (v && v.isTexture) v.dispose()
              }
              m.dispose()
            }
          })
          renderer.dispose()
          if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement)
        }
      } catch (err) {
        if (!disposed) {
          setStatus('error')
          setError(err && err.message ? err.message : String(err))
        }
      }
    })()

    return () => { disposed = true; cleanup() }
  }, [uid, ext, start, captureThumbnail])

  if (!supported) {
    return (
      <div className={'gp-3d gp-3d--msg ' + className}>
        <AlertTriangle className="gp-3d__icon" aria-hidden="true" />
        <p>Interactive preview supports GLB, glTF and FBX.</p>
      </div>
    )
  }

  if (heavy && !confirmed) {
    return (
      <div className={'gp-3d gp-3d--msg ' + className}>
        <AlertTriangle className="gp-3d__icon" aria-hidden="true" />
        <p>
          This model is {(Number(sizeBytes) / 1024 / 1024).toFixed(0)} MB.
          Loading it in the browser may take a moment.
        </p>
        <button type="button" className="gp-btn gp-btn--sm"
          onClick={() => setConfirmed(true)}>
          <span className="gp-btn__label">Load it anyway</span>
        </button>
      </div>
    )
  }

  return (
    <div className={'gp-3d ' + className}>
      <div ref={hostRef} className="gp-3d__canvas" />
      {status === 'loading' ? <div className="gp-3d__note">Loading 3D…</div> : null}
      {status === 'error' ? (
        <div className="gp-3d__error">
          <AlertTriangle className="gp-3d__icon" aria-hidden="true" />
          <p>Could not load this model.</p>
          {error ? <code className="gp-3d__detail">{error}</code> : null}
        </div>
      ) : null}
      {status === 'ready' ? (
        <>
          <button
            type="button"
            className={'gp-3d__spin' + (spin ? ' gp-3d__spin--on' : '')}
            onClick={(e) => { e.stopPropagation(); setSpin((v) => !v) }}
            aria-pressed={spin}
            title={spin ? 'Stop auto-rotate' : 'Auto-rotate'}
          >
            <RotateCw aria-hidden="true" />
          </button>
          <div className="gp-3d__hint">Drag to orbit · scroll to zoom</div>
        </>
      ) : null}
    </div>
  )
}
