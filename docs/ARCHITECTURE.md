# Architecture — Geekatplay ComfyUI Asset Vault & Manager
**v2.0 rebuild** · Author: Geekatplay Studio — Vladimir Chopine · Target install: `O:\ComfyUI` (ComfyUI 0.33.0)
Status: **FROZEN CONTRACT.** The code implements against this document; a change to one is a
change to both, in the same commit.

---

## 0. Ground rules

* Stack is fixed: **Python 3.12 + FastAPI + SQLite (stdlib `sqlite3`) + React 18 + Vite 4 (JSX, no TypeScript)**.
* No ORM, no Electron, no cloud dependency, no TypeScript.
* **Windows-first.** Every path operation assumes NTFS, case-insensitive compares, `\\?\` long paths, and UTF-16 filenames.
* **Local-first.** Civitai and Ollama are optional enrichment. The app is 100% functional offline.
* Evolve, don't rewrite: see §12 for the keep/rewrite ledger.

---

## 1. Component map

```
┌─────────────────────────────── Browser (React 18 / Vite) ───────────────────────────────┐
│  AppShell ─ TopBar(search+Smart+group+facets+actions)                                   │
│           ─ LeftRail(album/group tree + counts)                                         │
│           ─ AssetGrid (virtualized, list|grid, size slider)                             │
│           ─ DetailsPanel (right)                                                        │
│           ─ StatusBar (count + per-page)                                                │
│  state: useVaultStore (plain useReducer + context; no Redux)                            │
│  transport: fetch + EventSource(SSE)                                                    │
└──────────────────────────────────────┬──────────────────────────────────────────────────┘
                                       │ HTTP /api/v1  (Vite proxy → 127.0.0.1:8127)
┌──────────────────────────────────────┴──────────────────────────────────────────────────┐
│                        FastAPI process (single, uvicorn, 127.0.0.1 only)                 │
│                                                                                          │
│  api/          v1 routers: system, index, models, nodes, workflows, outputs,             │
│                search, files, fileops, hash, embeddings, mcp                             │
│                                                                                          │
│  core/         config_service   ← THE ONLY reader of the ComfyUI path                    │
│                pathsafe         ← containment checks, long-path normalization            │
│                db               ← connection pools (RO readers + 1 writer thread)        │
│                errors           ← versioned error envelope                               │
│                                                                                          │
│  indexing/     IndexerService (owns ThreadPoolExecutors, phase machine, progress bus)    │
│                walker · fingerprint · phases/{models,nodes,workflows,outputs,links}      │
│                                                                                          │
│  parsers/      safetensors_header · gguf_header · torch_zip (pickle-free) ·              │
│                arch_detect · node_ast · workflow_graph · graph_utils(B1) · image_meta    │
│                                                                                          │
│  jobs/         HashService (resumable SHA-256 queue)                                     │
│                EmbedService (ONNX MiniLM, incremental)                                   │
│                ThumbService (lazy WebP cache)                                            │
│                CivitaiService · OllamaService (optional, circuit-broken)                 │
│                                                                                          │
│  search/       fts (SQLite FTS5) + vec (numpy matrix) + hybrid (RRF fusion)              │
│                                                                                          │
│  mcp/          server (tool registry) · http transport · stdio transport                 │
└──────────────────────────────────────┬──────────────────────────────────────────────────┘
                                       │
        backend/data/vault.db (WAL) · backend/data/thumbs/ · backend/data/models/ ·
        backend/data/.vault-trash/ (per-root trash lives beside the root, see §9.3)
```

---

## 2. Process model — **in-process, thread-pool based**

**Decision: the scanner runs inside the FastAPI process on dedicated `ThreadPoolExecutor`s owned by the app lifespan. No separate worker process.**

**Justification (3 lines):** the audit measured the entire workload as I/O-bound — 0.23 s to parse 231 safetensors headers, 0.21 s to walk 3,569 outputs, 1.65 s to PIL-open 400 images — and every one of those calls releases the GIL, so threads scale nearly linearly while the GIL never becomes the bottleneck. A second process would require IPC, a second SQLite writer (WAL cross-process contention), lifecycle supervision in `start_app.bat`, and orphan cleanup on Windows — real cost for zero measured gain. The single-process design also lets `IndexerService` hold the embedding matrix and thumbnail in-flight map in shared memory with no serialization.

### 2.1 Executors

| Executor | Threads | Owns |
|---|---:|---|
| `EX_IO` | `min(8, cpu*2)` | header reads, `os.stat`, JSON loads |
| `EX_IMG` | `min(6, cpu)` | PIL decode/resize (thumbnails + output dimensions) |
| `EX_AST` | `min(4, cpu)` | Python AST parsing of node packages |
| `EX_HASH` | **2** (configurable 1–4) | full-file SHA-256 |
| `EX_EMBED` | **1** | ONNX inference (ORT has its own intra-op pool) |
| `T_WRITER` | **1 dedicated thread** | the *only* thread that writes to SQLite |

`EX_HASH` defaults to 2, not 8: the model store is 1.5 TB on a single volume (`O:`), and parallel large sequential reads on one spindle/array thrash. 2 gives read-ahead overlap without seek storms. Exposed as `hash_concurrency` in config.

### 2.2 SQLite concurrency model

```
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store   = MEMORY;
PRAGMA mmap_size    = 268435456;   -- 256 MB
PRAGMA cache_size   = -65536;      -- 64 MB
```

* **Readers:** every request handler gets a thread-local read-only connection (`file:...?mode=ro`, `uri=True`). WAL means readers never block on the writer. This is what guarantees "a scan never blocks the API."
* **Writer:** one `WriteQueue` (a `queue.Queue` of callables) drained by `T_WRITER`. All mutations — indexer, file ops, hash results, embeddings, user edits — are submitted as units of work. Serialized writes eliminate `SQLITE_BUSY` entirely and make the commit strategy in §3.4 possible.
* Never pass a connection across threads. Never `check_same_thread=False`.

### 2.3 Progress streaming — **SSE**

**Decision: Server-Sent Events at `GET /api/v1/index/stream` (and `/api/v1/hash/stream`), with `GET .../status` as a poll fallback.**

**Justification:** progress is strictly server→client, so a bidirectional WebSocket buys nothing while costing a second protocol, a reconnect state machine, and extra Vite proxy configuration; SSE is plain HTTP/1.1, passes the existing `/api` proxy untouched, and `EventSource` reconnects automatically with `Last-Event-ID`. Polling alone was rejected because a 25-second scan with per-file granularity would need ~4 Hz polling of a JSON status doc, which is wasteful and janky.

Event contract (`event:` name + JSON `data:`):

```
event: phase     data: {"job_id":17,"phase":"models","index":3,"of":8,"label":"Models"}
event: progress  data: {"job_id":17,"phase":"models","done":118,"total":231,"rate":512.4,"eta_ms":220,"current":"loras\\vovka.safetensors"}
event: item      data: {"job_id":17,"kind":"model","op":"upsert","uid":"model:41","name":"vovka.safetensors"}
event: error     data: {"job_id":17,"kind":"model","path":"...","code":"HEADER_INVALID","message":"..."}
event: done      data: {"job_id":17,"status":"completed","stats":{...},"duration_ms":18420,"errors":3}
event: heartbeat data: {"t":1766000000}
```

* `progress` is **coalesced to ≤10 Hz per phase** in the progress bus — never one event per file.
* `heartbeat` every 15 s keeps Windows proxies/antivirus from closing an idle stream.
* Response headers: `Cache-Control: no-store`, `X-Accel-Buffering: no`, `Connection: keep-alive`.
* Multiple concurrent subscribers are supported (each gets its own `asyncio.Queue` fed by a broadcast bus). If a subscriber's queue exceeds 1000 items it is dropped with `event: overflow`.

---

## 3. Indexing pipeline

### 3.1 Design goals restated as invariants

1. **Incremental** — a file whose `(size, mtime_ns)` matches the stored fingerprint is never re-parsed.
2. **Resumable** — a scan killed mid-way leaves all completed work committed; the next scan continues.
3. **Parallel** — every phase fans out over an executor.
4. **Non-blocking** — the API stays responsive throughout (guaranteed by WAL + reader pool, §2.2).
5. **Per-item isolated** — one malformed file produces one `scan_errors` row and nothing else.

### 3.2 Fingerprint

```
fingerprint = blake2b(
    normcase(abs_path).encode('utf-16-le') + b'\x1f' +
    str(size).encode()                     + b'\x1f' +
    str(mtime_ns).encode(),
    digest_size=16
).hexdigest()
```

`blake2b` is stdlib and ~3× faster than sha256 for tiny inputs. `mtime_ns` (not `st_mtime` float) because NTFS has 100 ns resolution and float seconds collide on fast writes. `normcase` because NTFS is case-insensitive — `Loras\X.safetensors` and `loras\x.safetensors` are the same file and must not double-index.

A row is **fresh** iff `stored.fingerprint == computed.fingerprint` **and** `stored.parser_version == PARSER_VERSION` for its kind. Bumping `PARSER_VERSION` (a per-parser integer constant) forces a targeted re-parse without a user-visible "force" flag — this is how parser bug fixes roll out.

### 3.3 Phases

| # | Phase | Executor | Concurrency | Notes |
|---:|---|---|---:|---|
| 0 | `roots` | main | 1 | Resolve ComfyUI root + `extra_model_paths.yaml` roots. Emits `roots` table. |
| 1 | `walk` | `EX_IO` | 1 per root dir | `os.scandir` recursive, iterative (no recursion limit), yields `(path,size,mtime_ns)` from the `DirEntry` cache — no extra `stat` syscall. |
| 2 | `diff` | main | 1 | Set-difference against `asset_files`-equivalent columns. Produces `to_parse`, `to_touch`, `to_prune`. |
| 3 | `models` | `EX_IO` | 8 | Header parse + arch detect. |
| 4 | `nodes` | `EX_AST` | 4 | Official core + custom packages. |
| 5 | `workflows` | `EX_IO` | 4 | JSON + embedded-graph media. |
| 6 | `outputs` | `EX_IMG` | 6 | Dimensions + embedded generation metadata. |
| 7 | `links` | `T_WRITER` | 1 | Resolve `workflow_dependencies`, `output_models`, node-class → package. Pure SQL. |
| 8 | `index` | `T_WRITER` + `EX_EMBED` | 1 + 1 | FTS5 upserts (sync) + embedding enqueue (async). |
| 9 | `prune` | `T_WRITER` | 1 | Soft-delete rows whose files vanished (`missing_since`), hard-delete after 30 days. |

Phase 9 uses **soft delete**: a disconnected network drive or a running ComfyUI mid-write must not wipe the vault. `missing_since` is set on first miss and cleared if the file returns. Rows with `missing_since` are hidden from list endpoints by default but exposed via `?include_missing=true`, and shown in the UI as a "Missing" facet.

### 3.4 Commit strategy — batched, per-item isolated

The current design's single terminal `conn.commit()` is the root cause of B1's total data loss. Replacement:

```python
BATCH = 256                 # rows per transaction
for chunk in chunks(work, BATCH):
    conn.execute("BEGIN IMMEDIATE")
    for item in chunk:
        sp = f"sp_{item.seq}"
        conn.execute(f"SAVEPOINT {sp}")
        try:
            upsert(conn, item)
            conn.execute(f"RELEASE {sp}")
        except Exception as e:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            record_scan_error(conn, job_id, item, e)   # same txn, always succeeds
    conn.commit()
    bus.publish("progress", ...)
