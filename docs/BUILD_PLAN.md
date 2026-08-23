# Build Plan — Geekatplay ComfyUI Asset Vault v2.0
Work breakdown for the downstream agents. **File ownership is exclusive**: an agent may only create/modify files it owns. Cross-boundary changes are requested from the owner, never made directly.

> **Note on agent count.** The brief says "six downstream implementation agents" and then names eight roles. This plan treats it as **eight roles executed in six waves**, with `ui-design`+`ui-dev` running as a paired wave and `security`+`qa` as another. If the runtime truly supports only six concurrent agents, merge `ui-design` into `ui-dev` (tokens become a deliverable of the same agent) and `security` into `qa`.

---

## 0. Blocking decisions — resolve BEFORE parallel work starts

| # | Decision | Default chosen by the architect | Who confirms |
|---|---|---|---|
| **D1** | **Port change 8000 → 8127** | Adopt 8127 (8000 collides constantly). Touches `start_app.bat`, `vite.config.js`, README, MCP client config. | user / coordinator |
| **D2** | **DB file rename** `asset_vault.db` → `vault.db` with a one-time config import | Adopt. Legacy DB has 0 asset rows; only `config` is worth importing. | coordinator |
| **D3** | **ONNX embedder shipping model** | Download on first explicit enable from HuggingFace `Xenova/all-MiniLM-L6-v2` (~23 MB INT8); manual offline placement supported. Not vendored in the repo (license + size). | user |
| **D4** | **`extra_model_paths.yaml.hold` is NOT read** | Ignore `.hold`/`.example`; expose `read_held_extra_paths` toggle, default OFF. The target machine has no active YAML. | user |
| **D5** | **Root-level `O:\ComfyUI\workflows` is scanned** (28 files) alongside `user\default\workflows` (20) | Adopt — both are real. | user |
| **D6** | **No frame extraction for video thumbnails** without a user-installed `ffmpeg` on PATH | Adopt; placeholder + duration/resolution from MP4 boxes. | coordinator |
| **D7** | **MCP exposes no destructive tools** | Adopt. Only `vault_reindex` mutates. | user |
| **D8** | **API is `/api/v1`; the old `/api/*` routes are deleted, not aliased** | Adopt — the frontend ships in the same wave. | coordinator |
| **D9** | **New Python deps**: `PyYAML`, `onnxruntime`, `tokenizers` (+dev `pytest-asyncio`, `ruff`) | Adopt (ARCHITECTURE §11). | user |
| **D10** | **Geekatplay palette = Studio Graphite + Signal Amber** (amber primary, violet secondary), not cyan | Adopt (ARCHITECTURE §7.1). | user |

`ui-design` cannot start until **D10** is confirmed. `backend-core` cannot start until **D2** and **D9** are confirmed. Everything else can proceed on the defaults.

---

## 1. Dependency order

```
WAVE 1   backend-core            (scanner / parsers / DB / jobs / search)
            │
WAVE 2   api-connectivity        (FastAPI v1 routers over backend-core services)
            │
WAVE 3   mcp                     (MCP server reusing the same services)
            │
WAVE 4   ui-design ──┬── ui-dev  (tokens+CSS  ──►  React components)
            │
WAVE 5   security  ──┬── qa      (audit; tests / lint / build / perf)
            │
WAVE 6   docs                    (verification pass, README, launchers)
```

**Parallelism that is safe from day 1** (does not wait for Wave 1):
* `ui-design` may author `frontend/src/styles/**` immediately once D10 is confirmed — it depends only on `ARCHITECTURE.md §7.1`.
* `qa` may author the API contract-conformance test skeletons from `API_CONTRACT.md` immediately (they will fail until Wave 2 lands — that is the point).
* `docs` may draft the README structure immediately.

**Hard gates:**
* Wave 2 starts when `backend-core` exposes the service-layer signatures in §2.1 (they are frozen at the start of Wave 1, so `api-connectivity` can code against stubs).
* Wave 4 starts when Wave 2's routers return contract-shaped JSON — `ui-dev` must never invent an endpoint (D8).
* Wave 5 starts when Waves 2–4 are feature-complete.

---

## 2. Agent 1 — `backend-core`

