# Module Boundaries — Geekatplay ComfyUI Asset Vault

Where each part of the system lives, the interfaces between them, and the constraints that keep
them from drifting apart. Section numbers below are cited by their number elsewhere (source
comments, `DESIGN_SYSTEM.md`, `SECURITY_REVIEW.md`) — keep the numbering stable even if a section's
content changes.

Product-level decisions (ports, storage format, palette, MCP access model) live in
[DECISIONS.md](DECISIONS.md); this file covers module boundaries and engineering constraints only.

---

## 2. Backend core

Everything below the HTTP layer: database, config, path safety, walking, parsing, architecture
detection, node extraction, workflow analysis, output metadata, the job services (hash / embed /
thumbnail), and the search engine — see `backend/app/{core,parsers,indexing,jobs,search,services}/`.

### Interfaces the API layer and MCP server both depend on
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

The `*_query` modules return **contract-shaped dicts** (`API_CONTRACT.md` §3–§6) so the routers are
thin adapters — this is what lets the REST API and the MCP server share one implementation with
zero divergence.

---

## 3. API layer

Every FastAPI router, request/response Pydantic models, the error envelope middleware, SSE
endpoints, and static serving — `backend/app/api/` and `backend/app/main.py`. Routers call the
backend core services from §2 above; no business logic lives here.

### Constraints
* **`API_CONTRACT.md` is authoritative.** Path, method, every parameter name/type/default, every
  response field name, every error code — no undocumented endpoint, and no documented one missing.
* Error envelope on **every** non-2xx, including FastAPI's own 422.
* `X-Vault-Request` enforced on all POST/PATCH/PUT/DELETE via a dependency.
* SSE endpoints yield from a service's `subscribe()` async iterator; heartbeat every 15 s;
  `X-Accel-Buffering: no`.
* `/api/v1/files/raw` implements HTTP `Range` (206, `Accept-Ranges`) — needed for `<video>`.
* CORS restricted to the dev origin; disabled when serving the built SPA.
* Binds `127.0.0.1:8127`; refuses `0.0.0.0` unless `ALLOW_LAN=1`.
* Every route declares a `response_model` or explicit `responses={}` so `/openapi.json` stays
  accurate.

---

## 4. MCP server

A conformant MCP server on two transports (`backend/app/mcp/`), reusing the same query services as
the REST API — see `MCP_SPEC.md` for the protocol conformance detail and `DECISIONS.md` §C5 for the
access-model decision.

Mounted by `app/main.py` with a single `include_router` line — that is the only cross-boundary
touch between the API layer and the MCP module. Handlers call `services/queries/*`: **zero SQL
lives under `backend/app/mcp/`** (a `grep SELECT` there returns nothing). stdio writes nothing but
JSON-RPC to stdout. Origin validation and loopback posture per `MCP_SPEC.md` §9.

---

## 5. Design tokens & CSS

The visual system — `frontend/src/styles/{tokens,base,layout,components,utilities}.css` and
`frontend/src/assets/brand/`. Full component inventory, usage rules and states are in
[DESIGN_SYSTEM.md](DESIGN_SYSTEM.md), whose own checklist is what this section's constraints feed.

* Palette per `DECISIONS.md` §C4 (Studio Graphite + Signal Amber + Vault Violet). No cyan.
* Layout geometry and the five signature elements are in `ARCHITECTURE.md` §7.1.
* Contrast: body text ≥ 7:1, all interactive ≥ 4.5:1; a visible focus ring on every focusable
  element; `prefers-reduced-motion` honoured.
* Class names only — no inline styles, no CSS-in-JS.
* Grid sizing driven by one custom property (`--gp-grid-size`) set on the shell.
* Zero `#hex` literals outside `tokens.css`; no `!important`.

---

## 6. Frontend

All React — `frontend/src/`. Consumes the tokens above and the endpoints in `API_CONTRACT.md`.

* `services/api.js` is the sole `fetch` site; it injects `X-Vault-Request` on mutations,
  normalises the error envelope into a thrown `ApiError {code, message, fieldErrors, requestId}`,
  and supports `AbortSignal`.
* The asset grid is virtualised — a bounded number of mounted cards at any zoom/page size.
* Thumbnails via `/files/thumbnail` with `loading="lazy"`, `decoding="async"`, and intrinsic
  `width`/`height` to prevent layout shift.
* Search: debounced, `AbortController` cancellation, the Smart toggle bound to `smart=`; when the
  response says `smart_available:false` the toggle renders disabled with `smart_reason` as the
  tooltip — never an error toast.
* Scan/hash progress via SSE with a polling fallback if the stream errors.
* Every network-touching component is wrapped in an error boundary; every empty state has an
  explanatory message with a next action.
* Full keyboard support: `/` focus search, `Esc` close, arrows navigate the grid, `Enter` open,
  `Del` delete (with confirmation), `Ctrl+A` select all.

---

## 7. Security requirements checklist

The enumerated requirements `SECURITY_REVIEW.md`'s finding table verifies against, by number
(`§7.1`, `§7.12`, etc.) — keep the numbering stable; it is cited from source comments too.

1. **Path traversal:** `..`, `%2e%2e`, UNC (`\\server\share`), `\\?\`, alternate data streams
   (`file.txt:hidden`), 8.3 short names, junctions/symlinks escaping a root, drive-letter
   switching, case-only differences, trailing dots/spaces, > 260 char paths.
2. **No client-supplied paths** on any endpoint — only `uid` is acceptable (except
   `/system/validate-path`, which is read-only and non-recursive beyond its cap).
3. **No code execution:** no `import` / `exec` / `eval` / `compile` / `pickle.load` /
   `torch.load` / `subprocess` anywhere in the parsing path. `torch_zip.py` uses only `zipfile` +
   `pickletools.genops`.
4. **YAML:** `yaml.safe_load` only.
5. **SQL injection:** no f-string/`%`/`.format()` into SQL. Sort/group/filter map through
   allowlists, never interpolate user text.
6. **CSRF:** `X-Vault-Request` enforced on every mutating route; a cross-origin simple request
   cannot mutate.
7. **Bind posture:** loopback only; `ALLOW_LAN` gated and loud; CORS not `*` in any build.
8. **MCP:** Origin validation, session handling, no path input, no SSRF pivot, rate limiting.
9. **Secrets:** `civitai_api_key` never returned by `/system/config`, never logged, never sent
   anywhere but `civitai.com`.
10. **Log hygiene:** no prompts, no filenames with PII, no tracebacks in responses, no API keys.
11. **Delete safety:** permanent delete requires `confirm:true`; trash is the default; no
    recursive directory delete is reachable from the API surface.
12. **DoS:** header-size cap (100 MB) on safetensors, JSON size caps, AST walk depth/file-count
    caps, thumbnail dimension cap, `limit` max enforced, SSE subscriber cap.
13. **Zip/archive:** `torch_zip` never follows a path out of the archive and caps entry count and
    read size.
14. **Dependency review:** each runtime dependency is justified; checked for known CVEs at the
    pinned floor.

Zero unresolved High or Critical findings is the standing bar — see `SECURITY_REVIEW.md` for
current status.

---

## 10. Cross-cutting rules

1. **The contract documents stay in sync with the code.** `API_CONTRACT.md`, `MCP_SPEC.md`, and
   `DATA_MODEL.md` change in the same commit as the code they describe — never left to drift.
2. **No new runtime dependency** without a stated reason and a CVE check at the pinned floor.
3. **No TypeScript, no ORM, no Electron, no cloud call.** Stack fixed: Python + FastAPI + SQLite +
   React + Vite.