```

* **Every item is wrapped in a `SAVEPOINT`.** A bad row rolls back only itself; the other 255 in the batch commit.
* `record_scan_error` binds only `TEXT`/`INTEGER` and truncates the message to 2000 chars, so the error path cannot itself fail on a binding error.
* `BEGIN IMMEDIATE` (not deferred) takes the write lock up front, avoiding mid-transaction upgrade failures.
* Batch = 256 balances fsync amortization against worst-case loss on hard kill (256 items ≈ 0.3 s of work).

### 3.5 Universal binding guard

Every value crossing into `sqlite3.execute` passes through:

```python
def bind(v, *, kind: str = "text"):
    """Coerce any Python value into a SQLite-bindable scalar. Never raises."""
```

`list`/`dict` → `json.dumps(..., ensure_ascii=False, default=str)` for JSON columns, or `None` for scalar columns. `bool` → `int`. `Path` → `str`. `float('nan'/'inf')` → `None`. Unknown objects → `str(v)[:2000]`. **This is the last line of defence behind §4.1's semantic fix, and it makes B1's crash class structurally impossible.**

### 3.6 Error isolation and reporting

* `scan_errors(job_id, phase, kind, path, code, message, traceback_head)` — one row per failure.
* Stable `code` vocabulary: `HEADER_INVALID`, `HEADER_TOO_LARGE`, `NOT_A_MODEL`, `JSON_INVALID`, `IMAGE_UNREADABLE`, `PERMISSION_DENIED`, `FILE_LOCKED`, `PATH_TOO_LONG`, `AST_SYNTAX_ERROR`, `ENCODING_ERROR`, `UNKNOWN`.
* A scan **never** fails because of item errors. `scan_jobs.status` becomes `completed` (with `error_count > 0`), `cancelled`, or `failed` (only for root-level failures: root missing, DB unwritable, disk full).
* `GET /api/v1/index/errors` surfaces them; the UI shows a "3 issues" chip in the status bar linking to a Health drawer.

### 3.7 Cancellation & resumability

* `IndexerService.cancel(job_id)` sets a `threading.Event`. Every worker checks it between items; every phase checks it between batches. Cancellation is observed within one batch (~0.3 s).
* Committed batches survive. `scan_jobs.phase_cursor` (JSON) records the last completed phase and the offset within it. A restart after a crash finds `status='running'` with a stale `heartbeat_at`, marks it `interrupted`, and the next scan resumes from `phase_cursor` — but because phase 2 (`diff`) is fingerprint-driven and idempotent, resumption is naturally correct even without the cursor. The cursor is a fast path, not a correctness requirement.

### 3.8 Auto-reindex

* **On startup:** if `auto_reindex` is on and the vault is non-empty, run an incremental scan 2 s after the server is listening (never blocking lifespan startup).
* **On demand:** `POST /api/v1/index/start`.
* **Watch mode (opt-in, default OFF):** a debounced directory-mtime poller (30 s interval) comparing top-level directory `mtime_ns` per watched folder. **Decision: polling, not `ReadDirectoryChangesW`** — the poll costs ~5 ms across all roots (measured walk of the whole tree is 0.21 s; a shallow dir-mtime check is far less), needs zero dependencies, and cannot leak OS handles on a network drive. `watchdog` is explicitly rejected as an unnecessary dependency.

---

## 4. Correctness fixes owned by this design

### 4.1 B1 — ComfyUI prompt-graph value coercion

**Single canonical module: `backend/app/parsers/graph_utils.py`.** No other module may reach into a prompt graph.

ComfyUI's API-format `prompt` is `{ "<node_id>": {"class_type": str, "inputs": {name: value}} }` where `value` is either a scalar **or a link** `[<source_node_id>, <output_slot>]`. Measured on the real install: **3005 link-valued inputs vs 2024 scalar-valued** in a 200-image sample — links are the majority. Node ids are **strings and may be subgraph-qualified** (`'88:97'`), so all lookups are by `str(key)`.

```python
LINK_RE_KINDS = (str, int, float, bool, type(None))

@dataclass(frozen=True)
class Resolved:
    value: Any | None          # scalar, or None if unresolvable
    origin: str                # "literal" | "link" | "widget" | "unresolved"
    source_node_id: str | None
    source_class_type: str | None
    depth: int

def is_link(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and isinstance(v[0], (str, int))
            and isinstance(v[1], int))

def resolve_input(graph: dict, node_id: str, input_name: str, *,
                  max_depth: int = 12,
                  _seen: frozenset = frozenset()) -> Resolved: ...