**Scope.** Everything below the HTTP layer: database, config, path safety, walking, parsing, architecture detection, node extraction, workflow analysis, output metadata, the three job services (hash / embed / thumbnail), and the search engine.

### Files owned (exclusive)
```
backend/app/config.py                         (rewrite: constants only, no COMFYUI_PATH)
backend/app/core/__init__.py
backend/app/core/db.py                        (connections, WAL, writer thread, bind guard)
backend/app/core/migrations/__init__.py
backend/app/core/migrations/m001_initial.py
backend/app/core/migrations/m002_import_legacy.py
backend/app/core/config_service.py            ← the ONLY reader of the ComfyUI path
backend/app/core/pathsafe.py
backend/app/core/errors.py                    (error codes + AppError hierarchy)
backend/app/core/progress.py                  (broadcast bus for SSE)
backend/app/core/fingerprint.py

backend/app/parsers/__init__.py
backend/app/parsers/safetensors_header.py
backend/app/parsers/gguf_header.py
backend/app/parsers/torch_zip.py              (pickletools-only; NEVER unpickle)
backend/app/parsers/arch_detect.py            (+ arch_rules.py data table)
backend/app/parsers/node_ast.py               (S1–S6)
backend/app/parsers/node_registry.py          (ComfyUI-Manager extension-node-map)
backend/app/parsers/graph_utils.py            ← the ONLY prompt-graph reader (B1)
backend/app/parsers/workflow_graph.py
backend/app/parsers/image_meta.py
backend/app/parsers/mp4_boxes.py
backend/app/parsers/extra_paths_yaml.py

backend/app/indexing/__init__.py
backend/app/indexing/service.py               (IndexerService, executors, phase machine)
backend/app/indexing/walker.py
backend/app/indexing/phases/{models,nodes,workflows,outputs,links,index,prune}.py

backend/app/jobs/__init__.py
backend/app/jobs/hash_service.py
backend/app/jobs/embed_service.py
backend/app/jobs/thumb_service.py

backend/app/search/__init__.py
backend/app/search/fts.py
backend/app/search/vec.py
backend/app/search/hybrid.py
backend/app/search/doc_builder.py

backend/app/services/civitai_service.py       (harden existing)
backend/app/services/ollama_service.py        (keep, minor hardening)
backend/app/services/file_ops.py              (rewrite internals, keep guard shape)
backend/app/services/queries/{models,nodes,workflows,outputs,albums,tags}_query.py
backend/requirements.txt
```

### Deleted by this agent
`backend/app/services/scanner.py`, `safetensors_parser.py`, `node_analyzer.py`, `workflow_parser.py`, `vector_search.py`, `mcp_server.py`, `backend/app/database.py`.

### Interfaces it MUST honour (frozen at wave start; `api-connectivity` codes against these)
```python
# core/config_service.py
def get_config() -> AppConfig
def set_config(patch: dict) -> AppConfig

# indexing/service.py
class IndexerService:
    def start(self, mode: str, phases: list[str] | None, root_ids: list[int] | None,
              force: bool, enrich_online: bool, trigger: str) -> int          # job_id
    def cancel(self, job_id: int | None = None) -> dict
    def status(self) -> dict
    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]              # (event, payload)

# jobs/hash_service.py
class HashService:
    def enqueue(self, scope: str, **kw) -> dict          # {batch_id, queued, bytes_total, eta_ms}
    def cancel(self, batch_id: str | None, uids: list[str] | None) -> dict
    def status(self) -> dict
    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]

# jobs/embed_service.py
class EmbedService:
    def status(self) -> dict
    async def enable(self, source: str = "auto") -> dict
    def disable(self, purge: bool = False) -> dict
    def rebuild(self, kinds: list[str] | None, force: bool) -> str
    def embed_query(self, text: str) -> np.ndarray | None

# jobs/thumb_service.py
class ThumbService:
    async def get(self, uid: str, size: int) -> ThumbResult   # .path .etag .source .mime
    def relocate(self, uid: str, old_path: str, new_path: str) -> None
    def gc(self, max_mb: int) -> dict

# search/hybrid.py
def search(q: str, *, smart: bool, kinds: list[str] | None,
           filters: dict, limit: int, offset: int) -> SearchResult

# services/queries/*_query.py  — one module per domain
def list_models(filters: ModelFilters, sort: str, group: str,
                limit: int, offset: int, conn: Connection) -> ListResult
def get_model(model_id: int, conn: Connection) -> dict | None
def model_facets(filters: ModelFilters, conn: Connection) -> dict
def model_usage(model_id: int, limit: int, offset: int, conn: Connection) -> dict
# …identical shape for nodes / workflows / outputs

# services/file_ops.py
def rename(uid: str, new_name: str, **kw) -> OpResult
def move(uids: list[str], target_root_id: int, target_folder: str, **kw) -> list[OpResult]
def delete(uids: list[str], mode: str, confirm: bool) -> list[OpResult]
def trash_list(...) / trash_restore(...) / trash_empty(...)
```

