# Baseline Audit — Geekatplay ComfyUI Asset Vault
Audited: 2026-08-22 · Target install: `O:\ComfyUI` · Pre-rebuild baseline

## Verdict
The app **installs** but **does not work**. A full scan against the real install crashes
before `conn.commit()`, so **all four tables are empty (0 rows)**. Nothing is ever indexed.

## Install / environment state — PASS
| Check | Result |
|---|---|
| `venv` Python | 3.12.10 |
| Backend deps (fastapi, uvicorn, pydantic, httpx, pillow, safetensors, numpy, pytest) | all installed |
| Node / npm | v22.22.0 / 10.9.4 |
| `frontend/node_modules` | present (37 pkgs) |
| `npm run build` | PASS — 1369 modules, 194 kB, 1.44 s |
| `pytest tests` | PASS — 3 passed |
| `from app.main import app` | imports OK |

## Real-install scale (drives every perf decision)
| Asset | Count |
|---|---|
| Model files | 231 (**1.5 TB** on disk) |
| Custom node packages | 35 |
| Output files | 3,569 images/videos |
| Workflow `.json` in `user/default/workflows` | 20 |
| `input/` files | 223 |

Categories: diffusion_models 61, loras 59, text_encoders 34, checkpoints 21, vae 19,
vae_approx 8, controlnet 8, geometry_estimation 6, frame_interpolation 6,
latent_upscale_models 5, clip 2, clip_vision 1, audio_encoders 1.

## BLOCKING DEFECTS (verified by execution, not inspection)

### B1 — Scan crashes; nothing is ever persisted
`sqlite3.ProgrammingError: Error binding parameter 8: type 'list' is not supported`
at `backend/app/services/scanner.py:220`.

Cause: in ComfyUI's API-format `prompt` metadata, `CLIPTextEncode.inputs.text` is often a
**node link**, not a string — e.g. `['88:97', 0]`. The scanner assigns it straight to
`prompt_text` and binds it to SQLite.
Measured: **126 of 400** sampled outputs carry a non-string `text` input.
Because `conn.commit()` is only reached at the very end, the models/nodes/workflows already
inserted are rolled back too. **Confirmed: 0 rows in `models`, `nodes`, `workflows`, `output_assets`.**

### B2 — Civitai enrichment can never match (AutoV2 hash is wrong)
Civitai's AutoV2 = first 10 hex chars of the **full-file SHA-256**. The implementation
(`safetensors_parser.compute_autov2_hash`) hashes only the **first 64 KB after the safetensors
header**. Verified on `flux2-vae-new.safetensors`:
- implementation → `E3B0C44298`  ← this is SHA-256 of the **empty string** (read fell past EOF)
- true AutoV2   → `5628B30A8E`
- **match: False**

Consequence: every advertised Civitai feature is dead — description, update alerts,
"what's new" benefits, download URL, trigger words, recommended sampler/CFG/steps, rating.
Note: full-file SHA-256 over 1.5 TB is not viable synchronously — needs a cached,
opt-in, background, resumable hashing job keyed on (path, size, mtime).

### B3 — Base-model detection largely wrong
Heuristic inspects only `tensor_keys[:100]`; safetensors key order is arbitrary, so the
discriminating keys are usually outside that window. Measured on real files:

| File | Detected | Correct |
|---|---|---|
| `flux1-dev-fp8.safetensors` | **VAE** | FLUX.1 checkpoint |
| `acestep_v1.5_xl_base_bf16` | Unknown | ACE-Step |
| `controlnet_tile_sdxl_1_0` | Unknown | SDXL ControlNet |
| most loras | Unknown | SD1.5 / SDXL / FLUX LoRA |

Param counts also sum *all* tensors in a bundled checkpoint (flux1-dev reported 16.87 B vs 12 B).

### B4 — Node class extraction fails on 66% of packages
`extract_node_classes_from_folder` only parses a literal `NODE_CLASS_MAPPINGS = {...}`
assignment, and only in `__init__.py` when it exists. Real suites build the mapping via
imports/`.update()`/merges across modules.
Measured: **21 of 32** packages yield **zero** node classes — including ComfyUI-KJNodes,
ComfyUI_IPAdapter_plus, ComfyUI-WanVideoWrapper, ComfyUI-Manager, ComfyMath.
This also poisons workflow analysis: with no known node classes, "missing nodes" is meaningless.

### B5 — `start_app.bat` never starts the backend
`python -m uvicorn app.main:app ... --cwd backend` → `Error: No such option '--cwd'`
(captured in `backend_log.txt`). uvicorn has no `--cwd` flag.

### B6 — `settings.COMFYUI_PATH` desyncs from the DB on restart
The wizard writes the path to the `config` table, but `settings.COMFYUI_PATH` is only mutated
in-process by `update_system_config`. After a restart it reverts to the `C:\ComfyUI` default while
the DB holds the real path. Downstream:
- `POST /api/system/reindex` scans the wrong (nonexistent) directory
- `is_safe_path` guards against the wrong root → **all rename/move/delete denied**
- `GET /api/outputs/file` → 403 → **no thumbnails render**

## DESIGN GAPS vs. the requested product
- **"Vector database" is not one.** `vector_embedding` columns exist but are never written.
  `/api/search` loads **every row of all four tables**, rebuilds the vocabulary from scratch, and
  re-vectorizes every document **on every keystroke** — O(corpus) per query. The FTS5 table
  `asset_fts` is created but never populated or queried.
- **No thumbnails.** The grid would stream 3,569 full-size originals through
  `/api/outputs/file`. Needs a generated thumbnail cache to hit the "extremely fast" bar.
- **Scan is single-threaded, non-incremental, non-resumable**, with no progress reporting, and
  runs inside the request handler (`await` on a fully blocking body).
- **Workflows under-discovered** — only `user/default/workflows`; ignores embedded graphs in
  `output/` PNGs and other workflow locations.
- **No per-file re-scan skip** — every reindex redoes all work regardless of mtime.
- Missing entirely: sort by group/album, list vs grid vs preview-size slider wired to real data,
  fullsize lightbox served from a thumbnail-backed grid, node/workflow deep detail, update checks
  for custom nodes (git upstream compare), and "where is this model used" workflow mapping.

## Measured performance headroom (the good news)
Correctness is the bottleneck, not speed. On the real install:
- walk 231 model files: **0.00 s**; parse headers + hash all 231: **0.23 s**
- analyze 35 custom-node packages: **0.02 s**
- walk 3,569 output files: **0.21 s**; PIL-open 400: 1.65 s → full set ≈ **15 s**

A correct, incremental, parallel scanner can index this install in seconds.