```

Resolution algorithm:

1. Fetch `graph[str(node_id)]["inputs"][input_name]`. Missing → `Resolved(None, "unresolved", …)`.
2. If it is a scalar (`str|int|float|bool|None`) → `Resolved(v, "literal", …)`.
3. If `is_link(v)`:
   a. Guard `node_id in _seen` (cycle) or `depth >= max_depth` → `unresolved`.
   b. Look up `src = graph[str(v[0])]`. Missing → `unresolved`.
   c. If `src["class_type"]` is in **`VALUE_PRODUCER_MAP`**, recurse into that node's own designated value input. The map is data, not code:
      ```python
      VALUE_PRODUCER_MAP = {
        "PrimitiveNode": ("value",), "PrimitiveString": ("value",),
        "PrimitiveStringMultiline": ("value",), "PrimitiveInt": ("value",),
        "PrimitiveFloat": ("value",), "PrimitiveBoolean": ("value",),
        "String": ("string","value"), "StringConstant": ("string","value"),
        "StringConstantMultiline": ("string","value"),
        "Text": ("text",), "CR Text": ("text",), "ttN text": ("text",),
        "easy string": ("value",), "JoinStrings": ("string1","string2"),
        "ImpactWildcardProcessor": ("populated_text","wildcard_text"),
        "CLIPTextEncode": ("text",), "CLIPTextEncodeSDXL": ("text_g","text_l"),
        "CLIPTextEncodeFlux": ("clip_l","t5xxl"),
        "Reroute": ("*",), "Reroute (rgthree)": ("*",),
        "GetNode": ("value",), "SetNode": ("value",),
      }
      ```
      `("*",)` means "follow the single input whatever it is named" (Reroute).
   d. Otherwise → `Resolved(None, "link", source_node_id=str(v[0]), source_class_type=src["class_type"], …)`. The **caller decides** what to store; the DB gets `NULL` for the scalar plus a `provenance` JSON note.
4. If it is any other type (`dict`, nested list, …) → `unresolved`.

Convenience wrappers used by every consumer:

```python
def as_text(r: Resolved, max_len=8000) -> str | None
def as_int(r: Resolved)   -> int | None
def as_float(r: Resolved) -> float | None
def as_model_name(r: Resolved) -> str | None      # strips dirs, validates extension
```

Higher-level extractor, also in this module — the *only* thing `outputs`/`workflows` phases call:

```python
def summarize_graph(prompt: dict | None, workflow: dict | None) -> GraphSummary
# GraphSummary(positive_prompt, negative_prompt, seed, steps, cfg, sampler,
#              scheduler, denoise, width, height, models[], loras[], node_types[],
#              unresolved_count, provenance: dict)
```

Positive vs negative is determined **structurally, not by ordering**: walk the sampler node (`KSampler*`, `SamplerCustom*`, `*Sampler*`), follow its `positive` and `negative` link inputs to their conditioning sources, and resolve the text from there. The current code's "first text seen is positive" heuristic is wrong on ~every multi-encoder graph. Falls back to document order only when no sampler node exists.

**Acceptance gate:** a fixture set of 30 real outputs (including ≥10 with link-valued `text`) must index with 0 exceptions and ≥90% correct positive/negative attribution.

### 4.2 B2 — Hashing strategy (USER-DECIDED: background, opt-in, resumable)

**Never blocks a scan. A model card is fully usable with `hash_state='unhashed'`.**

* **Algorithm:** full-file **SHA-256**, streamed in 8 MiB chunks. `autov2 = sha256_hex[:10].upper()`. The current 64 KB-after-header implementation is deleted outright — it produced `E3B0C44298` (SHA-256 of the empty string) on a 129 KB file because the seek landed past EOF.
* **Cache key:** `(abs_path_normcase, size, mtime_ns)` stored on `model_files`. If the fingerprint changes, `hash_state` resets to `unhashed` and any cached Civitai enrichment is marked `stale` (kept visible, badged) rather than deleted.
* **Content-addressed reuse:** when a file is renamed or moved, the fingerprint changes but the content does not. Before hashing, check for another `model_files` row with the same `(size, sha256 IS NOT NULL)` and a matching first-1 MiB + last-1 MiB probe hash (`probe_sha256`, cheap, always computed during the scan phase). On probe match, copy the full hash without re-reading 12 GB. This makes rename/move effectively free.
* **Queue:** `hash_jobs(model_file_id, priority, state, attempts, bytes_done, …)` — a real table, so the queue survives restart. States: `queued → running → done | failed | cancelled`.
* **Scoping:** the user enqueues by scope — `all`, `category:loras`, `folder:<path>`, `ids:[…]`, or `unhashed_only`. Priority 0 = user-clicked single model (jumps the queue), 5 = category, 10 = bulk.
* **Resumability:** `bytes_done` is checkpointed every 256 MiB along with a **serialized hasher state**? No — `hashlib` objects are not picklable across restarts. Instead, resumption is at **file granularity**: a partially hashed file restarts from 0, and `bytes_done` exists purely for progress display. Justification: at 150 MB/s, the worst-case lost work is one file (~24 GB max here ≈ 2.7 min), which is acceptable versus the complexity and fragility of custom Merkle checkpointing.
* **Cancellation:** a `threading.Event` checked per chunk → sub-second response.
* **Windows file locking:** open with `open(path,'rb')` (share-read is the CPython default on Windows). On `PermissionError`/`WinError 32` (ComfyUI holding the file), mark `state='failed'`, `error_code='FILE_LOCKED'`, `attempts+=1`, and auto-retry with exponential backoff up to 3 attempts. Never abort the queue.
* **Throttling:** `hash_throttle_mbps` config (default 0 = unlimited). A simple token bucket in the read loop lets the user hash without starving a live ComfyUI render.
* **UI states surfaced per model:** `unhashed | queued | hashing | done | failed | stale`. The details panel shows a "Compute hash" button when `unhashed`, a determinate bar with MB/s when `hashing`, and the AutoV2 + a Civitai link when `done`.
* **Civitai enrichment is a consumer, not a coupler:** when a hash completes, the writer enqueues a Civitai lookup *if* `online_enabled`. Enrichment lands progressively; the grid re-renders those cards via the SSE `item` event. If offline, the hash is still stored and enrichment happens later.
* **Budget:** 1.5 TB at ~150 MB/s ≈ **2.8 hours**. The UI must state this estimate before the user starts a full-vault hash.

### 4.3 B3 — Architecture / base-model detection

**Module: `backend/app/parsers/arch_detect.py`. Scans the FULL tensor key set. Never `[:100]`.**

Header reading is unchanged in cost — the header is one read, and iterating 2,515 keys instead of 100 is free (measured: all 231 files parse in 0.23 s).

#### Layer 0 — Integrity (new, fixes a real corruption found on disk)
```
magic8 = first 8 bytes
header_len = u64le(magic8)
if header_len < 2 or header_len > min(200 MiB, file_size - 8):  → integrity='invalid_header'
if header bytes are not valid UTF-8 JSON:                        → integrity='invalid_header'
if the first bytes look like '<!doctype'/'<html'/'version http': → integrity='not_a_model' (HTML error page / Git-LFS pointer)
if file_size < 4096:                                             → integrity='truncated'
```
`O:\ComfyUI\models\vae\flux2-vae-new.safetensors` is a **129 KB HuggingFace HTML error page**. It must render as a red "Corrupt / not a model" card, not as "VAE". `integrity` values: `ok | invalid_header | not_a_model | truncated | unreadable | unsupported_format`.

#### Layer 1 — Declared metadata (authoritative when present; 101/230 files have `__metadata__`, 6 have `modelspec.architecture`)
Ordered probe of `__metadata__`:
`modelspec.architecture` → `ss_base_model_version` → `ss_sd_model_name` → `general.architecture` (GGUF) → `model_type` → `config` (JSON blob → `.architectures[0]`, `._class_name`) → `modelspec.title`.
Mapped through a normalization table to the canonical vocabulary (§4.3.1). `arch_source='metadata'`, `arch_confidence=0.95`.

#### Layer 2 — Adapter (LoRA/LyCORIS) detection, run **before** base detection
Suffix vocabulary confirmed on disk:
| Suffix pattern | Format |
|---|---|
| `.lora_A.weight` / `.lora_B.weight` | PEFT / ai-toolkit |
| `.lora_down.weight` / `.lora_up.weight` | kohya |
| `.lora.down.weight` / `.lora.up.weight` | diffusers |
| `.alpha` | kohya alpha scalar |
| `.hada_w1_a`, `.hada_w2_b` | LoHa |
| `.lokr_w1`, `.lokr_w2` | LoKr |
| `.diff`, `.diff_b` | full-diff patch |
| `.oft_blocks` | OFT |

If ≥5% of keys match → `is_adapter=1`, `adapter_format=<format>`, `rank` inferred from the `lora_A`/`lora_down` second dimension. Then **strip the adapter suffix and the `lora_unet_`/`lora_te_`/`diffusion_model.`/`transformer.` prefix**, and feed the residual key set into Layer 3 to determine the *base*. This is why "most loras → Unknown" today: they were never stripped.

Observed real LoRA prefix families and their base mapping:
`diffusion_model.blocks.*.cross_attn` → **WAN video** · `transformer.transformer_blocks.*.attn.to_*` → FLUX/SD3-class DiT · `diffusion_model.layers.*.adaLN_modulation` → Lumina/NextDiT-class · `lora_unet_down_blocks_*` → SD1.5/SDXL (disambiguate by context dim) · `text_encoders.*` → text-encoder LoRA.

#### Layer 3 — Structural prefix signature (the workhorse, ~85% of files)
An **ordered** rule table matched against `top_prefixes = sorted(set(k.split('.')[0] for k in keys))` plus targeted substring probes. Every entry below was verified against the real install:

| # | Signature (all must be present unless noted) | Result | Role |
|---:|---|---|---|
| 1 | `model` + `text_encoders` + `vae` | FLUX.1 (or successor) **bundled** | checkpoint |
| 2 | `conditioner` + `first_stage_model` + `model` | SDXL | checkpoint |
| 3 | `cond_stage_model` + `first_stage_model` + `model` (+`alphas_cumprod`) | SD1.5 / SD2.x (→ L4) | checkpoint |
| 4 | `audio_vae` + `vocoder` + `text_embedding_projection` | **ACE-Step audio** | checkpoint |
| 5 | `conditioner` + `model` + `pretransform` | Stable Audio | checkpoint |
| 6 | `cap_embedder` + `context_refiner` + `noise_refiner` | Lumina/NextDiT | unet |
| 7 | `double_blocks` + `single_blocks` + `img_in` + `txt_in` + `time_in` | FLUX.1 (BFL layout) | unet |
| 8 | `img_in` + `time_text_embed` + `transformer_blocks` + `norm_out` + `proj_out` | FLUX.1 (diffusers layout) | unet |
| 9 | `joint_blocks` ∨ `x_embedder`+`context_embedder`+`t_embedder` | SD3 / MMDiT | unet |
| 10 | `patch_embedding` + `text_embedding` + `time_embedding` + `blocks` + `head` | **WAN video** | unet |
| 11 | rule 10 + `patch_embedding_pose`/`motion_encoder`/`face_adapter` | WAN Animate variant | unet |
| 12 | `input_blocks` + `input_hint_block` + (`zero_convs` ∨ `middle_block_out`) | ControlNet (SD1.5; +`label_emb` → SDXL) | controlnet |
| 13 | `controlnet_down_blocks` + `controlnet_cond_embedding` (+`add_embedding`→SDXL) | ControlNet (diffusers) | controlnet |
| 14 | `controlnet_blocks` + `controlnet_x_embedder` (+`single_transformer_blocks`) | ControlNet (FLUX) | controlnet |
| 15 | `encoder`+`decoder`+(`post_quant_conv` ∨ `quant_conv`) **and no** `model`/`conditioner`/`cond_stage_model` | **standalone VAE** | vae |
| 16 | `encoder` + `shared` (+`spiece_model`) | T5 text encoder | text_encoder |
| 17 | `text_model` only | CLIP-L/G | text_encoder |
| 18 | `vision_model` + `visual_projection` | CLIP vision | clip_vision |
| 19 | `embed_tokens` + `layers` + `norm` (+`tokenizer_json`) | LLM text encoder (Qwen/Gemma) | text_encoder |
| 20 | `language_model` + `vision_tower` + `multi_modal_projector` | VLM text encoder | text_encoder |
| 21 | `initial_conv` + `res_blocks` + `upsampler` + `final_conv` | latent upscaler | upscaler |
| 22 | `model.0.weight` + `model.1.sub.*` (RRDB) ∨ `body.*.rdb*` | ESRGAN-class upscaler | upscaler |
| 23 | `blocks` + `encode` (only) | frame interpolation | other |
| 24 | `emb_params` ∨ `string_to_param` ∨ `clip_g`/`clip_l` 2-D only, <10 keys | textual-inversion embedding | embedding |

**Rule 15 is the explicit fix for B3's "flux1-dev-fp8 → VAE" misdetection.** The old code matched `post_quant_conv`/`decoder.conv_in` anywhere in the key set and stopped. The new rule is *negative-gated*: a VAE-family signature only yields `role='vae'` when **no** UNet/DiT family and **no** text-encoder family is present. Rules 1–14 are tried before 15 and win.

#### Layer 3b — Component decomposition (fixes the 16.87 B vs 12 B param bug)
Independently of the winning rule, classify **every** tensor key into a component by prefix family and sum params per component:

```
components = {
  "unet":         {"params": 11_901_408_320, "dtype": "F8_E4M3"},
  "text_encoder": {"params":  4_887_121_408, "dtype": "F16"},
  "vae":          {"params":     83_819_683, "dtype": "F16"},
}
```
* `param_count_primary` = the **unet/transformer** component (what users mean by "12B"), stored as an integer.
* `param_count_total` = sum of all components.
* `is_bundled = len(components) >= 2`.
* `precision` is reported **per component** and as a dominant summary; `scaled_fp8` key present → `quantization='comfy_scaled_fp8'`. GGUF → `quantization` from `general.file_type`.

#### Layer 4 — Shape probes (disambiguation)
When Layer 3 returns an SD-family checkpoint, read the cross-attention context dim from
`model.diffusion_model.input_blocks.*.1.transformer_blocks.0.attn2.to_k.weight` `shape[1]`:
**768 → SD1.5**, **1024 → SD2.x**, **2048 → SDXL**. Also: `add_embedding.linear_1` present → SDXL. Number of `down_blocks`/`input_blocks` distinguishes SDXL from SDXL-Refiner.

#### Layer 5 — Priors (lowest confidence)
Category directory (`models/loras/` etc.), filename tokens (`pony`, `illustrious`, `noobai`, `sdxl`, `flux`, `wan`, `qwen`, `hunyuan`, `ltx`, `sd15`), and total param count buckets. `arch_confidence ≤ 0.5`; the UI shows an "inferred" dot next to such badges.

**Pony and Illustrious** are SDXL derivatives and are *not* structurally distinguishable from SDXL. They are resolved by, in order: Civitai `baseModel` (after hashing) → `__metadata__` title → filename token → else reported as `SDXL` with `base_model_variant=NULL`. This limitation is documented in the UI tooltip rather than guessed at.

#### 4.3.1 Canonical vocabulary (frozen — used by DB, API, UI facets, MCP)
`base_model_family` ∈ `SD1.5 | SD2.x | SDXL | SD3 | FLUX.1 | FLUX.2 | Pony | Illustrious | NoobAI | Lumina | HiDream | Qwen-Image | WAN | HunyuanVideo | LTX-Video | Mochi | CogVideo | ACE-Step | StableAudio | Hunyuan3D | Cascade | AuraFlow | Kolors | PixArt | Other | Unknown`

`model_role` ∈ `checkpoint | unet | vae | text_encoder | clip_vision | controlnet | lora | embedding | upscaler | latent_upscaler | ipadapter | style_model | gligen | hypernetwork | frame_interpolation | geometry | detection | audio_encoder | other | unknown`

`modality` ∈ `image | video | audio | 3d | multimodal | text | unknown`

#### 4.3.2 Non-safetensors formats
* **`.gguf`** — parse the GGUF header natively (magic `GGUF`, version, KV pairs). Pure struct reads, no dependency. `general.architecture` is authoritative.
* **`.pt` / `.pth` / `.ckpt` / `.bin`** — **NEVER unpickle.** Two safe paths:
  1. If it is a ZIP (modern `torch.save`): `zipfile` → find `*/data.pkl` → `pickletools.genops()` and collect `SHORT_BINUNICODE`/`BINUNICODE` string operands. Verified working on `models/controlnet/control_v11f1p_sd15_depth.pth` (682 strings, real tensor keys). Feed those keys into Layers 2–5.
  2. If not a ZIP (legacy pickle, e.g. `4x-UltraSharp.pth`): mark `header_parsed=0`, `integrity='unsupported_format'`, fall back to Layer 5 priors only. Do not attempt heroics.
  `pickletools.genops` only *disassembles* the opcode stream; it never executes a `REDUCE`/`GLOBAL`. This is the security boundary.
* **`.onnx`** — read the protobuf `graph.name`/`producer_name` via a minimal varint scan; otherwise priors only.
* **Skip list:** `.crdownload`, `.part`, `.tmp`, `.download`, `.gitkeep`, `.yaml`, `.txt`, `.md`, `put_*_here`, any file `< 4096` bytes, `desktop.ini`, `Thumbs.db`. `Unconfirmed 46066.crdownload` exists in `models/checkpoints` right now and must be listed under a "Partial downloads" health item, not indexed as a model.

### 4.4 B4 — Node-class extraction

**Module: `backend/app/parsers/node_ast.py`. Absolute rule: never `import`, never `exec`, never `subprocess` a package's code.**

Measured baseline (this design's detector, run against the real `custom_nodes/`): **943 node classes across 32 packages, 30 of 32 non-zero.** The two zeros are correct: `ComfyUI-Manager` literally declares `NODE_CLASS_MAPPINGS = {}`, and `ComfyUI-Unreal` contains no `.py` files at all. **Coverage target: ≥95% of packages that genuinely register nodes yield ≥1 class; ≥90% of individual class ids recovered.**

Six strategies, unioned, each recording provenance so the UI can show confidence:

| S | Strategy | Catches (real examples) |
|---|---|---|
| **S1** | Literal `NODE_CLASS_MAPPINGS = {...}` in **any** `.py` under the package (recursive walk, `__pycache__`/`.git`/`node_modules`/`web`/`js`/`tests` pruned), unioned across modules | `was-ns` (220), `comfyui_controlnet_aux/node_wrappers/*` (66), `ComfyUI-WanVideoWrapper` submodules (147) |
| **S2** | Augmenting assignment: `NODE_CLASS_MAPPINGS.update(X)`, `{**A, **B}`, `dict(A, **B)`, `NODE_CLASS_MAPPINGS \|= X` — resolve the RHS `Name` back through the module's `import` table to a file, then apply S1 there | `ComfyMath` (`{**convert_NCM, **bool_NCM, …}` → 61), `comfyui_controlnet_aux` |
| **S3** | Re-export: `from .mod import NODE_CLASS_MAPPINGS [as X]` — resolve `.mod` / `..pkg.mod` to a path (handles `src/` layouts, `__init__.py` packages) and recurse S1–S3, depth ≤ 4 | `ComfyUI_IPAdapter_plus` (37), `was-ns` |
| **S4** | **V3 schema**: any `Call` to `Schema(...)`/`SchemaV3(...)`/`IO.Schema(...)` with a constant `node_id=` kwarg; also capture `display_name`, `category`, `description`, `is_deprecated`, `is_experimental` | **453 official core nodes in ComfyUI 0.33** — invisible to every other strategy |
| **S5** | **Structural class scan**: `ClassDef` having an `INPUT_TYPES` method **and** ≥1 of `RETURN_TYPES`/`FUNCTION`/`CATEGORY`/`OUTPUT_NODE` | `ComfyUI-KJNodes` (195 — built via `generate_node_mappings(NODE_CONFIG)`, defeats S1–S4), `ComfyUI-UltimateUpsacaler` (6) |
| **S6** | **Registry enrichment** (no code read at all): ComfyUI-Manager's `extension-node-map.json` (5,590 repos, keyed by git remote URL) + `pyproject.toml [tool.comfy]` + `node_list.json` | Names, display names, and the authoritative class list for anything the AST misses |

**Merge and confidence:**
`confidence='declared'` if from S1–S4 (an explicit registration id). `confidence='inferred'` if only from S5 (class name used as id — usually correct, since most suites key the mapping by class name, but not guaranteed). `confidence='registry'` if only from S6. When S5 produces a name already present from S1–S4, it contributes `implementation_file`/`lineno` and the `category` but does not change confidence. `node_classes.source` stores a JSON array of contributing strategies.

**Enrichment detail per class** (this is what powers the Nodes tab's "deep detail"): `node_id`, `display_name`, `category`, `description` (from the class docstring or `DESCRIPTION`), `input_types` (best-effort literal parse of the `INPUT_TYPES` return dict — required/optional names + type strings), `return_types`, `return_names`, `output_node`, `function`, `is_deprecated`, `is_experimental`, `source_file`, `source_lineno`.

**Official core nodes** are indexed as a synthetic package `__comfyui_core__` (`is_official=1`), scanning `nodes.py` (65 via S1) + `comfy_extras/*.py` (120 via S1 + 453 via S4) = **638 classes**. Without this the Workflows tab's "missing nodes" is meaningless — it is the single largest correctness lever after B1.

**Graceful degradation ladder** (a package always produces *something*):
1. AST strategies →
2. registry lookup by normalized git remote →
3. registry lookup by folder name →
4. `pyproject.toml`/`node_list.json` →
5. zero classes + `extraction_status='no_classes_found'` + a UI "Could not read node list" note with a "Report" affordance. Never a crash, never a silent empty.

**Package metadata** additionally captured: `pyproject.toml` (`name`, `version`, `description`, `dependencies`, `[tool.comfy] PublisherId/DisplayName/Icon`), `requirements.txt`, README first paragraph, `LICENSE` type, `WEB_DIRECTORY` presence, folder size, file count.

**Disabled packages:** a trailing `.disabled` (e.g. `ComfyQR.disabled`) or a `.disabled` marker file → indexed with `enabled=0` and shown greyed in a "Disabled" facet. `custom_nodes/__pycache__` and `*.example` are skipped. Loose top-level `.py` files (`websocket_image_save.py`) are indexed as single-file packages.

### 4.5 Node-package update status

Read `.git` **as files** — no `subprocess` on the hot path (25 repos × `git fetch` would take minutes and needs network):
* `.git/config` → `remote.origin.url` (normalize: strip `.git`, lowercase host, strip credentials).
* `.git/HEAD` → branch ref → `.git/refs/heads/<b>` or `.git/packed-refs` → local commit SHA.
* `.git/FETCH_HEAD` mtime → `last_fetch_at`.
* Commit date from `.git/logs/HEAD` last line (no object parsing needed).

Online check (only when `online_enabled`, rate-limited, cached 6 h in `civitai_cache`-style `http_cache`): GitHub `GET /repos/{owner}/{repo}/commits?sha={branch}&per_page=1` → compare SHA → `has_update`, `commits_behind` via `/compare/{local}...{remote}` (best-effort). Unauthenticated GitHub allows 60 req/h; with 25 repos and a 6 h cache this fits comfortably. Optional user-supplied token raises it to 5,000/h.

**Suspect-remote flag:** `was-ns`'s `remote.origin.url` is `https://github.com/Comfy-Org/ComfyUI` — the wrong repository. When the remote's repo basename does not fuzzy-match the folder name and is not found in the ComfyUI-Manager registry under that folder, set `repo_url_suspect=1` and suppress update claims for that package. Presenting "update available" from the wrong repo is worse than presenting nothing.

### 4.6 B5 — launcher

`start_app.bat` is rewritten (owned by the `docs` agent, verified by `qa`): `python -m uvicorn app.main:app --host 127.0.0.1 --port 8127 --app-dir backend`. `--cwd` does not exist; `--app-dir` is the correct flag. Port moves 8000 → **8127** to avoid the extremely common collision with other local dev servers; the Vite proxy and `VITE_API_TARGET` follow.

### 4.7 B6 — config desync

**`backend/app/core/config_service.py` is the only module in the codebase permitted to answer "where is ComfyUI?".** See §9.1.

---

## 5. Search & vector layer

### 5.1 What is deleted

`services/vector_search.py` in its entirety. Rebuilding a 256-term vocabulary and re-vectorizing every document on every keystroke is O(corpus) per query and cannot meet the latency bar.

### 5.2 Lexical — SQLite FTS5

```sql
CREATE VIRTUAL TABLE search_fts USING fts5(
    uid UNINDEXED,           -- 'model:41', 'output:930', ...
    kind UNINDEXED,          -- model | node_package | node_class | workflow | output
    title,                   -- weight 10.0
    subtitle,                -- weight 4.0   (base model, package, category)
    body,                    -- weight 1.0   (description, prompt, node list)
    tags,                    -- weight 6.0
    tokenize = "unicode61 remove_diacritics 2 tokenchars '-_.'",
    prefix = '2 3 4'
);
```

* `tokenchars '-_.'` keeps `sd_xl_base_1.0` and `flux1-dev-fp8` as single searchable tokens — essential for filenames.
* `prefix='2 3 4'` builds prefix indexes so `flu*` is index-served, enabling type-ahead.
* **`porter` is deliberately NOT used.** Model/node identifiers are not English prose; stemming `loras`→`lora` helps marginally while mangling `WanVideoSampler`. Recall is instead handled by the prefix index and the vector arm.
* **Maintenance: explicit upserts, not triggers.** All writes already funnel through one writer thread (§2.2), so a `delete_fts(uid)` + `insert_fts(uid, …)` pair inside the same transaction as the row upsert is simpler, debuggable, and avoids trigger-ordering surprises with `ON CONFLICT DO UPDATE`. Triggers are rejected because FTS5 has no `UPDATE` semantics for external ids and the trigger set would need to mirror every column-set change.
* Ranking: `bm25(search_fts, 0, 0, 10.0, 4.0, 1.0, 6.0)` (lower is better; negated for fusion).
* Query sanitization: user input is tokenized and rebuilt as a quoted `"tok1"* "tok2"*` FTS expression. Raw input is never passed through, so `AND`/`NEAR`/`"` cannot produce a syntax error. An advanced mode (`raw=true`) passes the expression verbatim for power users and returns `SEARCH_SYNTAX` on error.

### 5.3 Semantic — local ONNX embeddings (USER-DECIDED)

**Model: `all-MiniLM-L6-v2`, ONNX, INT8-quantized, 384 dimensions.**
Source: the `Xenova/all-MiniLM-L6-v2` ONNX export (`onnx/model_quantized.onnx` ≈ 23 MB, or `onnx/model.onnx` fp32 ≈ 90 MB) plus `tokenizer.json`, `config.json`.

**Justification:** 384 dims × 4 bytes = 1.5 KB per item, so 10k items is a 15 MB matrix that lives in RAM; MiniLM-L6 is the standard compact general-purpose sentence embedder with excellent short-text quality; the INT8 ONNX export runs on `onnxruntime` CPU at ~1–3 ms per short document single-threaded, so a full 10k-item index build is ~10–30 s and an incremental update is milliseconds. Crucially it needs **no torch** — `onnxruntime` + `tokenizers` are two self-contained wheels totalling ~60 MB, versus torch's 2+ GB.

**Storage:** `backend/data/models/all-MiniLM-L6-v2/` (`model.onnx`, `tokenizer.json`, `config.json`, `MANIFEST.json` with sha256 + license).

**First-use flow (explicitly user-initiated, never automatic):**
1. Fresh install → `embeddings.state = 'not_installed'`; the Smart toggle renders disabled with tooltip *"Smart search needs a one-time 23 MB download."*
2. User clicks Smart (or Settings → Enable Smart Search) → `POST /api/v1/embeddings/enable` → downloads from the configured `embedding_model_url` (HuggingFace CDN) into a `.part` file, verifies sha256 against `MANIFEST.json`, atomically renames.
3. Progress via `GET /api/v1/embeddings/status` (`downloading`, `bytes_done/bytes_total`).
4. On success → `state='ready'`, a background full-index embedding build starts (SSE `event: embed_progress`).
5. **No network / download fails / hash mismatch** → `state='unavailable'`, `reason` set, Smart toggle stays disabled with an explanatory tooltip, and **`/api/v1/search` keeps working via FTS5 alone**. No error is surfaced to a search request.
6. **Offline install path:** the user may drop the three files into `backend/data/models/all-MiniLM-L6-v2/` manually; startup detects them and flips to `ready`. This is documented in the README.

**Runtime:**
```python
ort.InferenceSession(path, providers=["CPUExecutionProvider"],
                     sess_options=so)      # so.intra_op_num_threads = min(4, cpu)
```
Tokenize with `tokenizers.Tokenizer.from_file(...)`, truncate to 256 tokens, mean-pool the last hidden state over the attention mask, L2-normalize. Batch size 32 for indexing, 1 for queries.

**Document text per item** (the `embed_text` builder is shared with FTS `body`):
* model → `name · role · base_model · precision · params · folder · civitai description · trigger words · tags`
* node package → `name · author · description · top 40 class display names`
* node class → `display_name · category · description · package · input/output type names`
* workflow → `name · folder · node class list · positive prompt excerpt · models used`
* output → `filename · positive prompt · negative prompt · model · sampler · album`

**Persistence:** `embeddings(uid PK, kind, dim, vec BLOB, text_hash, model_id, created_at)`. `vec` is raw little-endian float32 (`np.ndarray.tobytes()`), 1536 bytes at dim 384. Recomputed **only** when `text_hash` (blake2b of `embed_text`) changes. Query embeddings are never persisted; a 512-entry LRU caches them in memory.

**Query path:** at startup (and on invalidation) the `VecIndex` loads all vectors into one contiguous `np.ndarray(float32, shape=(N,384))` plus a parallel `uids` list. A query is `scores = M @ q` — for N=10,000 that is 3.84 M multiply-adds, **~1–2 ms** with NumPy's BLAS. `np.argpartition` takes the top 200.

**Decision: brute-force NumPy, no ANN index, no `sqlite-vec`/`sqlite-vss`.** At 10k items a full matmul is already ~1 ms, which is faster than an HNSW graph traversal plus its build cost, and it adds zero dependencies, zero native SQLite extension loading (a real fragility on Windows), and zero index-staleness bugs. A `# SCALE NOTE` in the code documents the crossover: if `N > 150_000`, revisit with IVF or `sqlite-vec`. This install has ~3,900 items; the design target is 10k; the headroom is 15×.

Memory: 10k × 1.5 KB = **15 MB**. 100k would be 150 MB — still acceptable.

### 5.4 Hybrid ranking

**Reciprocal Rank Fusion, k = 60.**

```
score(d) = Σ_arms  w_arm / (k + rank_arm(d))
w_lexical = 1.0,  w_vector = 0.8
```

**Justification:** RRF fuses rankings, not scores, so it needs no normalization between BM25 (unbounded, negative) and cosine (−1…1), requires no per-corpus tuning, and degenerates gracefully to a single arm when the other returns nothing or is unavailable. It is also trivially explainable in the UI ("matched: name, semantic").

Pipeline: each arm returns its top 200 → fuse → apply structured filters (`kind`, `category`, `base_model`, `album`, date/size ranges) **after** fusion → apply the requested sort (default `relevance`) → paginate. Exact-substring matches on `title` get a `+0.35` post-fusion bonus so that typing an exact filename always puts it first.

**"Smart" toggle mapping:**
| Smart | Behaviour |
|---|---|
| **OFF** (default) | Lexical only. `mode='lexical'`. Deterministic, instant, exact-match friendly. |
| **ON**, embeddings ready | Hybrid RRF. `mode='hybrid'`. Enables conceptual queries ("anime style lora for flux", "video model that does lip sync"). |
| **ON**, embeddings unavailable | Server returns `mode='lexical'` + `smart_available=false` + `smart_reason`. The UI shows the toggle in a dimmed "unavailable" state with a tooltip. **Never an error response.** |

The response always echoes `mode` and `smart_available` so the frontend never has to guess.

### 5.5 Latency budget

| Path | Target p95 @10k items |
|---|---|
| FTS5 lexical (200 results) | **≤ 25 ms** |
| Query embedding (1 doc, 256 tok, INT8) | ≤ 8 ms |
| Vector matmul + top-k | ≤ 5 ms |
| Fusion + filter + hydrate 100 rows | ≤ 20 ms |
| **Hybrid end-to-end** | **≤ 70 ms** |

Frontend debounces keystrokes at **140 ms** and cancels the in-flight request with `AbortController`.

---

## 6. Thumbnail cache

3,569 outputs (3,070 PNG + 451 MP4 + 60 MP3 + 48 JPG + 26 GLB + 35 FBX + 23 EXR + 15 WAV + 12 FLAC) cannot be streamed at full size.

**Storage:** `backend/data/thumbs/<h[0:2]>/<h[2:4]>/<h>_<size>.webp` where `h = blake2b(normcase(abs_path), 16).hexdigest()`. Two levels of fan-out keeps any NTFS directory under ~1,000 entries, which matters for `os.scandir` performance during GC.

**Sizes:** exactly three — **160, 320, 640** (longest edge, aspect preserved, never upscaled). Three tiers cover the whole slider range at ≤2× downscale, which the browser handles with no visible softness.

**Format:** WebP, quality 82, `method=4`. Roughly 6–14 KB at 320 px versus 2–8 MB for a source PNG — a ~500× reduction. WebP is supported by every Chromium/Firefox/WebView2 build the app can run in, and Pillow encodes it natively. Animated sources are flattened to their first frame.

**Generation: lazy on-demand with in-flight dedupe.**

`GET /api/v1/files/thumbnail?uid=output:930&size=320`:
1. Compute cache path. Hit → `FileResponse` with `ETag: "<fingerprint>-320"`, `Cache-Control: public, max-age=31536000, immutable`, `Last-Modified`. `If-None-Match` → `304`.
2. Miss → check `ThumbService._inflight: dict[key, Future]`. If present, await it (this is what stops 60 simultaneous grid tiles from generating the same thumb 60 times on a fast scroll).
3. Otherwise submit to `EX_IMG`, generate, write to a `.part` file, `os.replace` (atomic on NTFS), return.

**Decision: lazy, not batch.** A cold batch of 3,569 thumbnails costs ~15 s of CPU and ~40 MB of disk that most users never look at; lazy generation makes first paint instant and the cache warms exactly to the user's browsing pattern. A **warm-ahead** optimization runs in the background: when the client requests page N, the service pre-generates the first 24 thumbs of page N+1 at low priority. An explicit "Pre-generate all thumbnails" button exists in Settings for users who want it.

**Per-kind source:**
| Kind | Thumbnail source |
|---|---|
| Image output (png/jpg/webp/exr) | Pillow. EXR via Pillow's `OpenEXR` support if built, else placeholder. `Image.draft()` used for JPEG to decode at reduced scale — 3–5× faster. |
| Video output (mp4/webm) | **First keyframe via a minimal MP4 box parser + Pillow?** No. **Decision: no frame extraction.** Extracting an H.264 frame requires ffmpeg or PyAV, both heavyweight. Videos get a generated placeholder card (duration + resolution read from the MP4 `mvhd`/`tkhd` boxes with a ~60-line pure-stdlib parser) with a play glyph. A `video_thumbnails` config flag documents that installing `ffmpeg` on PATH enables real frames via `subprocess` — opt-in, absent by default. |
| Audio (mp3/wav/flac) | Generated waveform-style placeholder + duration from header. |
| 3D (glb/fbx/obj) | Generated placeholder with the format badge. |
| Workflow `.json` | Rendered node-count/complexity card; if a sibling `<name>.png` exists, use it. |
| Model | Civitai preview image (downloaded once, stored in the same cache, only when `online_enabled`) → else a **deterministic generated placeholder**: a 2-stop gradient whose hue is `blake2b(base_model_family)` mod 360, overlaid with the role glyph and the base-model abbreviation. Verified: there are **zero** sidecar preview images in `O:\ComfyUI\models`, so placeholders are the normal case, not the exception. |

**Invalidation:** the cache key includes the path hash only, but the **ETag includes the fingerprint**. A changed file yields a new ETag, so the browser refetches; the server compares `fingerprint` against `thumb_cache.fingerprint` and regenerates on mismatch. On rename/move, `ThumbService.relocate(old, new)` renames the cache files (cheap) rather than regenerating.

**GC:** `POST /api/v1/system/thumbs/gc` (and an automatic sweep on startup if `thumbs_bytes > thumb_cache_max_mb`, default 2048) deletes by LRU `atime` and orphan check.

**Slider mapping (frozen):**
```
slider ∈ [96 … 384] px  (step 8, default 208)
served = 160  if slider <= 160
         320  if slider <= 320
         640  otherwise
```
The `<img>` gets `width`/`height` attributes from the stored dimensions to reserve layout space and eliminate scroll jank, plus `loading="lazy"` and `decoding="async"`.

---

## 7. Frontend architecture

* **State:** one `useReducer` + `VaultContext`. No Redux, no Zustand, no React Query — the app has ~8 server resources and a hand-rolled `useResource(key, fetcher)` hook with an in-memory cache, `AbortController` cancellation, and SWR-style revalidation is ~80 lines. Keeps `package.json` at three runtime deps (`react`, `react-dom`, `lucide-react`).
* **Virtualization: hand-rolled.** A `useVirtualGrid({itemCount, itemSize, gap, overscan: 2})` hook computes `columns = floor((w+gap)/(itemSize+gap))`, total height, and the visible row window from `scrollTop`, then renders only those rows inside a spacer div. ~120 lines, no `react-window` dependency, and it handles the size slider's dynamic reflow natively. Cap: ≤ 150 mounted cards regardless of dataset size.
* **Routing:** none. Tabs are state. No `react-router`.
* **Data flow:** the grid requests one page at a time (`limit` from the status-bar per-page selector: 50/100/200/500, default 100). Facet counts come from a separate `/facets` call so the grid query stays a simple indexed range scan.
* **SSE:** a single `EventSource` per active job, opened on demand, closed on `done`. `item` events patch the local cache in place (no full refetch), which is what makes progressive Civitai/hash enrichment feel live.
* **Details panel:** lazy — selecting a card fires `GET /:id` for the deep record (heavy fields like the full graph, component breakdown, and usage list are not in the list payload).

### 7.1 Design tokens — Geekatplay identity (frozen; shared by the CSS and the React components)

Same *structure* as the reference, deliberately different *identity*. The current build's cyan-on-blue-black is retired.

**Palette — "Studio Graphite + Signal Amber"** (warm neutrals, not blue-blacks; single warm brand accent, one cool secondary):

```css
:root{
  /* surfaces — warm graphite ramp */
  --gp-bg-000:#0B0B0D; --gp-bg-100:#131317; --gp-bg-200:#1A1A20;
  --gp-bg-300:#23232B; --gp-bg-400:#2E2E38; --gp-bg-inset:#08080A;
  --gp-line-100:#2A2A33; --gp-line-200:#3A3A46; --gp-line-300:#4A4A58;

  /* brand — Geekatplay Amber */
  --gp-brand-100:#FFC46B; --gp-brand-200:#F2A03D; --gp-brand-300:#D9841F;
  --gp-brand-400:#B8721F; --gp-brand-ghost:rgba(242,160,61,.12);

  /* secondary — Vault Violet (semantic / Smart / AI features) */
  --gp-vio-100:#A79AF5; --gp-vio-200:#8B7BF0; --gp-vio-300:#6E5CD6;
  --gp-vio-ghost:rgba(139,123,240,.14);

  /* status */
  --gp-ok:#4CC38A; --gp-warn:#E8B341; --gp-danger:#E5484D; --gp-info:#6E9BF5;
  --gp-ok-ghost:rgba(76,195,138,.13); --gp-danger-ghost:rgba(229,72,77,.13);

  /* text — warm greys */
  --gp-fg-100:#F1EFEB; --gp-fg-200:#B9B5AD; --gp-fg-300:#8A867E;
  --gp-fg-400:#63605A; --gp-fg-on-brand:#17130B;

  /* type */
  --gp-font-ui:"Inter Tight","Inter","Segoe UI Variable Text",system-ui,sans-serif;
  --gp-font-mono:"JetBrains Mono","Cascadia Mono",ui-monospace,monospace;
  --gp-fs-10:10px; --gp-fs-11:11px; --gp-fs-12:12px; --gp-fs-13:13px;
  --gp-fs-15:15px; --gp-fs-18:18px; --gp-fs-22:22px; --gp-fs-28:28px;
  --gp-lh-tight:1.25; --gp-lh-base:1.45;
  --gp-fw-400:400; --gp-fw-500:500; --gp-fw-600:600; --gp-fw-700:700;
  --gp-tnum:"tnum" 1,"lnum" 1;          /* tabular figures for counts/sizes */

  /* spacing (4px base, 9 steps) */
  --gp-s-1:2px;  --gp-s-2:4px;  --gp-s-3:6px;  --gp-s-4:8px;  --gp-s-5:12px;
  --gp-s-6:16px; --gp-s-7:24px; --gp-s-8:32px; --gp-s-9:48px;

  /* radii — squarer than the reference */
  --gp-r-1:3px; --gp-r-2:6px; --gp-r-3:10px; --gp-r-4:16px; --gp-r-full:999px;

  /* elevation — hairlines over heavy shadows */
  --gp-e-0:none;
  --gp-e-1:0 1px 2px rgba(0,0,0,.40);
  --gp-e-2:0 6px 16px -6px rgba(0,0,0,.55);
  --gp-e-3:0 24px 48px -12px rgba(0,0,0,.65);
  --gp-focus:0 0 0 2px rgba(242,160,61,.45);

  /* motion */
  --gp-t-micro:90ms; --gp-t-base:140ms; --gp-t-overlay:220ms;
  --gp-ease:cubic-bezier(.2,.6,.2,1);

  /* layout geometry */
  --gp-topbar-h:52px; --gp-statusbar-h:28px;
  --gp-rail-w:264px;  --gp-rail-min:200px; --gp-rail-max:420px;
  --gp-details-w:340px; --gp-details-min:280px; --gp-details-max:520px;
}
@media (prefers-reduced-motion:reduce){
  :root{--gp-t-micro:0ms;--gp-t-base:0ms;--gp-t-overlay:0ms}
}
```

**Signature elements that make it read as Geekatplay, not as the reference:**
1. A **2 px amber "spine"** on the left edge of the selected rail row and along the top of the DETAILS panel header — the app's recurring motif.
2. **Hairline separation** (1 px `--gp-line-100`) instead of drop-shadowed floating cards; elevation is reserved for overlays.
3. **Square-leaning geometry** — 6 px card radius, 3 px chips — against the reference's pill-heavy look.
4. **Monospace tabular metadata** rows in DETAILS (`key … value` with a dotted leader), which reads as a technical instrument rather than a media browser.
5. **Amber = local/known, Violet = AI/inferred.** Every inferred or AI-derived value (Smart results, `arch_confidence < 0.7`, Ollama summaries) is tinted violet with a small `~` prefix. This is a functional convention, not decoration.
6. Wordmark: `GEEKATPLAY` in `--gp-fw-700` with `letter-spacing:.14em`, and `ASSET VAULT` beneath in `--gp-fs-10 / --gp-fg-300`. Footer of the rail: `Vladimir Chopine · v2.0`.

**Accessibility:** `--gp-fg-100` on `--gp-bg-200` = 14.8:1; `--gp-fg-200` on `--gp-bg-200` = 7.1:1; `--gp-brand-200` on `--gp-bg-200` = 7.6:1; `--gp-fg-on-brand` on `--gp-brand-200` = 9.2:1. All ≥ AA, body text ≥ AAA. Focus ring visible on every interactive element; full keyboard traversal (`/` focuses search, `Esc` closes overlays, arrows navigate the grid, `Enter` opens the lightbox).

---

## 8. Path & security model

### 8.1 `config_service` — the single source of truth (fixes B6)

```python
@dataclass(frozen=True)
class AppConfig:
    comfyui_path: Path | None
    is_configured: bool
    roots: tuple[Root, ...]          # resolved, ordered, deduped
    auto_reindex: bool
    online_enabled: bool
    civitai_enabled: bool
    civitai_api_key: str | None
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    smart_search_enabled: bool
    hash_concurrency: int
    hash_throttle_mbps: int
    thumb_cache_max_mb: int
    page_size_default: int
    watch_enabled: bool
    trash_mode: str                  # 'trash' | 'permanent'
    extra_workflow_dirs: tuple[Path, ...]