The `*_query` modules return **contract-shaped dicts** (API_CONTRACT §3–§6) so the routers are thin adapters. This is deliberate: it is what lets MCP and REST share one implementation with zero divergence.

### Acceptance criteria
1. **A full scan of `O:\ComfyUI` completes with `status='completed'` and non-zero rows in every table.** (Today: crashes, 0 rows everywhere.)
2. Scan of the real install: **≥ 231 models, ≥ 36 node packages, ≥ 1,500 node classes (of which ≥ 600 official), ≥ 48 workflows, ≥ 3,569 outputs.**
3. **Zero uncaught exceptions.** Every failure lands in `scan_errors`. Deliberately corrupt fixtures (an HTML file named `.safetensors`, a 0-byte file, a truncated PNG, a `.py` with a syntax error, a path > 260 chars, a filename with emoji + CJK) all produce exactly one error row and do not abort.
4. **B1:** `flux2-vae-new.safetensors` is indexed with `integrity='not_a_model'`. All 3,569 outputs index; no `ProgrammingError`; `provenance` populated; ≥ 90% correct positive/negative attribution on a 30-output fixture.
5. **B2:** `compute_autov2('<known file>')` equals the value from a reference `sha256sum` (specifically: the `flux2-vae-new` case that produced `E3B0C44298` must now produce `5628B30A8E`, or be excluded as `not_a_model` — either is acceptable, silently returning the empty-string hash is not). Hashing never runs during a scan.
6. **B3:** on a labelled fixture of ≥ 30 real files — `flux1-dev-fp8` → FLUX.1 **checkpoint** (not VAE) with `param_count_primary ≈ 11.9 B` (not 16.87 B); `acestep_v1.5_xl_base_bf16` → ACE-Step; `controlnet_tile_sdxl_1_0` → SDXL ControlNet; ≥ 90% of LoRAs resolve a non-Unknown base. Overall accuracy ≥ 92%.
7. **B4:** ≥ 30 of 32 custom packages yield ≥ 1 class (the two exceptions must be `ComfyUI-Manager` and `ComfyUI-Unreal`, both genuinely empty); total custom classes ≥ 900; official classes ≥ 600.
8. **B6:** `settings.COMFYUI_PATH` does not exist. `grep -rn "settings\.COMFYUI_PATH" backend/` returns nothing. Restart-then-scan uses the DB path.
9. **Incremental:** second consecutive scan with no filesystem changes reports `items_skipped == items_total` and completes in **≤ 1.5 s**.
10. **Cold full scan ≤ 25 s p95** on the real install; **API `GET /health` p95 stays < 50 ms while a scan runs** (proves non-blocking).
11. Search: FTS5 populated for every indexed row; lexical query p95 ≤ 25 ms at 10k synthetic rows; graceful `mode='lexical'` when the embedder is absent.
12. `ruff check backend/app` clean; no `bare except:`; no `subprocess` call on a scan hot path; no `import` of any file under `custom_nodes/`.

---

## 3. Agent 2 — `api-connectivity`

**Scope.** Every FastAPI router, request/response Pydantic models, the error envelope middleware, SSE endpoints, static serving, and app wiring. **No business logic** — routers call `backend-core` services only.

### Files owned
```
backend/app/main.py                     (rewrite: v1 prefix, middleware, lifespan, static)
backend/app/api/__init__.py
backend/app/api/deps.py                 (RO connection, CSRF header, pagination parser,
                                         sort/filter/group parsers)
backend/app/api/middleware.py           (error envelope, X-Request-Id, X-API-Version, timing)
backend/app/api/schemas/*.py            (Pydantic v2 request/response models, one per domain)
backend/app/api/v1/system_router.py
backend/app/api/v1/index_router.py      (+ SSE)
backend/app/api/v1/models_router.py
backend/app/api/v1/nodes_router.py
backend/app/api/v1/workflows_router.py
backend/app/api/v1/outputs_router.py
backend/app/api/v1/search_router.py
backend/app/api/v1/embeddings_router.py (+ SSE)
backend/app/api/v1/hash_router.py       (+ SSE)
backend/app/api/v1/files_router.py      (thumbnail / raw / download / reveal, Range support)
backend/app/api/v1/fileops_router.py
backend/app/api/v1/albums_router.py
backend/app/api/v1/tags_router.py
backend/app/api/v1/ai_router.py
```

### Deleted
`backend/app/api/{models,nodes,workflows,outputs,search,system,mcp}_api.py`.

### Must honour
* **`API_CONTRACT.md` verbatim.** Path, method, every parameter name/type/default, every response field name, every error code. No additions, no renames, no "while I was here" improvements.
* Error envelope on **every** non-2xx, including FastAPI's own 422 (install an exception handler for `RequestValidationError` that reshapes it into `field_errors`).
* `X-Vault-Request` enforced on all POST/PATCH/PUT/DELETE via a dependency, with an explicit allowlist for none.
* SSE endpoints yield from the service `subscribe()` async iterators; heartbeat every 15 s; `X-Accel-Buffering: no`.
* `/api/v1/files/raw` implements HTTP `Range` (206, `Accept-Ranges`, multi-range not required) — mandatory for `<video>`.
* CORS restricted to `localhost:3000`/`127.0.0.1:3000` in dev; disabled when serving the built SPA.
* Bind `127.0.0.1:8127`; refuse `0.0.0.0` unless `ALLOW_LAN=1`.
* Every route declares a `response_model` or explicit `responses={}` so `/openapi.json` is accurate.

### Acceptance criteria
1. Every endpoint in `API_CONTRACT.md` exists, responds, and matches its documented shape (verified by the `qa` contract suite).
2. No endpoint exists that is *not* in `API_CONTRACT.md`.
3. Missing `X-Vault-Request` on any mutating route → `400 CSRF_HEADER_MISSING`.
4. A forced internal exception returns the envelope with a `request_id` that appears verbatim in the server log — and no traceback in the body.
5. `GET /api/v1/index/stream` delivers `phase`/`progress`/`done` during a real scan and closes cleanly; two simultaneous subscribers both receive events.
6. `GET /api/v1/files/raw?uid=<mp4>` with `Range: bytes=0-1023` → `206`, `Content-Range` correct, 1024 bytes.
7. `GET /api/v1/files/thumbnail` returns WebP, correct `ETag`, and `304` on `If-None-Match`; a request for an undecodable source still returns `200` (placeholder).
8. `GET /api/v1/models?sort=bogus` → `422 VALIDATION_ERROR` with `field_errors[0].field == "sort"`.
9. `/openapi.json` validates; `/docs` renders.
10. p95 for `GET /api/v1/models?limit=100` ≤ 40 ms on the real dataset.

---

## 4. Agent 3 — `mcp`

**Scope.** A conformant MCP server on two transports, reusing `backend-core`'s query services.

### Files owned
```
backend/app/mcp/__init__.py
backend/app/mcp/protocol.py
backend/app/mcp/registry.py
backend/app/mcp/handlers.py
backend/app/mcp/resources.py
backend/app/mcp/prompts.py
backend/app/mcp/http.py            (router mounted by api-connectivity at /api/v1/mcp)
backend/app/mcp/stdio.py
backend/app/mcp_stdio.py           (module entrypoint)
```
`api-connectivity` includes `mcp.http.router` in `main.py` — the **only** cross-boundary touch, agreed in advance and limited to one `include_router` line.