def get_config() -> AppConfig      # process-cached, invalidated on write
def set_config(patch: dict) -> AppConfig
def reload_config() -> AppConfig
```

Rules enforced by the `security` and `qa` agents:
* **`Settings` loses `COMFYUI_PATH` entirely.** `app/config.py` keeps only immutable build constants (`APP_NAME`, `VERSION`, `AUTHOR`, `DATA_DIR`, `DB_PATH`, `API_PREFIX`). Nothing mutates `settings` at runtime — that mutation *was* B6.
* The DB `config` table is the sole persistent store. `get_config()` reads it once and caches; `set_config()` writes and invalidates. **On restart the cache is cold and reloads from the DB, so desync is structurally impossible.**
* Environment override `COMFYUI_PATH` is honoured **only** when the DB has no value (first run convenience).
* A `qa` lint gate greps for `settings.COMFYUI_PATH` and `Path(settings.` and fails the build on any hit outside `config_service.py`.

### 8.2 Allowed roots

Computed once per config load, cached:
1. The ComfyUI root (`comfyui_path`).
2. Every `base_path` in `extra_model_paths.yaml` **plus** each resolved category directory under it (a category may point outside `base_path`, e.g. an absolute `D:\shared\loras`). **Note: `extra_model_paths.yaml` does not exist on the target machine** — only `.example` and `.hold`. The loader must therefore treat absence as normal and silent. `.hold`/`.example` are **not** read (that is ComfyUI's own convention for "disabled"); a Settings toggle `read_held_extra_paths` exists for users who want it, default OFF.
3. `extra_workflow_dirs` (user-added).
4. `backend/data/` (for thumbnails/trash).

`Root(id, kind, path, label, is_default, source)` rows are persisted so file operations can validate against them without re-parsing YAML.

### 8.3 Containment check — `core/pathsafe.py`

```python
def normalize(p: str | Path) -> Path:
    """realpath + normcase + long-path aware. Never raises."""

def is_contained(child: Path, root: Path) -> bool:
    c, r = normalize(child), normalize(root)
    if c.drive.lower() != r.drive.lower():   # commonpath raises across drives
        return False
    try:
        return os.path.commonpath([str(c), str(r)]) == str(r)
    except ValueError:
        return False

def resolve_within_roots(p: str | Path) -> tuple[Path, Root]:
    """Returns the normalized path and its owning root, or raises PathNotAllowed."""
```

Windows specifics, all mandatory:
* **`os.path.realpath` (not `Path.resolve`)** — resolves NTFS junctions, symlinks, and 8.3 short names (`PROGRA~1`). The current `base_root in resolved.parents` check is also subtly wrong: it fails when `child == root`'s own parent chain differs in case.
* **`os.path.normcase`** before comparison — NTFS is case-insensitive. `commonpath` is a *string* operation and would otherwise treat `O:\ComfyUI` and `o:\comfyui` as different.
* **Long paths:** any path ≥ 250 chars is prefixed `\\?\` before the syscall. `\\?\` disables normalization, so it must be applied *after* `realpath`. UNC becomes `\\?\UNC\server\share`.
* **Non-ASCII:** Python 3's `str` paths are UTF-16 on Windows natively. Never `.encode('ascii')`. All file reads use `errors='replace'`; all JSON/text reads try `utf-8` then `utf-8-sig` then `cp1252` with replacement.
* **Reserved names / trailing dots:** reject `CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9` and any name ending in `.` or ` ` as rename targets.
* **Rename validation:** reject `<>:"/\|?*`, control chars `< 0x20`, `..`, absolute paths, and any name that resolves outside the file's current directory.

### 8.4 Network posture

* Uvicorn binds **`127.0.0.1` only**. `0.0.0.0` is refused at startup unless `ALLOW_LAN=1` is explicitly set, and then a loud warning is logged.
* CORS is narrowed from `["*"]` to `["http://localhost:3000","http://127.0.0.1:3000"]` in dev and **disabled entirely** in production (frontend is served as static files from the same origin).
* No authentication (localhost single-user), but **every mutating endpoint requires `X-Vault-Request: 1`**, a header a cross-origin form/`<img>`/simple-request cannot set. This is CSRF protection appropriate to a localhost app and costs nothing.
* `/api/v1/files/raw` streams only paths that pass `resolve_within_roots`. It accepts a **`uid`**, not a raw path — the path never round-trips through the client. (The current `/api/outputs/file?path=…` design lets the client name any file; that plus B6 is a traversal risk.)
* Civitai/Ollama/GitHub calls: 10 s timeout, 2 retries with jitter, a circuit breaker that opens after 5 consecutive failures for 5 minutes, and a hard kill-switch (`online_enabled=false`) that short-circuits before any socket is created.
* Outbound requests never include the ComfyUI path, filenames, or prompts — only hashes and repo names.

---

## 9. File operations

### 9.1 Trash

**Decision: an app-managed trash directory per root — `<root>/.vault-trash/` — not the Windows Recycle Bin.** Rationale: Recycle Bin access needs `SHFileOperation` via `pywin32` or `send2trash` (a new dependency), it silently fails on network drives and on volumes with the bin disabled, and it makes "restore to original location" opaque. An app-managed trash is a plain `shutil.move` on the same volume — instantaneous, works everywhere, and lets us store exact restore metadata.

* Layout: `<root>/.vault-trash/<yyyymmdd-HHMMSS>-<8hex>/<original filename>` plus a sibling `meta.json` (`original_path`, `deleted_at`, `size`, `uid`, `kind`).
* Same-volume move ⇒ O(1) even for a 24 GB checkpoint.
* `trash_mode` config: `trash` (default) or `permanent`. `permanent` still requires an explicit `confirm=true` in the request body.
* Retention: entries older than `trash_retention_days` (default 30) are purged on startup. `GET/POST /api/v1/fileops/trash*` expose list / restore / empty.
* `.vault-trash` is excluded from all scan walks.

### 9.2 Rename / move / delete

All three go through `fileops_service`, which:
1. Resolves the `uid` to a DB row and its `abs_path` (client never supplies a raw path).
2. Validates via `resolve_within_roots` on **both** source and destination.
3. Validates the new name (§8.3).
4. Refuses if the destination exists (no silent overwrite; the API returns `CONFLICT` with the existing path so the UI can offer "Keep both" → auto-suffix `(2)`).
5. Performs the operation, then **inside one transaction** updates the DB row's `abs_path`/`rel_path`/`name`/`fingerprint`, re-points FTS and embeddings (the `uid` is stable, so only `title`/`body` change), and calls `ThumbService.relocate`.
6. On `PermissionError`/`WinError 32` returns `FILE_LOCKED` with a message naming ComfyUI as the likely holder.
7. Batch operations (`uids: [...]`) are per-item isolated and return a `results[]` array with per-item status — one failure never aborts the batch.

**Sidecar awareness:** renaming a model also renames co-located `<stem>.preview.png`, `<stem>.civitai.info`, `<stem>.json`, `<stem>.txt` when present.

---

## 10. Performance budgets (anchored to measurements against the reference install)

| Metric | Measured baseline (audit) | Budget | Method |
|---|---|---|---|
| Walk 231 model files | 0.00 s | ≤ 0.1 s | `os.scandir` |
| Parse 231 safetensors headers | 0.23 s | ≤ 0.6 s | full key set + arch detect, 8 threads |
| Analyze 35 node packages | 0.02 s (broken) | ≤ 3.0 s | 6 strategies over ~4k `.py` files, 4 threads |
| Analyze 638 official node classes | n/a (absent) | ≤ 2.0 s | AST over `nodes.py` + 132 extras |
| Walk 3,569 outputs | 0.21 s | ≤ 0.5 s | |
| Read metadata for 3,569 outputs | ~15 s single-threaded | **≤ 5 s** | 6 threads; `Image.open` reads headers only, no full decode |
| Parse 48 workflows | n/a | ≤ 0.5 s | |
| DB writes (~4,000 upserts) | n/a | ≤ 3 s | batched 256, WAL, `synchronous=NORMAL` |
| **Cold full index** | ∞ (crashed) | **≤ 25 s p95** | |
| **Warm incremental, no changes** | n/a (always full) | **≤ 1.5 s p95** | walk + fingerprint diff only |
| **Warm incremental, 20 changed files** | n/a | ≤ 3 s | |
| API first byte `/models?limit=100` | n/a | ≤ 40 ms p95 | indexed query, no JSON blob hydration in list mode |
| Frontend first paint | n/a | ≤ 400 ms | 194 kB bundle measured; keep ≤ 350 kB |
| First grid page fully painted (cached thumbs) | n/a | ≤ 700 ms | |
| Grid scroll | n/a | **60 fps, ≤ 150 mounted cards** | hand-rolled virtualization |
| Thumbnail: cached serve | n/a | ≤ 5 ms | |
| Thumbnail: cold generate (4 MP PNG → 320 WebP) | n/a | ≤ 120 ms | |
| Search lexical p95 @10k | O(corpus)/keystroke | **≤ 25 ms** | FTS5 |
| Search hybrid p95 @10k | n/a | **≤ 70 ms** | +ONNX +matmul |
| Embedding index build, 10k items | n/a | ≤ 45 s background | INT8 MiniLM, batch 32 |
| Hash throughput | n/a | ≥ 120 MB/s reported | 1.5 TB ≈ 2.8 h, resumable |
| Idle RSS | n/a | ≤ 220 MB | incl. 15 MB vector matrix + ORT session |

A `qa`-owned `backend/tests/perf/test_budgets.py` asserts the index and search budgets against a synthetic 10k-item fixture, and a `--bench` mode runs them against the real `O:\ComfyUI`.

---

## 11. Dependencies

### 11.1 Additions to `backend/requirements.txt`

| Package | Version | Why | Windows/py3.12 wheel |
|---|---|---|---|
| `PyYAML` | `>=6.0.1,<7` | Parse `extra_model_paths.yaml`. **Not currently installed** (verified). Hand-rolling a YAML subset is a bug farm — the file supports multi-line scalars (`loras: \|`), anchors, and comments. Loaded exclusively with `yaml.safe_load`. | Yes, `cp312` wheel |
| `onnxruntime` | `>=1.17,<2` | CPU inference for the MiniLM embedder (user decision). Self-contained ~55 MB; **does not pull torch**. | Yes, official `cp312-win_amd64` |
| `tokenizers` | `>=0.15,<1` | HuggingFace Rust tokenizer; loads `tokenizer.json` directly. Alternative (`transformers`) drags in torch/huggingface_hub. | Yes, prebuilt `cp312-win_amd64` |
| `pytest-asyncio` | `>=0.23` | dev — async router tests | pure python |
| `ruff` | `>=0.4` | dev — lint gate, replaces flake8+isort | prebuilt |

### 11.2 Version bumps
`pillow >= 10.3` (WebP encode + CVE fixes) · `fastapi >= 0.110` · `uvicorn[standard] >= 0.29` · `httpx >= 0.27`.

### 11.3 Explicitly rejected
| Rejected | Reason |
|---|---|
| `torch` / `transformers` / `sentence-transformers` | 2+ GB for a 23 MB task |
| `sqlite-vec` / `sqlite-vss` | Native SQLite extension loading is fragile on Windows; brute-force NumPy is ~1 ms at 10k and adds nothing to install |
| `faiss` / `hnswlib` | Same; ANN is unnecessary below ~150k vectors |
| `watchdog` | 30 s dir-mtime polling costs ~5 ms and leaks no OS handles on network drives |
| `send2trash` / `pywin32` | App-managed `.vault-trash` is simpler, works on network drives, and supports exact restore |
| `SQLAlchemy` / `alembic` | Explicitly excluded by the brief; hand-rolled `PRAGMA user_version` migrations are ~120 lines |
| `celery` / `rq` / `redis` | An in-process thread pool is correct for I/O-bound local work |
| `ffmpeg-python` / `PyAV` | Video thumbnails are opt-in via a PATH `ffmpeg`; no hard dep |
| `orjson` | stdlib `json` handles this corpus well within budget |
| `react-window`, `react-router`, `zustand`, `@tanstack/react-query` | ~200 lines of hooks replace all four and keep the bundle ≤ 350 kB |

### 11.4 Frontend
Unchanged: `react@^18.2`, `react-dom@^18.2`, `lucide-react@^0.294`. Dev: `vite@^4.4`, `@vitejs/plugin-react@^4`. **No new runtime dependencies.**

---

## 12. Keep / rewrite ledger

| Area | Verdict | Note |
|---|---|---|
| Router layout (`api/*_api.py`, one router per domain) | **Keep**, extend | Sound. Add `/v1` prefix, versioned errors, pagination, the new routers. |
| Wizard flow (status → validate → complete → scan) | **Keep**, harden | Good UX. Add path validation with a live preview of what was found, and remove the synchronous scan from the request (return a `job_id`). |
| `file_ops.py` guard *structure* (validate → act → report) | **Keep**, rewrite internals | The shape is right; `is_safe_path` is wrong (§8.3) and it must take `uid`s and update the DB transactionally. |
| `config.py` `Settings` | **Rewrite** | Strip `COMFYUI_PATH`; becomes immutable constants only (B6). |
| `database.py` | **Rewrite** | New schema, WAL, writer thread, migrations, `PRAGMA user_version`. |
| `scanner.py` | **Delete, rewrite** as `indexing/` | Single transaction, no fingerprints, no threads, no progress, in-request. Nothing salvageable. |
| `safetensors_parser.py` | **Delete, rewrite** as `parsers/safetensors_header.py` + `arch_detect.py` | `compute_autov2_hash` is actively wrong (B2); detection is `[:100]`-truncated (B3). |
| `node_analyzer.py` | **Rewrite** as `parsers/node_ast.py` | Keep the README/git/requirements harvesting idea; replace extraction wholesale (B4). |
| `workflow_parser.py` | **Rewrite** | Must use `graph_utils` (B1), structural pos/neg attribution, and the real 638-class official list. |
| `vector_search.py` | **Delete** | Replaced by `search/{fts,vec,hybrid}.py`. |
| `civitai_service.py` | **Keep**, harden | Response mapping is broadly right. Add caching, circuit breaker, `nsfw` handling, and decouple from the scan. |
| `ollama_service.py` | **Keep** | Already correct and already degrades. Now used only for optional prose enrichment — **never** for embeddings (user decision). |
| `mcp_server.py` / `mcp_api.py` | **Rewrite** | Not a conformant MCP server; see `MCP_SPEC.md`. |
| `frontend/src/index.css` | **Rewrite** | New token system (§7.1). |
| React components | **Rewrite** with the same names where sensible | Layout structure is retained; every component is re-authored against the token system and the virtualized grid. `FirstLaunchWizard` keeps its step flow. |
| `start_app.bat` | **Rewrite** | B5. |

---

## 13. Assumptions (stated, with chosen defaults)

1. **`extra_model_paths.yaml` is absent** on the target machine; `.hold`/`.example` are ignored. Default: absence is normal; `read_held_extra_paths` toggle exists, OFF.
2. **Both** `<root>\workflows` (28 files) and `<root>\user\default\workflows` (20) are scanned, plus per-user extra dirs. Root-level `workflows/` is non-standard but real here.
3. Workflow graphs embedded in `output/` PNGs are **not** indexed as separate workflow entities (they'd create 3,000 near-duplicates). They are parsed for the *output's* metadata, and a "Extract workflow from this output" action exists in the lightbox.
4. The ONNX embedder is downloaded on first explicit enable, not shipped in the repo (license + repo size). Offline manual placement is supported and documented.
5. Pony/Illustrious/NoobAI are reported as `SDXL` until a Civitai hash match or filename/metadata token disambiguates.
6. Hashing may run while ComfyUI is running; locked files are retried, never fatal.
7. Single-user, single-machine, localhost only. No multi-user, no auth.
8. `O:` is treated as a potentially slow/removable volume: a missing root degrades to "offline root" (rows soft-flagged), never a wipe.