### Must honour
* `MCP_SPEC.md` in full: handshake, capabilities, session header, notification 202 semantics, error-code discipline, all 13 tools with `inputSchema` + `outputSchema` + `annotations`, 5 resources + 4 templates, 4 prompts.
* Handlers call `services/queries/*` — **zero SQL in `backend/app/mcp/`.** A grep for `SELECT` under `app/mcp/` must return nothing.
* stdio writes nothing but JSON-RPC to stdout.
* Read-only DB connections; `vault_reindex` is the sole mutating tool and is disabled in read-only mode.
* Origin validation and loopback posture per `MCP_SPEC.md §9`.

### Acceptance criteria
1. All 14 conformance checks in `MCP_SPEC.md §10` pass.
2. A real MCP client (or the official inspector) completes `initialize` → `tools/list` → `tools/call` against **both** transports.
3. Parity: for identical filters, `list_models` / `list_node_classes` / `inspect_workflow` / `query_outputs` return data equal to their REST counterparts (field-by-field test).
4. `inspect_workflow` on a workflow with missing deps returns non-empty `install_hint` sourced from the ComfyUI-Manager registry.
5. Every `inputSchema`/`outputSchema` validates as JSON Schema draft-07 and sets `additionalProperties:false`.
6. Cold stdio start → first response < 800 ms.
7. Text blocks never exceed 4,000 chars; `structuredContent` never exceeds 512 KB.

---

## 5. Agent 4 — `ui-design`

**Scope.** The visual system only. No React logic.

### Files owned
```
frontend/src/styles/tokens.css        (ARCHITECTURE §7.1, verbatim)
frontend/src/styles/base.css          (reset, typography, scrollbars, focus, selection)
frontend/src/styles/layout.css        (app shell grid, rails, resizers, status bar)
frontend/src/styles/components.css    (buttons, chips, inputs, toggles, sliders, tables,
                                       cards, list rows, tree, badges, tooltips, toasts,
                                       modals, lightbox, progress bars, empty states, skeletons)
frontend/src/styles/utilities.css
frontend/src/assets/brand/*           (wordmark SVG, favicon, placeholder gradients)
docs/DESIGN_SYSTEM.md                 (component inventory + usage rules + do/don't)
```

### Must honour
* **D10** palette exactly: Studio Graphite surfaces + Signal Amber primary + Vault Violet secondary. **No cyan.** It must not read as a reskin of the reference.
* Layout geometry from the tokens (topbar 52, status 28, rail 264, details 340) and the reference *structure* (left tree / top toolbar / center grid / right details / bottom status).
* The five signature elements (ARCHITECTURE §7.1): amber spine, hairline separation, square-leaning radii, mono tabular metadata, amber=local/violet=inferred.
* Contrast: body text ≥ 7:1, all interactive ≥ 4.5:1. Visible focus ring on every focusable element.
* `prefers-reduced-motion` honoured.
* **Class names only — no inline styles, no CSS-in-JS.** BEM-ish flat naming: `.gp-card`, `.gp-card__title`, `.gp-card--selected`.
* Grid sizing driven by a single custom property `--gp-grid-size` set on the shell, so the slider is one variable write.
* Every component `ui-dev` needs must exist in `components.css` before Wave 4's second half.

### Acceptance criteria
1. `DESIGN_SYSTEM.md` documents every class with a purpose, states, and a do/don't.
2. A static HTML preview page renders every component in every state (default/hover/active/focus/disabled/loading/error).
3. Automated contrast check passes for every token pair used on text.
4. Zero `#hex` literals outside `tokens.css`.
5. No `!important`.
6. Total CSS ≤ 60 KB uncompressed.

---

## 6. Agent 5 — `ui-dev`

**Scope.** All React. Consumes `ui-design`'s classes and `api-connectivity`'s endpoints.

### Files owned
```
frontend/src/main.jsx
frontend/src/App.jsx
frontend/src/state/VaultContext.jsx        (useReducer store)
frontend/src/state/actions.js
frontend/src/hooks/useResource.js          (fetch + cache + abort + SWR)
frontend/src/hooks/useEventSource.js
frontend/src/hooks/useVirtualGrid.js       (hand-rolled windowing)
frontend/src/hooks/useDebounced.js
frontend/src/hooks/useResizablePanel.js
frontend/src/hooks/useKeyboardNav.js
frontend/src/services/api.js               (ONE module; every call goes through it)
frontend/src/services/format.js            (bytes, params, dates, durations)
frontend/src/components/shell/{AppShell,TopBar,LeftRail,StatusBar,DetailsPanel}.jsx
frontend/src/components/common/{Button,Chip,Toggle,Select,Slider,SearchInput,Tree,
                                 Badge,Tooltip,Toast,Modal,ProgressBar,EmptyState,Skeleton,
                                 ConfirmDialog,ErrorBoundary}.jsx
frontend/src/components/grid/{AssetGrid,AssetCard,AssetListRow,GroupHeader,GridToolbar}.jsx
frontend/src/components/tabs/{ModelsTab,NodesTab,WorkflowsTab,OutputsTab}.jsx
frontend/src/components/details/{ModelDetails,NodePackageDetails,NodeClassDetails,
                                  WorkflowDetails,OutputDetails,MetaRow,DependencyList,
                                  UsageList,HashStatus,ComponentBreakdown}.jsx
frontend/src/components/modals/{Lightbox,FileOpsModal,SettingsModal,FirstLaunchWizard,
                                 HealthDrawer,HashDialog,IndexProgress}.jsx
frontend/index.html
frontend/vite.config.js
frontend/package.json
```

### Must honour
* **Only endpoints in `API_CONTRACT.md`.** Any perceived gap is escalated to the architect, never worked around with a new route.
* `services/api.js` is the sole `fetch` site; it injects `X-Vault-Request` on mutations, normalizes the error envelope into a thrown `ApiError {code, message, fieldErrors, requestId}`, and supports `AbortSignal`.
* Grid is **virtualized**; ≤ 150 mounted cards at any zoom/page size.
* Thumbnails via `/files/thumbnail` with the §6 slider→size mapping, `loading="lazy"`, `decoding="async"`, and intrinsic `width`/`height` to prevent CLS.
* Lightbox uses `/files/raw` (with `<video controls>` for video, `<audio>` for audio, a download card for 3D).
* Search: 140 ms debounce, `AbortController` cancellation, Smart toggle bound to `smart=`; when the response says `smart_available:false` the toggle renders disabled with `smart_reason` as the tooltip — **never an error toast**.
* Scan/hash progress via `useEventSource`, with an automatic fallback to 2 s polling of `/status` if the stream errors twice.
* No new runtime dependencies (react, react-dom, lucide-react only).
* Full keyboard support: `/` focus search, `Esc` close, arrows navigate grid, `Enter` open, `Del` delete (with confirm), `Ctrl+A` select all, `F5`/`Ctrl+R` reindex.
* Every network-touching component wrapped in `ErrorBoundary`; every empty state has an explanatory `EmptyState` with a next action (e.g. "No models indexed — run a scan").

### Acceptance criteria
1. All four tabs render real data from the real install.
2. Left rail shows the album/folder tree with live counts; selecting a node filters the grid.
3. Top toolbar has search + Smart toggle + grouping dropdown + facet chips (with counts) + action buttons, all wired.
4. Grid supports list and grid modes; the size slider changes tile size smoothly and swaps thumbnail tiers.
5. Details panel shows deep detail per kind, including model component breakdown, hash state with a start button, "used in N workflows" with links, node class signatures, and workflow dependency lists with install hints.
6. Outputs: click → fullsize lightbox with prev/next, metadata sidebar, and copy-prompt.
7. Rename/move/delete work on all four asset types, with trash mode and an undo toast.
8. Status bar shows total count, selection count, and the per-page selector (50/100/200/500).
9. Wizard completes on a fresh DB and hands off to a live progress view.
10. **60 fps scroll** with 3,569 outputs at every slider size (verified in a profile trace).
11. `npm run build` succeeds; bundle ≤ 350 kB uncompressed; first paint ≤ 400 ms.
12. No console errors or React key warnings during a full click-through.

---

## 7. Agent 6 — `security`

**Scope.** Audit only. Owns no application source; produces a report plus test files.

### Files owned
```
docs/SECURITY_REVIEW.md
backend/tests/security/test_pathsafe.py
backend/tests/security/test_traversal.py
backend/tests/security/test_csrf.py
backend/tests/security/test_no_code_execution.py
backend/tests/security/test_mcp_posture.py
```
Findings are filed as issues against the owning agent; `security` does not patch other agents' files.

### Audit checklist
1. **Path traversal:** `..`, `%2e%2e`, UNC (`\\server\share`), `\\?\`, alternate data streams (`file.txt:hidden`), 8.3 short names, junctions/symlinks escaping a root, drive-letter switching, case-only differences, trailing dots/spaces, > 260 char paths.
2. **No client-supplied paths** on any endpoint — grep for `path` as a query/body param; only `uid` is acceptable (except `/system/validate-path`, which must be read-only and non-recursive beyond its cap).
3. **No code execution:** no `import` / `exec` / `eval` / `compile` / `pickle.load` / `torch.load` / `subprocess` anywhere in the parsing path. `torch_zip.py` must use only `zipfile` + `pickletools.genops`.
4. **YAML:** `yaml.safe_load` only; `yaml.load` without a loader is a fail.
5. **SQL injection:** no f-string/`%`/`.format()` into SQL. Sort/group/filter must map through allowlists, never interpolate user text (the current `models_api.list_models` interpolates `sort_column` — verify it is gone).
6. **CSRF:** `X-Vault-Request` enforced on every mutating route; a cross-origin simple request cannot mutate.
7. **Bind posture:** loopback only; `ALLOW_LAN` gated and loud; CORS not `*` in any build.
8. **MCP:** Origin validation, session handling, no destructive tools, no path input, no SSRF pivot, rate limit.
9. **Secrets:** `civitai_api_key` never returned by `/system/config`, never logged, never sent anywhere but `civitai.com`.
10. **Log hygiene:** no prompts, no filenames with PII, no tracebacks in responses, no API keys.
11. **Delete safety:** permanent delete requires `confirm:true`; trash is the default; no recursive directory delete is reachable from the API surface.
12. **DoS:** header-size cap (100 MB) on safetensors, JSON size caps, AST walk depth/file-count caps, thumbnail dimension cap, `limit` max enforced, SSE subscriber cap.
13. **Zip/archive:** `torch_zip` must not follow paths out of the archive and must cap entry count and read size.
14. **Dependency review:** justify each of `PyYAML`, `onnxruntime`, `tokenizers`; check for known CVEs at the pinned floor.

### Acceptance criteria
`SECURITY_REVIEW.md` with a finding table (severity, location, reproduction, recommendation, owning agent, status) and **zero unresolved High or Critical** at sign-off. All security tests pass.

---

## 8. Agent 7 — `qa`

### Files owned
```
backend/tests/conftest.py
backend/tests/fixtures/**                 (synthetic safetensors, corrupt files, prompt graphs,
                                           mini custom_nodes tree covering all 6 strategies)
backend/tests/unit/test_graph_utils.py         ← B1
backend/tests/unit/test_arch_detect.py         ← B3
backend/tests/unit/test_node_ast.py            ← B4
backend/tests/unit/test_safetensors_header.py
backend/tests/unit/test_torch_zip.py
backend/tests/unit/test_pathsafe.py
backend/tests/unit/test_fingerprint.py
backend/tests/unit/test_config_service.py      ← B6
backend/tests/unit/test_hash_service.py        ← B2
backend/tests/unit/test_search.py
backend/tests/integration/test_index_pipeline.py
backend/tests/integration/test_incremental.py
backend/tests/integration/test_error_isolation.py
backend/tests/contract/test_api_contract.py    (one test per endpoint in API_CONTRACT.md)
backend/tests/contract/test_error_envelope.py
backend/tests/contract/test_mcp_conformance.py
backend/tests/perf/test_budgets.py
backend/tests/live/test_real_install.py        (marked `live`, opt-in, targets O:\ComfyUI)
frontend/src/__tests__/**                      (if a runner is added; otherwise a documented
                                                manual click-through checklist)
pyproject.toml / ruff.toml / pytest.ini
docs/QA_REPORT.md
```

### Must verify (regression gates for every audited defect)
| Gate | Test |
|---|---|
| **B1** | 200-graph fixture with link-valued `text`; scan completes, 0 exceptions, rows persisted, provenance recorded |
| **B2** | AutoV2 equals reference SHA-256 prefix; hashing never runs inside `IndexerService`; queue resumes across a simulated restart |
| **B3** | Labelled fixture ≥ 30 files, ≥ 92% accuracy; `flux1-dev-fp8` is a checkpoint not a VAE; param count is the primary component |
| **B4** | Mini `custom_nodes` tree exercising S1–S6; ≥ 30/32 real packages non-zero; ≥ 600 official classes |
| **B5** | `start_app.bat` starts the backend and `/api/v1/ping` answers within 15 s |
| **B6** | Set path → restart process → path persists; file ops and thumbnails work post-restart |
| **Perf** | Cold ≤ 25 s, warm ≤ 1.5 s, lexical search ≤ 25 ms, hybrid ≤ 70 ms, `/ping` p95 < 50 ms during a scan |
| **Isolation** | Inject a failure into 1 of 500 items → 499 committed, 1 `scan_errors` row, job `completed` |
| **Contract** | Every documented endpoint/param/field present; no undocumented endpoint exists |
| **Build** | `npm run build` passes; `ruff check` clean; `pytest` green |

### Acceptance criteria
`pytest backend/tests` green (excluding `live`); `pytest -m live` green on the real install; `ruff check backend` clean; `npm run build` clean; `QA_REPORT.md` records measured numbers against every budget in ARCHITECTURE §10.

---

## 9. Agent 8 — `docs`

### Files owned
```
README.md
docs/INSTALL.md
docs/USER_GUIDE.md
docs/TROUBLESHOOTING.md
docs/MCP_CLIENT_SETUP.md
docs/CHANGELOG.md
start_app.bat                 ← B5 fix
start_app.ps1
install_dependencies.bat
install_dependencies.ps1
stop_app.bat
```

### Must deliver
* `start_app.bat` using **`--app-dir backend`** (not `--cwd`), port 8127, venv activation, a wait-for-listening loop, and a browser launch. It must fail loudly with a readable message if the venv or `node_modules` are missing.
* `install_dependencies` scripts installing `backend/requirements.txt` + `npm ci`, verifying Python ≥ 3.12 and Node ≥ 18, and printing a clear summary.
* README: what it is, the Geekatplay branding, a screenshot, quick start, the four tabs, Smart search (including the one-time 23 MB download and the offline manual-placement path), hashing expectations (**1.5 TB ≈ 2.8 h**), MCP setup, and an explicit "what this app never does" (never modifies your models without an explicit action; never uploads anything; never imports custom-node code).
* `TROUBLESHOOTING.md` covering: backend won't start, port in use, wrong ComfyUI path, `extra_model_paths.yaml` not found, models show "Unknown" base model, Civitai shows nothing (needs hashing), Smart search unavailable, thumbnails missing, `FILE_LOCKED` while ComfyUI is running, long-path errors, and how to read `/system/health`.
* `MCP_CLIENT_SETUP.md` with the copy-pasteable stdio config and the HTTP endpoint.
* **Verification pass:** follow the docs literally on a clean checkout and confirm every step works. Any discrepancy is a bug filed against the owning agent, not a doc edit.

### Acceptance criteria
A fresh clone → run `install_dependencies.bat` → run `start_app.bat` → complete the wizard → see indexed data, all following only the written instructions. Every command in every doc is executed and confirmed. No stale references to `/api/` v0 endpoints, port 8000, `asset_vault.db`, or `--cwd`.

---

## 10. Cross-agent rules

1. **Contract documents are frozen.** `API_CONTRACT.md`, `MCP_SPEC.md`, and `DATA_MODEL.md` change only via the architect. An agent that needs a change files a request; it does not edit and proceed.
2. **No file is owned by two agents.** The single exception is `main.py`'s one-line `include_router(mcp.http.router)`, pre-agreed here.
3. **No agent adds a dependency** not listed in ARCHITECTURE §11 without architect sign-off.
4. **No agent introduces TypeScript, an ORM, Electron, or a cloud call.**
5. Every agent leaves its area passing `ruff` (Python) / building (JS) before handoff.
6. Any newly discovered defect in the real install is appended to `docs/AUDIT.md` as an addendum by `qa` — the original audit is not edited.
