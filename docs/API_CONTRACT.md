# API Contract v1 — Geekatplay ComfyUI Asset Vault
**FROZEN.** Base URL `http://127.0.0.1:8127`. Prefix **`/api/v1`**.
Downstream agents MUST NOT invent endpoints, parameters, or field names. Changes go through the architect.

---

## 0. Global conventions

* `Content-Type: application/json; charset=utf-8` for all non-binary responses.
* Every response carries `X-API-Version: 1` and `X-Request-Id: <uuid4>`.
* **Every mutating request (POST/PATCH/PUT/DELETE) MUST send `X-Vault-Request: 1`.** Missing → `400 CSRF_HEADER_MISSING`. (CSRF defence for a localhost app; see ARCHITECTURE §8.4.)
* Timestamps in JSON are **integer epoch milliseconds UTC**. Sizes are **integer bytes**.
* Unknown query parameters are ignored (forward compatible). Unknown body fields are rejected (`VALIDATION_ERROR`) so typos surface.
* Absolute filesystem paths are returned for display only; they are **never accepted as input** — all operations take `uid`.

### 0.1 Error envelope (stable, versioned)

Every non-2xx response, without exception:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Model 4211 does not exist.",
    "details": { "uid": "model:4211" },
    "field_errors": [ { "field": "sort", "message": "must be one of name|created|…" } ],
    "request_id": "8e1f…",
    "retryable": false,
    "docs": "/docs#not_found"
  }
}
```
`details` and `field_errors` may be omitted. `error.code` is a **stable string** — clients branch on it, never on the message or the HTTP status alone.

### 0.2 Error code registry

| Code | HTTP | Meaning |
|---|---:|---|
| `VALIDATION_ERROR` | 422 | Body/query failed validation (`field_errors` populated) |
| `NOT_FOUND` | 404 | uid/id does not exist |
| `CONFLICT` | 409 | Destination exists; duplicate name |
| `PATH_NOT_ALLOWED` | 403 | Outside every configured root |
| `PATH_INVALID` | 422 | Illegal chars / reserved name / too long |
| `FILE_LOCKED` | 423 | Windows sharing violation (ComfyUI likely holding it) |
| `FILE_MISSING` | 410 | Indexed but gone from disk |
| `NOT_CONFIGURED` | 409 | Wizard not completed |
| `ROOT_UNAVAILABLE` | 503 | Configured volume not reachable |
| `JOB_ALREADY_RUNNING` | 409 | A scan/hash batch is already active |
| `JOB_NOT_FOUND` | 404 | |
| `FEATURE_UNAVAILABLE` | 503 | Smart search / online / ffmpeg disabled or missing |
| `UPSTREAM_UNAVAILABLE` | 502 | Civitai/GitHub/Ollama unreachable (`retryable: true`) |
| `RATE_LIMITED` | 429 | Upstream throttled; `details.retry_after_ms` |
| `SEARCH_SYNTAX` | 422 | Raw FTS expression rejected |
| `CSRF_HEADER_MISSING` | 400 | `X-Vault-Request` absent |
| `PAYLOAD_TOO_LARGE` | 413 | |
| `INSUFFICIENT_SPACE` | 507 | Target volume cannot hold the download (C9/R6); `details.shortfall_bytes` |
| `INTEGRITY_MISMATCH` | 502 | A download's size or hash did not match what the source published; the file was quarantined, never placed (C9/R4) |
| `INTERNAL` | 500 | Unexpected; `request_id` matches the server log line |

### 0.3 Pagination

**Offset pagination everywhere.** The reference UI has an explicit per-page selector and a total count in the status bar, both of which require a knowable `total` and random page access — keyset cursors provide neither. At the design scale (≤10k rows) `LIMIT/OFFSET` over a covering index is 1–3 ms.

Request: `limit` (default `100`, min `1`, max `500`), `offset` (default `0`, min `0`).

Every list response:
```json
{
  "items": [ … ],
  "page": { "limit": 100, "offset": 0, "total": 231, "returned": 100, "has_more": true },
  "meta": { "elapsed_ms": 7, "query_id": "…" }
}
```
`total` is `COUNT(*)` under the same filters. On a filtered set > 50,000 the server may return `"total": null, "total_is_estimate": true` — clients must handle `null`.

### 0.4 Sort / filter / group vocabulary (shared by all list endpoints)

**`sort`** — `<field>` or `-<field>` for descending. Comma-separated for tiebreakers. Invalid field → `VALIDATION_ERROR`.

| Scope | Allowed `sort` fields |
|---|---|
| models | `name` `created` `modified` `size` `category` `base_model` `role` `params` `rating` `hash_state` `relevance` |
| node_packages | `name` `author` `classes` `updated` `size` `relevance` |
| node_classes | `name` `display_name` `category` `package` `relevance` |
| workflows | `name` `modified` `size` `nodes` `missing` `relevance` |
| outputs | `created` `modified` `name` `size` `rating` `width` `height` `duration` `relevance` |

Default sorts: models `name`, node_packages `name`, node_classes `display_name`, workflows `-modified`, outputs `-created`.
`relevance` is only valid when `q` is present; otherwise `VALIDATION_ERROR`.
**Every sort implicitly appends `,id` for determinism.**

**`group`** — returns `groups[]` alongside `items[]` and orders items by group.

| Scope | Allowed `group` values |
|---|---|
| models | `none` `category` `base_model` `role` `folder` `precision` `root` `hash_state` `integrity` `first_letter` `date` |
| node_packages | `none` `author` `official` `enabled` `update_state` |
| node_classes | `none` `category` `package` |
| workflows | `none` `folder` `base_model` `runnable` `date` |
| outputs | `none` `folder` `date` `model` `media_kind` `album` `first_letter` |

`date` buckets: `Today`, `Yesterday`, `This week`, `This month`, `<Month YYYY>`, `Older`.

**Filters** (repeatable ⇒ OR within a field, AND across fields):

| Param | Type | Applies to |
|---|---|---|
| `q` | string | all — see §7 |
| `smart` | bool (default `false`) | all — hybrid vs lexical |
| `category` | string, repeatable | models |
| `base_model` | string, repeatable | models, workflows |
| `role` | string, repeatable | models |
| `modality` | string, repeatable | models |
| `precision` | string, repeatable | models |
| `hash_state` | enum, repeatable | models |
| `integrity` | enum, repeatable | models |
| `root_id` | int, repeatable | all |
| `folder` | string (prefix match) | models, workflows, outputs |
| `album_id` | int | all |
| `tag` | string, repeatable | all |
| `favorite` | bool | models, outputs |
| `min_rating` | 0–5 | models, outputs |
| `has_update` | bool | models, node_packages |
| `is_adapter` | bool | models |
| `official` | bool | node_packages |
| `enabled` | bool | node_packages |
| `media_kind` | enum, repeatable | outputs |
| `model_id` | int | outputs, workflows |
| `workflow_id` | int | outputs |
| `node_class` | string | workflows |
| `runnable` | bool | workflows |
| `missing_only` | bool | workflows (missing deps) |
| `size_min` / `size_max` | int bytes | all |
| `date_from` / `date_to` | epoch ms | all |
| `include_missing` | bool (default `false`) | all |
| `fields` | csv | all — sparse fieldsets |

### 0.5 Aggregate freshness

Every count, total, facet, byte figure and free-space number this API serves reflects the **last
committed write**, in the same process, with no restart and no cache-busting query parameter. A
client may read `/system/stats` before a mutation and immediately after it and see the delta.

Two mechanisms hold that, neither of which asks a mutation endpoint to remember anything:

* **Readers never hold a snapshot.** Read-only connections run in autocommit and are rolled back on
  acquisition, so no request can pin a WAL read snapshot that outlives it. (This was a real defect:
  a `CREATE TEMP TABLE` write on the read-only connection opened an implicit transaction that was
  never committed, and every later aggregate on that worker thread froze at the moment the Storage
  tab was first opened, until the process restarted.)
* **Derived caches key on `PRAGMA data_version`,** which SQLite bumps on every committed write from
  every path — rename, move, trash, restore, permanent delete, tag and album edits, and scan
  completion alike. The server-side footprint walk is the only cached figure, and a mutation
  invalidates it immediately.

**Free space is never cached at all** — `/storage/summary` re-probes every volume on every read.


---

## 1. System & config

### `GET /api/v1/system/info`
`200` — never fails, no config required.
```json
{ "app":"Geekatplay ComfyUI Asset Vault","version":"2.0.0",
  "author":"Geekatplay — Vladimir Chopine","api_version":1,"schema_version":2,
  "python":"3.12.10","platform":"win32",
  "features":{"smart_search":true,"civitai":true,"ollama":false,"mcp":true,"video_thumbnails":false} }
```

### `GET /api/v1/system/config`
`200`
```json
{ "comfyui_path":"O:\\ComfyUI","path_exists":true,"is_configured":true,
  "auto_reindex":true,"watch_enabled":false,
  "online_enabled":true,"civitai_enabled":true,"civitai_api_key_set":false,
  "ollama_enabled":false,"ollama_url":"http://localhost:11434","ollama_model":"llama3",
  "smart_search_enabled":true,
  "hash_concurrency":2,"hash_throttle_mbps":0,
  "thumb_cache_max_mb":2048,"thumb_video_ffmpeg":false,
  "page_size_default":100,"trash_mode":"trash","trash_retention_days":30,
  "read_held_extra_paths":false,
  "mcp_read_only":false,
  "extra_workflow_dirs":[],
  "roots":[{"id":1,"kind":"comfyui","path":"O:\\ComfyUI","label":"ComfyUI","available":true,"is_default":true,"source":"config"}] }
```
`civitai_api_key` is **never** returned — only `civitai_api_key_set`.
`mcp_read_only` switches the MCP server back to a read-only tool surface (DECISIONS C5 rail 6). Default **false** — full file-operation access is the shipped default; when `true`, every mutating MCP tool refuses and reads keep working.

### `PATCH /api/v1/system/config`
Body: any subset of the writable keys above. `200` returns the full config.
Changing `comfyui_path` re-resolves roots and returns `"roots_changed": true`; it does **not** auto-start a scan.
Errors: `VALIDATION_ERROR`, `PATH_INVALID`, `PATH_NOT_ALLOWED`.

### `POST /api/v1/system/validate-path`
Body `{"path":"O:\\ComfyUI"}` — the wizard's live preview.
`200`
```json
{ "valid":true,"normalized":"O:\\ComfyUI","exists":true,"is_comfyui_root":true,
  "signals":{"has_models":true,"has_custom_nodes":true,"has_output":true,
             "has_input":true,"has_user_workflows":true,"has_root_workflows":true,
             "has_main_py":true,"comfyui_version":"0.33.0"},
  "extra_model_paths":{"present":false,"held_present":true,"roots":[]},
  "preview":{"model_files":231,"model_bytes":1649267441664,"custom_node_packages":35,
             "workflows":48,"outputs":3569,"inputs":223},
  "warnings":["extra_model_paths.yaml not found (extra_model_paths.yaml.hold exists but is not loaded)"] }
```
`preview` is a bounded walk (hard cap 60,000 entries / 4 s) — cheap enough for a live wizard field (measured full walk ≈ 0.4 s).
`valid:false` still returns `200` with `reason` — this is a validation preview, not an error.

### `POST /api/v1/system/wizard/complete`
Body:
```json
{ "comfyui_path":"O:\\ComfyUI","online_enabled":true,"auto_reindex":true,
  "smart_search_enabled":false,"ollama_enabled":false,
  "ollama_url":"http://localhost:11434","ollama_model":"llama3",
  "start_scan":true }
```
`202`
```json
{ "is_configured":true, "job_id":1, "scan_started":true }
```
**The scan is never awaited inside the request** (the current build blocks the wizard for the entire scan). The client subscribes to `/index/stream`.

### `GET /api/v1/system/stats`
`200` — feeds the status bar and the dashboard.
```json
{ "models":231,"model_files":231,"models_bytes":1649267441664,"models_hashed":0,
  "node_packages":36,"node_classes":1581,"official_node_classes":638,
  "workflows":48,"workflows_broken":6,
  "outputs":3569,"outputs_bytes":18327364821,
  "inputs":223,"embedded":0,"integrity_issues":1,
  "by_category":[{"category":"diffusion_models","count":61,"bytes":812345678901},
                 {"category":"loras","count":59,"bytes":12345678901}],
  "by_base_model":[{"base_model":"FLUX.1","count":34},{"base_model":"WAN","count":22}],
  "last_scan":{"job_id":17,"finished_at":1766000000000,"duration_ms":18420,"errors":3} }
```

### `GET /api/v1/system/health`
`200` — the Health drawer.
```json
{ "status":"degraded",
  "checks":[
    {"id":"comfyui_root","status":"ok","message":"O:\\ComfyUI"},
    {"id":"database","status":"ok","message":"WAL, 14.2 MB"},
    {"id":"embeddings","status":"warn","message":"Model not installed","action":"POST /api/v1/embeddings/enable"},
    {"id":"civitai","status":"ok"},
    {"id":"ollama","status":"warn","message":"Not reachable at http://localhost:11434"},
    {"id":"integrity","status":"error","message":"1 model file is not a valid model","count":1,
     "items":[{"uid":"model:118","name":"flux2-vae-new.safetensors","reason":"not_a_model"}]},
    {"id":"partial_downloads","status":"warn","count":1,
     "items":[{"path":"O:\\ComfyUI\\models\\checkpoints\\Unconfirmed 46066.crdownload"}]},
    {"id":"suspect_remotes","status":"warn","count":1,
     "items":[{"package":"was-ns","repo_url":"https://github.com/Comfy-Org/ComfyUI"}]},
    {"id":"thumb_cache","status":"ok","message":"412 MB / 2048 MB"}
  ] }
```

### `GET /api/v1/system/roots` · `POST /api/v1/system/roots` · `DELETE /api/v1/system/roots/{id}`
List / add / remove extra scan roots (`kind` ∈ `extra_models`, `extra_workflows`). `POST` body `{"path": "...", "kind": "...", "label": "..."}`.

### `POST /api/v1/system/thumbs/gc`
Body `{"max_mb": 2048}` → `200 {"deleted":1204,"freed_bytes":318000000,"remaining_bytes":220000000}`.

### `POST /api/v1/system/ollama/test`
Body `{"url":"…"}` → `200 {"available":true,"models":["llama3:latest"],"latency_ms":41}` or `200 {"available":false,"reason":"connection refused"}`. **Never a 5xx** — unavailability is a normal state.

---

## 2. Indexing

### `POST /api/v1/index/start`
```json
{ "mode":"incremental",           // "full" | "incremental" | "targeted"
  "phases":null,                   // null = all; or ["models","outputs"]
  "root_ids":null,
  "force":false,                   // ignore fingerprints
  "enrich_online":true }
```
`202 {"job_id":18,"mode":"incremental","started_at":1766000000000}`
`409 JOB_ALREADY_RUNNING` with `details.job_id` if one is active.
`409 NOT_CONFIGURED` if the wizard has not run.

### `POST /api/v1/index/cancel`
Body `{"job_id":18}` (optional; defaults to the active job) → `202 {"job_id":18,"status":"cancelling"}`.

### `GET /api/v1/index/status`
```json
{ "active":true,
  "job":{ "id":18,"mode":"incremental","status":"running","trigger":"user",
          "phase":"outputs","phase_index":6,"phase_count":9,
          "items_done":1840,"items_total":3569,"items_skipped":1200,
          "error_count":2,"rate_per_sec":742.1,"eta_ms":2330,
          "current":"output\\seedance_i2v\\v_00071_.mp4",
          "started_at":1766000000000,"elapsed_ms":14200 },
  "last_completed":{ "id":17,"finished_at":1765999000000,"duration_ms":18420,
                     "stats":{"models":{"scanned":231,"skipped":0,"errors":1},
                              "node_packages":{"scanned":36,"skipped":0,"errors":0},
                              "node_classes":{"created":1581},
                              "workflows":{"scanned":48,"skipped":0,"errors":0},
                              "outputs":{"scanned":3569,"skipped":0,"errors":2},
                              "pruned":0} } }
```

### `GET /api/v1/index/stream`  (SSE)
`text/event-stream`. Events `phase`, `progress`, `item`, `error`, `done`, `heartbeat`, `overflow` — payloads in ARCHITECTURE §2.3. Query `?job_id=` optional. If no job is active the stream stays open and emits heartbeats until one starts.

### `GET /api/v1/index/jobs`  ·  `GET /api/v1/index/errors`
`/jobs` — paginated job history.
`/errors?job_id=&code=&kind=&limit=&offset=` →
```json
{"items":[{"id":9,"job_id":18,"phase":"outputs","kind":"output",
           "path":"O:\\ComfyUI\\output\\3d\\bad.png","code":"IMAGE_UNREADABLE",
           "message":"cannot identify image file","created_at":1766000000000}],
 "page":{…}, "summary":{"IMAGE_UNREADABLE":2,"HEADER_INVALID":1}}
```

---

## 3. Models

### `GET /api/v1/models`
Query: §0.3 pagination + §0.4 sort/group/filters.

`200`
```json
{ "items":[
   { "uid":"model:41","id":41,
     "name":"flux1-dev-fp8","filename":"flux1-dev-fp8.safetensors","ext":".safetensors",
     "category":"checkpoints","role":"checkpoint",
     "base_model":{"family":"FLUX.1","variant":null,"confidence":0.95,"source":"metadata"},
     "modality":"image","architecture":"FLUX Transformer (dual-stream, bundled)",
     "precision":"fp8","quantization":null,
     "params":{"primary":11901408320,"total":16872349411,"display":"11.9B"},
     "is_bundled":true,"is_adapter":false,
     "size":17246978048,"modified_at":1740000000000,
     "folder":"","root_id":1,"rel_path":"checkpoints\\flux1-dev-fp8.safetensors",
     "abs_path":"O:\\ComfyUI\\models\\checkpoints\\flux1-dev-fp8.safetensors",
     "hash":{"state":"unhashed","autov2":null,"sha256":null},
     "integrity":"ok",
     "civitai":{"state":"none","model_id":null,"url":null,"has_update":false},
     "thumbnail_url":"/api/v1/files/thumbnail?uid=model:41&size=320",
     "favorite":false,"user_rating":null,"tags":[],
     "counts":{"workflows":3,"outputs":128},
     "missing":false }
  ],
  "page":{"limit":100,"offset":0,"total":231,"returned":100,"has_more":true},
  "groups":[{"key":"checkpoints","label":"Checkpoints","count":21,"bytes":181234567890,"offset":0}],
  "meta":{"elapsed_ms":6,"sort":"name,id","smart_available":true,"mode":"lexical"} }
```
Notes: `params.display` is server-formatted so every surface agrees. `thumbnail_url` is always present (placeholder generation guarantees a 200). `abs_path` is display-only.

### `GET /api/v1/models/facets`
```json
{ "category":[{"value":"diffusion_models","label":"Diffusion Models","count":61},…],
  "base_model":[{"value":"FLUX.1","count":34},…],
  "role":[…], "precision":[…], "modality":[…],
  "hash_state":[{"value":"unhashed","count":231},{"value":"done","count":0}],
  "integrity":[{"value":"ok","count":230},{"value":"not_a_model","count":1}],
  "root":[{"value":1,"label":"ComfyUI","count":231}],
  "tags":[…],
  "size":{"min":1024,"max":24696061952,"total":1649267441664},
  "date":{"min":1690000000000,"max":1766000000000} }
```
Honours all active filters *except* the facet's own field (standard faceted-search semantics). One query per facet over its covering index; budget ≤ 30 ms total.

### `GET /api/v1/models/groups?group=folder`
The left-rail tree.
```json
{ "group":"folder",
  "nodes":[ {"key":"checkpoints","label":"Checkpoints","count":21,"bytes":181234567890,
             "children":[{"key":"checkpoints/sdxl","label":"sdxl","count":4,"bytes":26000000000,"children":[]}]} ] }
```

### `GET /api/v1/models/{id}`
The DETAILS panel + deep detail. `404 NOT_FOUND`.
```json
{ "uid":"model:41","id":41, "...all list fields...",
  "technical":{
    "tensor_count":1442,"format":"safetensors","header_parsed":true,
    "components":[{"name":"unet","params":11901408320,"dtype":"F8_E4M3","share":0.705},
                  {"name":"text_encoder","params":4887121408,"dtype":"F16","share":0.290},
                  {"name":"vae","params":83819683,"dtype":"F16","share":0.005}],
    "prediction_type":"flow","resolution_hint":"1024x1024",
    "detection":{"source":"metadata","confidence":0.95,
                 "signals":["modelspec.architecture=flux-1-dev/normal",
                            "prefixes: model,text_encoders,vae"]},
    "header_metadata":{"modelspec.architecture":"flux-1-dev/normal","modelspec.title":"FLUX.1 [dev]"}
  },
  "build_spec":{ "trained_by":null,"training_steps":null,"dataset_notes":null,
                 "adapter":null, "license":"flux-1-dev-non-commercial-license" },
  "files":[{"id":41,"abs_path":"…","size":17246978048,"modified_at":1740000000000,
            "hash_state":"unhashed","autov2":null,"root_id":1}],
  "civitai":{ "state":"none","reason":"not_hashed",
              "hint":"Compute the SHA-256 hash to enable Civitai matching." },
  "update":{ "has_update":false,"latest_version_name":null,"benefits":null,"checked_at":null },
  "description":{"text":null,"source":null},
  "usage_notes":null,
  "trigger_words":[],
  "recommended_settings":null,
  "download":{"url":null,"source":null},
  "usage":{"workflow_count":3,"output_count":128,
           "top_workflows":[{"uid":"workflow:12","name":"flux_basic","occurrences":2}]},
  "tags":[],"user_notes":null,"user_rating":null,"favorite":false,
  "integrity":{"status":"ok","note":null},
  "actions":{"can_hash":true,"can_rename":true,"can_move":true,"can_delete":true,
             "can_refresh_metadata":false,"refresh_blocked_reason":"hash_required"} }
```
The `actions` block is authoritative — the UI **enables buttons from it** rather than re-deriving the rules.

### `GET /api/v1/models/{id}/usage`
```json
{ "workflows":[{"uid":"workflow:12","name":"flux_basic","rel_path":"flux_basic.json",
                "occurrences":2,"via":[{"class":"CheckpointLoaderSimple","input":"ckpt_name"}],
                "match_method":"exact_relpath"}],
  "outputs":{"count":128,"recent":[{"uid":"output:930","filename":"Anima_00021_.png",
             "created_at":1765000000000,"thumbnail_url":"/api/v1/files/thumbnail?uid=output:930&size=160"}]},
  "page":{…} }
```

### `POST /api/v1/models/{id}/refresh-metadata`
Body `{"force":false}`. Requires `hash_state='done'` and `online_enabled`.
`202 {"state":"pending"}` · `409 {"error":{"code":"CONFLICT","details":{"reason":"hash_required"}}}` · `503 FEATURE_UNAVAILABLE` when offline.

### `PATCH /api/v1/models/{id}`
Body (any subset): `{"favorite":true,"user_rating":4,"user_notes":"…","tags":["portrait"],"album_id":3,"color_label":"amber"}` → `200` full detail.

### `POST /api/v1/models/bulk`
`{"uids":["model:1","model:2"],"patch":{"favorite":true}}` → `200 {"updated":2,"results":[{"uid":"model:1","ok":true}]}`.

---

## 4. Nodes

### `GET /api/v1/node-packages`
Filters: `q`, `official`, `enabled`, `has_update`, `author`, `tag`, `update_state`.
```json
{ "items":[
   { "uid":"node_package:7","id":7,"folder_name":"ComfyUI-KJNodes",
     "display_name":"ComfyUI-KJNodes","author":"kijai","publisher_id":"kijai",
     "description":"Various quality of life -nodes for ComfyUI…",
     "is_official":false,"enabled":true,"is_single_file":false,
     "class_count":195,
     "extraction":{"status":"ok","strategies":["S5"],"confidence":"inferred"},
     "repo":{"url":"https://github.com/kijai/ComfyUI-KJNodes","suspect":false,
             "branch":"main","commit":"450dc91","commit_at":1748100000000},
     "update":{"state":"none","has_update":false,"commits_behind":null,"checked_at":null},
     "version":"1.4.0","deps":{"count":3,"satisfied":null,"missing":[]},
     "size":48213445,"file_count":312,
     "counts":{"workflows":9},
     "thumbnail_url":"/api/v1/files/thumbnail?uid=node_package:7&size=160" } ],
  "page":{…},"groups":[…],"meta":{…} }
```

### `GET /api/v1/node-packages/{id}`
Adds: `long_description` (README), `license`, `homepage_url`, `python_deps` (full list), `has_web_directory`, `abs_path`, `class_categories` (histogram), `top_classes`, `extraction.notes`, `actions`.
For `__comfyui_core__` (`is_official:true`) it reports `comfyui_version`, `class_count: 638`, and `source_breakdown: {"nodes.py":65,"comfy_extras (legacy)":120,"comfy_extras (V3 schema)":453}`.

### `GET /api/v1/node-packages/{id}/classes`
Paginated `node_classes` scoped to the package. Same shape as `/node-classes`.

### `GET /api/v1/node-classes`
Filters: `q`, `package_id`, `category`, `official`, `deprecated`, `experimental`, `confidence`.
```json
{ "items":[
   { "uid":"node_class:9182","id":9182,"node_id":"ImageCrop",
     "display_name":"Crop Image (DEPRECATED)","class_name":"ImageCrop",
     "category":"image/transform","description":null,
     "package":{"uid":"node_package:1","name":"ComfyUI Core","official":true},
     "inputs":{"required":{"image":"IMAGE","width":"INT","height":"INT","x":"INT","y":"INT"},"optional":{}},
     "outputs":{"types":["IMAGE"],"names":null},"output_node":false,
     "flags":{"deprecated":true,"experimental":false,"api_node":false},
     "confidence":"declared","registration":"python",
     "source":{"strategy":"S4","file":"comfy_extras\\nodes_images.py","lineno":29},
     "counts":{"workflows":4} } ],
  "page":{…},"groups":[…] }
```
`registration` says who registers the class at runtime: `python` (a class in the package's own source, or the ComfyUI-Manager registry), `javascript` (the package's shipped `web/**/*.js`, strategy `S7` — `ComfyUI-KJNodes` registers `GetNode` and `SetNode` that way and defines neither in Python), or `frontend` (the web client itself: `Note`, `MarkdownNote`, `Reroute`, `PrimitiveNode`). The last two have no Python definition in any install, so no package can supply them and none is ever offered.

### `GET /api/v1/node-classes/{id}`
Adds `workflows_using` (top 20) and the raw `input_types_json`.

### `POST /api/v1/node-packages/{id}/check-update`
`202 {"state":"pending"}` · `503 FEATURE_UNAVAILABLE` (offline) · `200 {"state":"suspect_remote","reason":"remote does not match folder"}` for `was-ns`-class cases · `429 RATE_LIMITED` with `details.retry_after_ms`.

### `POST /api/v1/node-packages/check-updates`
Body `{"ids":[…] | null}` → `202 {"job_id":"upd-9","queued":25}`. Progress via `GET /api/v1/node-packages/update-status`.

---

## 5. Workflows

### `GET /api/v1/workflows`
Filters: `q`, `folder`, `base_model`, `runnable`, `missing_only`, `node_class`, `model_id`, `root_id`, date/size.
```json
{ "items":[
   { "uid":"workflow:12","id":12,"name":"wan22_animate_mix_character_replacement",
     "rel_path":"wan22_animate_mix_character_replacement_rtx3090_848x480.json",
     "folder":"","root_id":1,"source":"file","format":"ui",
     "title":null,"description":"Replaces a character in a driving video using WAN 2.2 Animate…",
     "description_source":"derived",
     "capability_tags":["video","character-replacement","controlnet"],
     "base_model":"WAN","modality":"video",
     "counts":{"nodes":84,"links":132,"groups":6,"missing_nodes":0,"missing_models":2},
     "is_runnable":false,"has_subgraphs":true,"subgraph_count":8,
     "prompt_summary":"a cowboy walking through a desert town at golden hour",
     "size":184321,"modified_at":1764000000000,
     "thumbnail_url":"/api/v1/files/thumbnail?uid=workflow:12&size=320",
     "counts_outputs":31 } ],
  "page":{…},"groups":[…] }
```

### `GET /api/v1/workflows/{id}`
Adds `node_breakdown` (class → count, resolved, package), `positive_prompt`, `negative_prompt`, `unresolved_inputs`, `graph_available`, `graph_truncated`, `outputs_recent`, `actions`.

### `GET /api/v1/workflows/{id}/graph`
`200` the raw graph JSON (`ui`, `api`, or both). `?format=ui|api|raw` (default `raw`). `413 PAYLOAD_TOO_LARGE` above 32 MB with a `download_url` instead.

### `GET /api/v1/workflows/{id}/dependencies`
```json
{ "summary":{"total":14,"satisfied":12,"missing":2,"ambiguous":0},
  "models":[
    {"ref_name":"wan2.2_i2v_high_noise.safetensors","category":"diffusion_models",
     "via":[{"class":"UNETLoader","input":"unet_name"}],"occurrences":1,
     "status":"missing","match_method":"none",
     "uid":null,"suggestions":[{"uid":"model:88","name":"wan2.2_i2v_low_noise.safetensors","score":0.82}]},
    {"ref_name":"umt5_xxl_fp8_e4m3fn_scaled.safetensors","category":"text_encoders",
     "status":"satisfied","match_method":"basename","uid":"model:143"}],
  "nodes":[
    {"class_type":"WanVideoSampler","status":"satisfied","uid":"node_class:1044",
     "provided_by":"python",
     "package":{"uid":"node_package:9","name":"ComfyUI-WanVideoWrapper"}},
    {"class_type":"MarkdownNote","status":"satisfied","uid":"node_class:1869",
     "provided_by":"frontend",
     "package":{"uid":"node_package:34","name":"ComfyUI Core"}},
    {"class_type":"SomeUnknownNode","status":"missing","uid":null,
     "registry_hint":{"package":"ComfyUI-Foo","repo_url":"https://github.com/x/ComfyUI-Foo"}}],
  "embeddings":[],"input_files":[] }
```
`provided_by` mirrors `node_classes.registration` and is present on every satisfied node: `frontend` and `javascript` mean the web client supplies the type and there is nothing to install. Nodes that instantiate a subgraph the workflow itself declares are not dependencies at all — they never appear in this list, and `subgraph_count` on the workflow item reports how many definitions the file carries.

`registry_hint` comes from the ComfyUI-Manager `extension-node-map.json` (5,590 repos) — it tells the user *which package to install*, which is the whole point of the missing-node report.

---

## 6. Outputs

### `GET /api/v1/outputs`
Filters: `q`, `folder`, `media_kind`, `model_id`, `workflow_id`, `album_id`, `favorite`, `min_rating`, `tag`, date/size, `has_metadata`, plus generation-metadata filters `sampler`, `seed`, `steps_min`/`steps_max`, `cfg_min`/`cfg_max`, `width_min`/`width_max`, `height_min`/`height_max`.
```json
{ "items":[
   { "uid":"output:930","id":930,"filename":"Anima_00021_.png","ext":".png",
     "media_kind":"image","mime":"image/png",
     "width":1024,"height":1024,"duration_ms":null,
     "size":1843211,"created_at":1765000000000,"modified_at":1765000000000,
     "folder":"","rel_path":"Anima_00021_.png","root_id":1,
     "has_metadata":true,"metadata_format":"comfy_prompt",
     "positive_prompt":"anime girl standing in rain, cinematic",
     "model_name":"flux1-dev-fp8.safetensors","model_uid":"model:41",
     "workflow_uid":"workflow:12",
     "seed":"842119274","steps":20,"cfg":3.5,"sampler":"euler","scheduler":"simple",
     "favorite":false,"user_rating":null,"album_id":null,"tags":[],
     "thumbnail_url":"/api/v1/files/thumbnail?uid=output:930&size=320",
     "raw_url":"/api/v1/files/raw?uid=output:930",
     "download_url":"/api/v1/files/download?uid=output:930" } ],
  "page":{…},"groups":[…] }
```

### `GET /api/v1/outputs/{id}`
Adds `negative_prompt`, `denoise`, `provenance`, `node_count`, `loras[]` (name + strength), `all_models[]`, `workflow_hash`, `siblings` (same `workflow_hash`), `exif`, `actions`.

`provenance` makes the B1 fix visible to the user:
```json
"provenance":{ "positive_prompt":{"origin":"link","source_node_id":"88:97","source_class_type":"PrimitiveStringMultiline","resolved":true},
               "negative_prompt":{"origin":"unresolved","reason":"link_to_non_value_node"},
               "seed":{"origin":"literal"} }
```
The details panel renders unresolved fields as `—` with a tooltip, instead of showing `['88:97', 0]` or crashing.

### `GET /api/v1/outputs/{id}/graph`
The embedded prompt/workflow graph, same semantics as `/workflows/{id}/graph`. `404` when the output carries no metadata (~13% of this install).

### `POST /api/v1/outputs/{id}/extract-workflow`
Saves the embedded graph as a real `.json` under a chosen workflow root and indexes it.
Body `{"root_id":1,"folder":"extracted","name":"anima_flux"}` → `201 {"uid":"workflow:57","abs_path":"…"}` · `409 CONFLICT` if the name exists.

### `PATCH /api/v1/outputs/{id}`  ·  `POST /api/v1/outputs/bulk`
Same shape as models (`favorite`, `user_rating`, `user_notes`, `tags`, `album_id`, `color_label`).

---

## 7. Search

### `GET /api/v1/search`
| Param | Type | Default | Notes |
|---|---|---|---|
| `q` | string, required, 1–512 | — | |
| `smart` | bool | `false` | ON → hybrid |
| `kinds` | csv | all | `model,node_package,node_class,workflow,output` |
| `limit` / `offset` | int | 50 / 0 | max 200 |
| `per_kind_limit` | int | `null` | when set, returns balanced results per kind |
| `raw` | bool | `false` | pass the FTS expression verbatim (advanced) |
| plus every scope filter from §0.4 | | | |

`200`
```json
{ "query":"flux lora for anime",
  "mode":"hybrid", "smart_available":true, "smart_reason":null,
  "items":[
    { "uid":"model:112","kind":"model","score":0.0327,
      "title":"anime_flux_v3","subtitle":"LoRA · FLUX.1 · rank 32",
      "snippet":"…<mark>anime</mark> style adapter trained on…",
      "thumbnail_url":"/api/v1/files/thumbnail?uid=model:112&size=160",
      "matched":["title","semantic"],
      "ranks":{"lexical":2,"vector":1},
      "entity":{ /* the same object the list endpoint returns */ } } ],
  "facets":{"kind":[{"value":"model","count":12},{"value":"output","count":31}]},
  "page":{"limit":50,"offset":0,"total":43,"returned":43,"has_more":false},
  "meta":{"elapsed_ms":38,"lexical_ms":9,"vector_ms":6,"fusion_ms":3} }
```
When Smart is requested but unavailable:
```json
{ "mode":"lexical","smart_available":false,
  "smart_reason":"embedding_model_not_installed", … }
```
**`200`, not an error.** `smart_reason` ∈ `embedding_model_not_installed | embedding_model_downloading | onnxruntime_missing | disabled_by_user | index_building`.

### `GET /api/v1/search/suggest?q=flu&limit=8`
Prefix-index-served type-ahead; budget ≤ 15 ms.
```json
{ "suggestions":[{"text":"flux1-dev-fp8","kind":"model","uid":"model:41"},
                 {"text":"FLUX.1","kind":"facet","field":"base_model","count":34}] }
```

### `GET /api/v1/search/status`
```json
{ "lexical":{"available":true,"documents":4238,"last_built_at":1766000000000},
  "semantic":{"available":false,"state":"not_installed",
              "model_id":"all-MiniLM-L6-v2-int8","dim":384,
              "embedded":0,"pending":4238,"reason":"embedding_model_not_installed"} }
```

### `POST /api/v1/search/rebuild`
Body `{"lexical":true,"semantic":true}` → `202 {"job_id":"search-3"}`. Drops and repopulates the derived indexes.

---

## 8. Embeddings

### `GET /api/v1/embeddings/status`
```json
{ "state":"not_installed",
  "model_id":"all-MiniLM-L6-v2-int8","dim":384,
  "install_dir":"backend/data/models/all-MiniLM-L6-v2",
  "download":{"bytes_done":0,"bytes_total":24117248,"percent":0},
  "index":{"embedded":0,"pending":4238,"stale":0,"last_built_at":null},
  "reason":"embedding_model_not_installed",
  "onnxruntime":{"installed":true,"version":"1.19.2","providers":["CPUExecutionProvider"]} }
```

### `POST /api/v1/embeddings/enable`
Body `{"source":"auto"}` (`auto` = download from `embedding_model_url`; `local` = expect files already present).
`202 {"state":"downloading","bytes_total":24117248}`
`503 FEATURE_UNAVAILABLE` with `details.reason = "onnxruntime_missing"` when the wheel is absent.
`502 UPSTREAM_UNAVAILABLE` (`retryable: true`) when the CDN is unreachable — the UI shows the manual-placement instructions.

### `POST /api/v1/embeddings/disable`
`200 {"state":"disabled"}`. Vectors are retained (re-enabling is instant); `?purge=true` deletes them.

### `POST /api/v1/embeddings/rebuild`
Body `{"kinds":null,"force":false}` → `202 {"job_id":"embed-4","pending":4238}`.

### `GET /api/v1/embeddings/stream` (SSE)
`event: embed_progress data: {"done":1200,"total":4238,"rate":118.4,"eta_ms":25700}` · `event: done`.

---

## 9. Hashing

### `POST /api/v1/hash/enqueue`
```json
{ "scope":"category",          // "all" | "category" | "folder" | "ids" | "unhashed"
  "category":"loras",           // when scope=category
  "folder":null,                // when scope=folder (rel path within a root)
  "root_id":null,
  "uids":null,                  // when scope=ids
  "priority":5,
  "skip_hashed":true }
```
`202`
```json
{ "batch_id":"hash-2026-08-22-a1b2","queued":59,"skipped":0,
  "bytes_total":42318452224,"eta_ms":352654000 }
```
`eta_ms` uses the measured rolling throughput (falls back to 120 MB/s). The UI **must** show this before a full-vault run (≈2.8 h for 1.5 TB).

### `POST /api/v1/hash/cancel`
Body `{"batch_id":null,"uids":null}` — omit both to cancel everything. → `202 {"cancelled":57,"running_stopped":1}`.

### `GET /api/v1/hash/status`
```json
{ "active":true,"concurrency":2,"throttle_mbps":0,
  "queue":{"queued":57,"running":2,"done":118,"failed":1,"cancelled":0},
  "bytes":{"done":18327364821,"total":42318452224,"percent":43.3},
  "throughput_mbps":142.7,"eta_ms":168200000,
  "running":[{"uid":"model:88","filename":"wan2.2_i2v_high_noise.safetensors",
              "size":16106127360,"bytes_done":4831838208,"percent":30.0,"mbps":141.2}],
  "recent_failures":[{"uid":"model:14","filename":"ltx-2.3-22b-dev.safetensors",
                      "code":"FILE_LOCKED","message":"The process cannot access the file",
                      "attempts":2,"will_retry":true}] }
```

### `GET /api/v1/hash/stream` (SSE)
`event: hash_progress` (≤4 Hz), `event: hash_item` (`{"uid":"model:41","state":"done","autov2":"5628B30A8E"}`), `event: done`.

### `POST /api/v1/hash/settings`
Body `{"concurrency":2,"throttle_mbps":100}` → `200`. Takes effect on the next chunk, no restart.

---

## 10. Files & media

All three take **`uid`**, never a path.

### `GET /api/v1/files/thumbnail?uid=&size=`
`size` ∈ `160|320|640` (default `320`). Any other value → nearest allowed tier (no error).
`200 image/webp` · headers `ETag: "<fingerprint>-<size>"`, `Cache-Control: public, max-age=31536000, immutable`, `Last-Modified`, `X-Thumb-Source: cache|generated|placeholder`.
`304` on `If-None-Match`.
`404 NOT_FOUND` (unknown uid) · `410 FILE_MISSING` · `403 PATH_NOT_ALLOWED`.
Never 500 for an undecodable source — falls back to a generated placeholder.

### `GET /api/v1/files/raw?uid=`
Full-resolution stream for the lightbox and `<video>`.
`200` / `206 Partial Content` — **`Accept-Ranges: bytes` and full `Range` support are mandatory** (MP4 seeking in `<video>` requires it; 451 videos here).
`Content-Type` from `outputs.mime`. `Cache-Control: private, max-age=3600`. `X-Content-Type-Options: nosniff`.
`Content-Disposition: inline`.

### `GET /api/v1/files/text?uid=&max_bytes=`
A capped, decoded excerpt for previewing `.txt`, `.json` and anything else that
reads as text. `max_bytes` is clamped to 512 KB regardless of what is asked for.

Whether a file counts as text is decided **from its bytes, not its extension** —
a ComfyUI output folder holds `.pt` tensor files that are PyTorch pickles, some
of them hundreds of megabytes, and those must not be rendered as characters.
Nothing is unpickled, parsed or executed; JSON is validated only so the UI can
indent it.

`200` one of:
```json
{ "kind":"text", "text":"…", "encoding":"utf-8", "json":true,
  "truncated":false, "bytes_read":48586, "total_bytes":48586, "lines":1648,
  "uid":"output:903", "filename":"project.json" }
```
```json
{ "kind":"binary", "total_bytes":222955084,
  "message":"This is a binary file, so there is nothing to show as text." }
```
`404 NOT_FOUND` (unknown uid, or the file cannot be read) · `403 PATH_NOT_ALLOWED`.

### `POST /api/v1/files/thumbnail?uid=`
Store a poster frame the **browser** rendered for a 3D model.
Body `{"png":"data:image/png;base64,…"}`. Requires `X-Vault-Request`.

There is no server-side GL stack, and adding one to draw a `.glb` would be a
large dependency for a picture. The browser has already loaded the model in
order to show it, so it hands one frame back and every later view is an
ordinary cached thumbnail. Accepted **only** for `model3d` assets, only as a
PNG data URL, and only under 4 MB; the image is re-encoded to WebP server-side
rather than trusted as it arrived, and written to all three size tiers.

`200 {"ok":true,"uid":"output:3736","bytes":5070}`
`422 VALIDATION_ERROR` (not a 3D asset, not a PNG data URL, or over the cap) ·
`404 NOT_FOUND` · `403 PATH_NOT_ALLOWED` · `400 CSRF_HEADER_MISSING`.

Note: while a model has no stored poster, the thumbnail read endpoint above
serves a generated placeholder but does **not** cache it — otherwise a
placeholder fetched after the poster was stored would overwrite it.

### `GET /api/v1/files/download?uid=`
Same, but `Content-Disposition: attachment; filename*=UTF-8''<pct-encoded>` (RFC 5987 — required for the non-ASCII filenames present on this install).

### `GET /api/v1/files/reveal?uid=`
`POST`-like side effect but idempotent; opens Explorer at the file (`explorer /select,"<path>"`). `200 {"ok":true}` · `503 FEATURE_UNAVAILABLE` on non-Windows. Requires `X-Vault-Request`.

---

## 11. File operations

All require `X-Vault-Request: 1`. All accept a single `uid` or a `uids` array; array form returns per-item results and never aborts on the first failure.

### 11.0 Supported uid kinds — per-capability matrix

`fileops` accepts `model`, `workflow`, `output`, and `node_package`. Any other kind
(`node_class`, `album`, …) returns `VALIDATION_ERROR` with
`"Operations are not supported for '<kind>'."`

| uid kind | rename | move | delete (trash) | delete (permanent) | restore |
|---|:--:|:--:|:--:|:--:|:--:|
| `model` | yes | yes | yes | yes | yes |
| `workflow` | yes | yes | yes | yes | yes |
| `output` | yes | yes | yes | yes | yes |
| `node_package` | **no** | **no** | yes | yes | yes |

**Why node packages cannot be renamed or moved.** A `custom_nodes` folder is a
Python package that ComfyUI imports *by folder name*, and is usually a git
checkout that serves its own web assets from that path. Renaming or relocating it
silently changes which nodes load, breaks `WEB_DIRECTORY` asset URLs, and detaches
the checkout from its registry identity — with no way to detect the breakage until
ComfyUI is next started. Deletion is supported because it is trash-backed and
therefore reversible.

The refusal is explicit and displayable rather than a generic "unsupported kind":

```json
{ "error": { "code":"VALIDATION_ERROR",
             "message":"A node package cannot be renamed or moved: ComfyUI imports it by folder name, so changing the folder changes which nodes load and breaks the package's web assets and git checkout. Disable or delete it instead.",
             "details": { "kind":"node_package",
                          "reason":"node_package_immovable",
                          "allowed":["delete"] } } }
```

`GET /api/v1/node-packages/{id}` mirrors this in its `actions` block so the UI can
render the buttons disabled with a tooltip instead of discovering the refusal on
click:

```json
"actions": { "can_check_update":true, "can_delete":true,
             "can_rename":false, "can_move":false,
             "rename_blocked_reason":"A node package cannot be renamed or moved: …",
             "move_blocked_reason":"A node package cannot be renamed or moved: …" }
```

**Node-package deletion specifics.** The whole directory is moved into
`<root>/.vault-trash/` (a same-volume rename, so O(1) regardless of size), its
`node_classes` rows are captured in the trash payload and rebuilt verbatim on
restore, and `trash_items.size` is the package's **recursive** on-disk size —
including `.git`, `web/` and `node_modules/`, which the incremental-scan
fingerprint walk deliberately skips. `node_packages.total_size` uses the same
recursive figure so the Storage view accounts for packages correctly.

If the package looks like a git checkout with uncommitted changes, the successful
delete response carries a non-fatal `warning` naming example modified files; the
edits are preserved inside the trash copy.

```json
{ "uid":"node_package:7","ok":true,"mode":"trash","trash_id":12,
  "trash_path":"O:\ComfyUI\.vault-trash\20260822-141530-9ab3c1d0\ComfyUI-KJNodes",
  "warning":"This looks like a git checkout with uncommitted changes (nodes.py, util.py and more). Those edits are preserved in the trash copy." }
```

### 11.0.1 Search-index consistency

Every mutating file operation reindexes the affected uids **inside the same
transaction as the write**, so a committed row and its search document can never
disagree. This covers rename, move, trash, restore, permanent delete, tag
assignment, metadata patch and album membership; cascaded children (a package's
`node_classes`) are reindexed with their parent. Clients never need to trigger a
rescan to make a mutation searchable.

### `POST /api/v1/fileops/rename`
```json
{ "uid":"model:41","new_name":"flux1-dev-fp8-v2","keep_extension":true,"rename_sidecars":true }
```
`200`
```json
{ "ok":true,"uid":"model:41",
  "old_path":"O:\\ComfyUI\\models\\checkpoints\\flux1-dev-fp8.safetensors",
  "new_path":"O:\\ComfyUI\\models\\checkpoints\\flux1-dev-fp8-v2.safetensors",
  "sidecars_renamed":0,"db_updated":true,"thumbs_relocated":1 }
```
Errors: `PATH_INVALID` (illegal chars, reserved name, trailing dot/space, > 255), `CONFLICT` (`details.existing_path`), `FILE_LOCKED`, `FILE_MISSING`, `PATH_NOT_ALLOWED`.

### `POST /api/v1/fileops/move`
```json
{ "uids":["model:41","model:42"],"target_root_id":1,
  "target_folder":"checkpoints\\flux","create_missing":true,"on_conflict":"fail" }
```
`on_conflict` ∈ `fail | skip | rename` (`rename` → `name (2).ext`).
`200 {"ok":true,"moved":2,"skipped":0,"failed":0,"results":[{"uid":"model:41","ok":true,"new_path":"…"}]}`
`207`-style semantics are **not** used — always `200` with a `results[]` array; `ok:false` at the top level only if *every* item failed.
Cross-volume moves are performed as copy+verify+delete with progress on `/index/stream` (`event: fileop_progress`).

### `POST /api/v1/fileops/delete`
```json
{ "uids":["output:930"],"mode":"trash","confirm":true }
```
`mode` ∈ `trash` (default, from config) | `permanent`. `permanent` **requires** `confirm:true` → else `VALIDATION_ERROR`.
`200 {"ok":true,"deleted":1,"mode":"trash","trash_ids":[42],"freed_bytes":1843211,"results":[…]}`

### `GET /api/v1/fileops/trash`
```json
{ "items":[{"id":42,"uid":"output:930","kind":"output","filename":"Anima_00021_.png",
            "original_path":"O:\\ComfyUI\\output\\Anima_00021_.png",
            "size":1843211,"deleted_at":1766000000000,"purge_after":1768592000000,
            "restorable":true}],
  "page":{…},"summary":{"count":1,"bytes":1843211} }
```

### `POST /api/v1/fileops/trash/restore`
Body `{"ids":[42],"on_conflict":"rename"}` → `200 {"restored":1,"results":[{"id":42,"ok":true,"path":"…","uid":"output:930"}]}`. Rebuilds the DB row from `payload_json` — no rescan needed.

### `POST /api/v1/fileops/trash/empty`
Body `{"ids":null,"older_than_days":null,"confirm":true}` → `200 {"purged":18,"freed_bytes":94318452}`.

### `POST /api/v1/fileops/create-folder`
Body `{"root_id":1,"folder":"models\\loras\\anime"}` → `201 {"path":"…"}` · `409 CONFLICT`.

---

## 12. Albums & tags

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/v1/albums?scope=models` | tree with `item_count` — the left rail |
| `POST` | `/api/v1/albums` | `{"name","scope","kind","parent_id","icon","color","query_json"}` → `201` |
| `PATCH` | `/api/v1/albums/{id}` | rename/recolor/reparent |
| `DELETE` | `/api/v1/albums/{id}` | `?delete_items=false` (never touches files) |
| `POST` | `/api/v1/albums/{id}/items` | `{"uids":[…]}` → `200 {"added":n}` |
| `DELETE` | `/api/v1/albums/{id}/items` | `{"uids":[…]}` |
| `GET` | `/api/v1/tags` | `?q=&scope=&limit=` sorted by `use_count DESC` |
| `POST` | `/api/v1/tags` | `{"name","color"}` → `201` |
| `PATCH`/`DELETE` | `/api/v1/tags/{id}` | |
| `POST` | `/api/v1/tags/assign` | `{"uids":[…],"add":["anime"],"remove":["wip"]}` |

System albums are read-only (`"editable": false`); `PATCH`/`DELETE` on them → `409 CONFLICT`.

---

## 13. AI enrichment (optional, Ollama)

### `GET /api/v1/ai/status`
`200 {"enabled":false,"available":false,"url":"http://localhost:11434","models":[],"reason":"connection refused"}` — never an error.

### `POST /api/v1/ai/describe`
```json
{ "uid":"workflow:12","task":"workflow_summary","model":null,"stream":false }
```
`task` ∈ `workflow_summary` | `model_usage_notes` | `update_benefits` | `node_package_summary`.
`200 {"uid":"workflow:12","task":"workflow_summary","text":"…","model":"llama3:latest","cached":false,"elapsed_ms":4210}`
`503 FEATURE_UNAVAILABLE` when Ollama is off/unreachable.
Results are persisted (`description_source:"ollama"`) and marked in the UI with the violet `~` inferred convention.
`stream:true` switches the response to SSE (`event: token`, `event: done`).

---

## 14. MCP

`POST /api/v1/mcp` and `GET /api/v1/mcp` — see `MCP_SPEC.md`. Listed here only so no agent invents a second MCP path.

The **read-only audit log** of what those clients changed lives at `GET /api/v1/mcp/audit` and is specified in §21. It is the only other path under `/api/v1/mcp`.

---

## 15. Static & docs

* `GET /` → SPA `index.html` in production; JSON banner in dev.
* `GET /docs`, `GET /openapi.json` → FastAPI-generated (kept accurate; `qa` asserts every route has a `response_model` or an explicit `responses=` schema).
* `GET /api/v1/ping` → `{"pong":true,"t":…}` — the frontend's boot-retry probe.

---

## 16. Frozen enum values (copy verbatim; do not re-derive)

```
kind:        model | node_package | node_class | workflow | output | input
media_kind:  image | video | audio | model3d | text | other
model_role:  checkpoint | unet | vae | text_encoder | clip_vision | controlnet | lora |
             embedding | upscaler | latent_upscaler | ipadapter | style_model | gligen |
             hypernetwork | frame_interpolation | geometry | detection | audio_encoder |
             other | unknown
base_model:  SD1.5 | SD2.x | SDXL | SD3 | FLUX.1 | FLUX.2 | Pony | Illustrious | NoobAI |
             Lumina | HiDream | Qwen-Image | WAN | HunyuanVideo | LTX-Video | Mochi |
             CogVideo | ACE-Step | StableAudio | Hunyuan3D | Cascade | AuraFlow | Kolors |
             PixArt | Other | Unknown
modality:    image | video | audio | 3d | multimodal | text | unknown
precision:   fp32 | fp16 | bf16 | fp8 | int8 | int4 | mixed | unknown
hash_state:  unhashed | queued | hashing | done | failed | stale
integrity:   ok | invalid_header | not_a_model | truncated | unreadable | unsupported_format
dep_status:  satisfied | missing | ambiguous | unknown
confidence:  declared | inferred | registry
search_mode: lexical | hybrid
origin:      user | bundled | official_template
confidence2: measured | inferred    (storage reasons + reclaim groups; amber vs violet)
storage sort:   reclaim | size | age | name
storage reason: unused | duplicate | superseded | stale | large | integrity |
                orphan_output | non_media | protected
dup method:     sha256 | name+size | name across roots
dup coverage:   exact | partial | heuristic
install flavour: portable | git | desktop | manual | unknown
version state:  current | behind | ahead | unknown
update status:  idle | running | completed | failed
root retention: retain
audit outcome:  ok | partial | error
audit transport: http | stdio
audit tool kind: destructive | write | read | unknown   (resolved from the tool catalogue)
audit sort:      ts | tool | outcome | affected | elapsed   (default -ts)
thumb sizes: 160 | 320 | 640
page limits: 1..500 (default 100); search 1..200 (default 50)
```

---

## 17. Compatibility with the current build

The v1 endpoints from the existing app (`/api/models`, `/api/outputs/file?path=`, `/api/system/reindex`, …) are **removed, not aliased.** `/api/outputs/file?path=` in particular is deleted because it accepts a client-supplied path — the exact shape that combined with B6 to produce the 403-on-everything failure. The frontend is rewritten in the same wave, so there is no compatibility window to preserve.

---

## 18. Storage & maintenance  *(REQUIREMENTS_R2 C10)*

New top-level tab. Shaped for progressive disclosure (C11): `/summary` is one small payload that
answers "where did my terabyte go", and every number in it has a paged endpoint behind it. No
route here returns an unbounded list.

**Confidence is part of the contract.** Every reclaim group and every candidate reason carries
`confidence: "measured" | "inferred"`. `"0 references"` is measured — the index holds it.
`"probably a duplicate because the name and size match"` is inferred. Paint the first amber and
the second violet (C4/C11); never merge the two into one badge.

### `GET /api/v1/storage/summary`

Query: `stale_days` (1..3650, default 180) · `refresh` (bool, re-walks the install instead of
using the ~2-minute footprint cache).

```jsonc
{
  "generated_at": 1787441000000,
  "configured": true,
  "comfyui_path": "O:\\ComfyUI",
  "footprint": {
    "total_bytes": 1609886000000,
    "buckets": [                       // models | outputs | inputs | custom_nodes | cache | program
      {"key":"models","label":"Models","bytes":1593080592733,"files":281,
       "dirs":["models"],"measured":true,"truncated":false,
       "indexed_bytes":1588815471864,"indexed_count":237}
    ],
    "vault": {"key":"vault","label":"Vault database & thumbnails","bytes":52000000,
              "detail":{"thumbnails_bytes":0,"database_bytes":0,"wal_bytes":0,"path":"..."},
              "outside_comfyui": true},
    "measured_at": 1787441000000, "elapsed_ms": 96, "truncated": false
  },
  "volumes": [                          // one per DISTINCT volume - roots can be on different drives
    {"key":"o:","mount":"O:","total_bytes":2000381014016,"free_bytes":291926863872,
     "used_bytes":1708454150144,"used_pct":85.4,"available":true,"error":null,
     "roots":[{"id":1,"kind":"comfyui","path":"O:\\ComfyUI","label":"ComfyUI",
               "configured":true,"retired":false,"exists":true,"indexed":true}]}
  ],
  "primary_volume": { /* the volume holding the default root */ },
  "trash": {"count":0,"bytes":0,"bytes_on_disk":0,"next_purge_at":null,
            "retention_days":30,"directories":[],"endpoint":"/api/v1/fileops/trash"},
  "reclaim": {
    "stale_days": 180,
    "groups": [
      {"key":"unused_models","label":"Models referenced by no workflow and no output",
       "count":100,"bytes":520671699376,"confidence":"measured","reason":"unused",
       "unprotected_count":100,"unprotected_bytes":520671699376},
      {"key":"duplicates","count":4,"bytes":6320000000,"confidence":"inferred",
       "exact_count":0,"reason":"duplicate"},
      {"key":"superseded"}, {"key":"stale_models"}, {"key":"old_outputs"},
      {"key":"orphan_outputs"}, {"key":"non_media_outputs"}, {"key":"integrity"},
      {"key":"trash"}
    ]
  },
  "index": {"models":{"count":237,"bytes":1588815471864},"outputs":{},
            "node_packages":{},"workflows":{},"hashed_files":2,
            "duplicate_detection":"partial"},   // exact | partial | heuristic
  "detail_endpoints": {"candidates":"...","duplicates":"...","roots":"...","trash":"..."}
}
```

`bucket.bytes` is measured from disk; `bucket.indexed_bytes` is what the index holds. The gap is
real information (sidecars, previews, partial downloads, files the indexer skipped) — show both
rather than reconciling them.

`duplicate_detection` states how far hash-based detection can go: `exact` when every model file is
hashed, `partial` when some are, `heuristic` when none are. Hashing is opt-in (C1), so `heuristic`
is the normal cold state and the UI must not imply certainty it does not have.

### `GET /api/v1/storage/candidates`

**The one paged detail table** behind "largest files", "oldest / stale content" and "most
reclaimable". C10.5 asks for size and age as first-class sort keys alongside the combined score, so
they are three sorts over one query rather than three endpoints.

Query: `sort` (`reclaim` default | `size` | `age` | `name`) · `kind` (`model,output`) ·
`reason` (`unused,duplicate,superseded,stale,large,integrity,orphan_output,non_media,protected`) ·
`category` · `role` · `media_kind` · `root_id` · `folder` (prefix) · `q` ·
`min_size` · `max_size` · `older_than_days` · `stale_days` (default 180) ·
`include_protected` (default `true`) · `limit` · `offset`.

**Multi-value filters accept a CSV *or* repeated query params**, and the two are exactly
equivalent: `?kind=model,output` == `?kind=model&kind=output`. Order is preserved and duplicates
collapse. An unknown value is a `422` naming the field — values are never silently dropped.
(`kind`, `reason`, `category`, `role`, `media_kind`, `root_id`.)

```jsonc
{
  "items": [{
    "uid": "model:41", "kind": "model", "id": 41,
    "name": "ltx-2.3-22b-distilled", "filename": "ltx-2.3-22b-distilled.safetensors",
    "ext": ".safetensors", "category": "checkpoints", "role": "checkpoint",
    "media_kind": null,
    "folder": "checkpoints", "rel_path": "...", "abs_path": "...", "root_id": 1,
    "size": 46154000000, "modified_at": 1773000000000, "created_at": 1773000000000,
    "age_days": 163,
    "counts": {"workflows": 0, "outputs": 0},
    "hash_state": "unhashed",
    "reclaim_score": 67,                  // 0..100
    "confidence": "measured",             // 'inferred' if ANY reason is inferred
    "reasons": [
      {"code":"unused","label":"Referenced by no workflow and no output",
       "confidence":"measured","weight":35},
      {"code":"large","label":"Large file (46.2 GB)","confidence":"measured","weight":25}
    ],
    "protected": false,                   // favourite or rating >= 4
    "duplicate_group": null,
    "thumbnail_url": "/api/v1/files/thumbnail?uid=model:41&size=160"
  }],
  "page": {"limit":100,"offset":0,"total":4071,"returned":100,"has_more":true},
  "meta": {"elapsed_ms":12,"sort":"reclaim",
           "matched_bytes":1598700000000,   // total for the CURRENT filter - the
           "page_bytes":420000000000,       // "reclaimable" figure C10.2 asks for
           "weights":{"unused":35,"size":25,"age":20,"duplicate_hash":15,
                      "duplicate_inferred":10,"superseded":10,"integrity":10,
                      "orphan_output":25,"non_media":10,"no_provenance":5,
                      "protected":-40}}
}
```

`reclaim_score` is computed **inside** the SQL statement, so `sort=reclaim` pages correctly.
`meta.weights` is returned so the UI can explain any score without hard-coding the formula.

`role` is the model role (`checkpoint`, `lora`, `vae`, …) and is `null` for outputs. It is carried
on the item so a per-role coverage panel needs no second round trip.

**Freshness.** Every aggregate on this tab — and every `page.total`, facet count and album count
elsewhere — reflects the last committed write with no restart and no cache-busting parameter. See
§0.5.

Protected items (favourite, or rated 4+) score −40 and are **flagged, never hidden** — hiding a
favourited 46 GB file from a "largest files" view would be a lie. Pass `include_protected=false`
to exclude them from a cleanup workflow.

### `GET /api/v1/storage/duplicates`

Query: `method` (`sha256` | `name+size` | `name across roots`) · `limit` · `offset`.

```jsonc
{
  "items": [{
    "key": "twin.safetensors@5242880", "method": "name+size",
    "confidence": "inferred",            // only 'sha256' is ever 'measured'
    "count": 2, "bytes": 4620000000, "reclaimable_bytes": 2310000000,
    "suggested_keep_uid": "model:88",    // the largest member; a suggestion, not a decision
    "items": [{"uid":"model:88","name":"...","category":"...","size":2310000000,
               "abs_path":"...","protected":false}]
  }],
  "page": {},
  "meta": {"reclaimable_bytes": 3160000000,
           "hash_coverage": {"hashed":2,"total":237,"exact_detection_available":true},
           "methods": ["sha256","name+size","name across roots"]}
}
```

Two files can share a name and a byte count and still differ. `name+size` and
`name across roots` are **candidates the owner must confirm**; hash the group to promote it to
certainty. Show the method on every group — the UI must never present an inferred match as exact.

### `GET /api/v1/storage/roots`

Per-root volume, indexed contents, and the retired-root retention state. Also serves C7.3.

```jsonc
{
  "items": [{
    "id":1,"kind":"comfyui","path":"O:\\ComfyUI","label":"ComfyUI","category":null,
    "is_default":true,"source":"config","configured":true,"retired":false,
    "exists":true,"indexed":true,
    "volume":{"total_bytes":2000381014016,"free_bytes":291926863872,"used_pct":85.4,
              "available":true,"error":null},
    "contents":{"models":{"count":237,"bytes":1588815471864},
                "outputs":{"count":3834,"bytes":9885454083},
                "workflows":{"count":211,"bytes":0}},
    "indexed_bytes": 1598700925947
  }],
  "retention_policy": "retain",
  "retention_note": "Rows indexed under a root you have pointed away from are kept, not deleted...",
  "retired_roots": 0,
  "retired_bytes": 0
}
```

`retired: true` means the index still holds rows for a root that is no longer configured — see §19
`GET /api/v1/comfyui/path-policy`.

### `POST /api/v1/storage/estimate`

`{"uids":["model:41","output:9"]}` → the exact byte total, read from the index **before** anything
is deleted. This is the number the confirmation dialog must show.

```jsonc
{"requested":2,"resolved":2,"by_kind":{"model":1,"output":1},"bytes":5961000407,
 "unknown_uids":[],"protected_uids":["model:41"],"protected_count":1}
```

### `POST /api/v1/storage/cleanup`

```jsonc
{"uids":["model:41"], "mode":"trash", "confirm":false}
```

Delegates to the same `services/file_ops` the UI's own delete button uses — same trash, same root
guard, same audit path. Rails, all enforced in the service so MCP inherits them identically:

* `uids` is **required and explicit**; an empty selection is a `422`, never "delete everything stale".
* `model` and `output` only — a `workflow` uid is a `422`.
* Batch cap **200**.
* `mode` defaults to `trash`. `mode:"permanent"` without `confirm:true` is a `422`.

```jsonc
{"ok":true,"mode":"trash","requested":1,"deleted":1,"failed":0,
 "freed_bytes":46154000000,        // priced per item BEFORE the delete, summed over
 "estimated_bytes":46154000000,    // the items that actually went - a partial failure
 "trash_ids":[7],                  // never claims the whole estimate
 "protected_count":0,"recoverable":true,
 "results":[{"uid":"model:41","ok":true,"mode":"trash","trash_id":7,"trash_path":"..."}]}
```

Restore with `POST /api/v1/fileops/trash/restore` (§11). What the trash is holding is in
`summary.trash` and at `GET /api/v1/fileops/trash`.

---

## 19. ComfyUI version, updater, official templates, and opening a workflow  *(REQUIREMENTS_R2 C8)*

Two routes in this section can start a process: the updater run route, and the open-workflow route
when it is explicitly asked and confirmed to start ComfyUI. They are the only two in the entire
product. Everything else here is read-only, apart from the one optional copy into the ComfyUI
installation described under "Opening a workflow inside ComfyUI".

### `GET /api/v1/comfyui/info`

```jsonc
{
  "configured": true,
  "comfyui_path": "O:\\ComfyUI", "install_parent": "O:\\",
  "version": "0.33.0", "version_source": "comfyui_version.py",
  "flavour": "portable",                 // portable | git | desktop | manual | unknown
  "flavour_evidence": ["embedded interpreter at O:\\python_embeded",
                       "portable update folder at O:\\update",
                       "portable launcher batch files beside the ComfyUI folder"],
  "python_home": "O:\\python_embeded",
  "git": {"present":true,"branch":"master","commit":"c67885b1...","shallow":true,
          "remote":"https://github.com/Comfy-Org/ComfyUI","worktree":false},
  "packages": {"comfyui_frontend_package":"1.49.6",
               "comfyui_workflow_templates":"0.11.43",
               "comfyui_workflow_templates_json":"0.1.49",
               "comfyui_embedded_docs":"0.5.10"},
  "updaters": [ /* see /update/plan */ ],
  "recommended_updater": "portable",
  "launchers": [ /* see the open-workflow plan */ ],
  "recommended_launcher": "run_nvidia_gpu",
  "running": {"running":true,"ports":[8188],"comfyui_ports":[8188],"confirmed":true,
              "probed_ports":[8188,8189],
              "evidence":[{"port":8188,"open":true,"families":["ipv4"],"http_status":200,
                           "comfyui":true,"comfyui_version":"0.33.0","error":null},
                          {"port":8189,"open":false,"families":[],"http_status":null,
                           "comfyui":null,"error":null}],
              "method":"loopback probe: tcp on 127.0.0.1 and ::1, confirmed with /system_stats",
              "confidence":"measured","note":"ComfyUI answered on 127.0.0.1:8188. Nothing needs to be started."},
  "update_status": { /* see the update status route */ },
  "launch_status": { /* see the launch status route */ }
}
```

`flavour_evidence` exists so the UI can say *why* it decided, not just what it decided. Version is
**parsed** out of `comfyui_version.py` with `ast`, never imported or executed. Package versions come
from `*.dist-info` directory names in the interpreter that actually launches ComfyUI — for a
portable build that is `python_embeded`, not the interpreter running this app.

**How "is ComfyUI running" is decided.** Every route that branches on it — the update
refusal, the open-workflow plan, the start refusal — reads the same probe, and the probe
reports two different claims rather than one blurred one:

* `running` (and `ports`) means **a port is taken**: a TCP connection was accepted on a
  candidate port. Nothing more is claimed, and this is what the update refusal is decided on
  — updating the files underneath *whatever* holds that port is the risk, not updating
  underneath a process that identified itself;
* `comfyui_ports` (and `confirmed`) means **it answered as ComfyUI**: a request to the
  install's own `/system_stats` returned ComfyUI-shaped JSON. This is what "already running,
  so start nothing" is decided on, and it is what raises `confidence` from `inferred` to
  `measured`.

Each candidate port is probed on **both loopback families** — `127.0.0.1` and `::1`,
concurrently. A portable launcher passes `--listen 0.0.0.0`, which binds the IPv4 wildcard and
not the IPv6 one, so a probe that reaches for one family only will miss a running install and
offer to start a second copy of it. The candidate ports are 8188 and 8189 plus any port a
discovered launcher script pins on its own command line, so an install configured away from
the defaults is still found. `evidence` reports every port that was looked at and what
answered, so the UI can say what was seen instead of asserting.

### `GET /api/v1/comfyui/latest`

Query: `force` (bypass the 6-hour cache).

```jsonc
{"installed":"0.33.0","installed_source":"comfyui_version.py",
 "latest":"0.34.2","state":"behind",          // current | behind | ahead | unknown
 "checked_at":1787441000000,"source":"github","cached":false,
 "release_url":"...","release_notes":"...","reason":null}
```

Read-only and strictly optional. **Offline, blocked, rate-limited and "online lookups are switched
off" all return `200`** with `latest:null` and a `reason` (`online_disabled`, `offline`,
`rate_limited`, `http_403`, ...) plus a `hint`. Never an error toast — the panel must render with no
network at all. Never auto-updates, ever.

### `GET /api/v1/comfyui/update/plan`

Query: `updater` (id; omit for the recommended one). Fetch this **before** showing a confirmation.

```jsonc
{
  "updater":"portable","label":"Portable updater - latest master",
  "path":"O:\\update\\update_comfyui.bat","working_dir":"O:\\update",
  "command":["O:\\update\\update_comfyui.bat"],
  "confirm_path":"O:\\update\\update_comfyui.bat",
  "running":{"running":false,"ports":[]},
  "can_run":true,"blocked_reason":null,      // 'comfyui_running' | 'updater_path_unresolved'
  "warnings":["Restart ComfyUI after the update, then re-scan the vault."],
  "alternatives":[{"id":"portable_stable"},{"id":"git"}]
}
```

The updater is **discovered, never assumed** (C8.3). Candidates are probed in priority order:
`update\update_comfyui_stable.bat`, `update\update_comfyui.bat` and
`update\update_comfyui_and_python_dependencies.bat` **beside** the ComfyUI folder (the portable
layout), plus `git pull --ff-only` when a real checkout is present and `git` is on PATH. Every
mechanism actually found is returned; if none is, `/update/plan` is a `404` naming what was looked
for rather than inventing a path.

### `POST /api/v1/comfyui/update/run` → `202`

```jsonc
{"updater":"portable","confirm_path":"O:\\update\\update_comfyui.bat"}
```

`confirm_path` **must** equal the resolved absolute path from `/update/plan` (compared after
normalisation, so slash direction and case do not matter). A mismatch is a `422` whose `details`
name `resolved_path` and the `confirm_path` received. That is not ceremony: it is what makes the
dialog's promise — *this exact file will be executed* — structurally true, so a stale UI or a
mistaken MCP call cannot run something the user never saw.

Refused with `409 CONFLICT` when a ComfyUI port is accepting connections. Never automatic, never
scheduled, never from MCP without the same confirmation.

```jsonc
{"started":true,"updater":"portable","label":"...","path":"O:\\update\\update_comfyui.bat",
 "working_dir":"O:\\update","stream":"/api/v1/comfyui/update/stream",
 "started_at":1787441000000}
```

### `GET /api/v1/comfyui/update/stream` (SSE) · `GET /api/v1/comfyui/update/status`

Stream events: `open`, `output` (`{"line":"...","n":12}`, capped at 4 000 lines), `done`, `error`,
`heartbeat`. `done` and `/update/status` carry:

```jsonc
{"status":"completed","exit_code":0,"error":null,"duration_ms":42000,"lines":180,
 "version_after":"0.34.2","restart_required":true,
 "note":"ComfyUI was updated. Restart ComfyUI, then re-scan the vault..."}
```

The exit code is always surfaced. A run that exceeds 30 minutes is killed and reported.

### Opening a workflow inside ComfyUI

The owner's ask is one action: open this workflow in ComfyUI, and if ComfyUI is not running,
offer to start it first. Three things can happen, and each is a separate consent, because
they are separate acts:

| Act | Consent |
|---|---|
| Open a URL | none needed — a URL is not a side effect |
| Copy the file into `<comfyui>\user\default\workflows` | `confirm_copy_destination` naming that exact path |
| Start ComfyUI | `start: true` **and** `confirm_launcher_path` naming the resolved absolute launcher path |

Clicking "open" never starts a program. That is the same rule the updater follows.

**What this ComfyUI frontend can actually be told to open.** Established by reading the
frontend package this install serves (`comfyui_frontend_package` 1.49.6) and ComfyUI's own
`app/custom_node_manager.py`, not by assuming a URL shape:

* the router declares one application route (`/`), and the only query parameters any code in
  it reads are `template`, `source`, `mode` and `share`;
* `?template=<name>&source=default` loads `/templates/<name>.json` — the official templates
  distribution;
* `?template=<name>&source=<module>` loads `/api/workflow_templates/<module>/<name>.json`,
  which ComfyUI's server mounts as a static directory for every **loaded** node package, one
  level deep, for the folder names `example_workflows`, `example`, `examples`, `workflow`,
  `workflows`;
* `?share=<id>` is a cloud share id and needs an account;
* both ends reject a `template` or `source` that is not `^[A-Za-z0-9_.-]+$`.

**There is therefore no deep link for a workflow in `user\default\workflows`** — no loader in
this build reads a user workflow path out of the URL. For those, the plan reports
`open_method: "manual"`, opens ComfyUI at its own address, and names the file to pick from
the Workflows sidebar. Shipping a link that silently does nothing would be worse than saying
so. On the owner's install this splits **147 of 211** indexed workflows into `deep_link` and
the remaining 64 into `manual`.

The optional copy into the user workflows folder is offered for what it really buys — the
graph appears in ComfyUI's Workflows sidebar — and `copy.creates_deep_link` is **always
`false`**, because this frontend has no user-workflow deep link to create.

### `GET /api/v1/comfyui/open-workflow/plan`

Query: `uid` (a workflow uid, required) · `launcher` (launcher id; omit for the recommended
one). Fetch this **before** showing any confirmation.

```jsonc
{
  "uid": "workflow:12", "workflow_id": 12, "name": "basic_flow",
  "abs_path": "O:\\ComfyUI\\custom_nodes\\ComfyUI-Pack\\example_workflows\\basic_flow.json",
  "rel_path": "custom_nodes\\ComfyUI-Pack\\example_workflows\\basic_flow.json",
  "origin": "bundled", "origin_package": "ComfyUI-Pack",
  "origin_label": "bundled with ComfyUI-Pack",
  "running": {"running": true, "ports": [8188], "comfyui_ports": [8188],
              "confirmed": true, "probed_ports": [8188, 8189], "evidence": [ /* per port */ ],
              "method": "loopback probe: tcp on 127.0.0.1 and ::1, confirmed with /system_stats",
              "confidence": "measured", "note": "ComfyUI answered on 127.0.0.1:8188. ..."},
  "port": 8188,
  "port_reason": "the port ComfyUI answered on",
  "url": "http://127.0.0.1:8188/?template=basic_flow&source=ComfyUI-Pack",
  "open_method": "deep_link",                 // deep_link | manual
  "deep_link": {
    "supported": true, "reason": null, "template": "basic_flow", "source": "ComfyUI-Pack",
    "params": {"template": "basic_flow", "source": "ComfyUI-Pack"},
    "query": "?template=basic_flow&source=ComfyUI-Pack",
    "explanation": "ComfyUI serves this package's example graphs at ...",
    "verified_against": "comfyui_frontend_package 1.49.6",
    "checked": true, "served": true, "served_reason": null, "served_note": null
  },
  "deep_link_check": {"checked": true, "served": true, "reason": null, "note": null},
  "filename": "basic_flow.json",
  "manual_hint": null,
  "launcher": {"id": "run_nvidia_gpu", "kind": "batch", "label": "run_nvidia_gpu.bat",
               "path": "O:\\run_nvidia_gpu.bat", "working_dir": "O:\\",
               "command": ["O:\\run_nvidia_gpu.bat"], "port": 8188,
               "port_source": "launcher command line", "available": true,
               "recommended": true, "note": "..."},
  "launcher_confirm_path": "O:\\run_nvidia_gpu.bat",
  "launcher_alternatives": [{"id": "run_cpu"}, {"id": "run_nvidia_gpu_stable_memory"}],
  "launcher_error": null,
  "copy": {"possible": true, "needed": true, "reason": null,
           "destination": "O:\\ComfyUI\\user\\default\\workflows\\basic_flow.json",
           "target_dir": "O:\\ComfyUI\\user\\default\\workflows",
           "exists": false, "creates_deep_link": false, "note": "..."},
  "steps": ["Start ComfyUI by running O:\\run_nvidia_gpu.bat - ...", "..."],
  "needs_start": true, "can_open": true, "blocked_reason": null,
  "frontend_version": "1.49.6", "comfyui_version": "0.33.0"
}
```

Launchers are **discovered, never assumed**, exactly as the updater is: `run_*.bat` files
beside the ComfyUI folder, each reported with its resolved absolute path, its working
directory (a portable launcher uses relative paths and breaks anywhere else), and the port
read out of its own command line rather than guessed. `main.py` with the interpreter that
ships with the install is the fallback when no script is found. If nothing is found,
`launcher` is `null`, `blocked_reason` is `"no_launcher_found"`, and the UI says so instead
of inventing a path.

Three gates decide what may appear in that list at all, because nominating an executable is
the first half of running one (`SECURITY_REVIEW` §8, S-19 · S-20 · S-21):

* the configured folder must be a **verified install** — `comfyui_version.py` **and**
  `main.py` **and** `models/`, the same proof the updater routes require. A directory that
  merely satisfies the config route's weaker check gets no launchers, and the plan says why;
* the script must **carry evidence that it starts this install** — its text names ComfyUI's
  entry point. `run_*.bat` is globbed in the *parent* of the ComfyUI folder, which on a
  portable build is a drive root; a batch file that lands there is not a launcher just
  because of its name. Each entry reports the `evidence` it was accepted on;
* the resolved path must be **inert to `cmd.exe`**. A `.bat`/`.cmd` target is never executed
  directly on Windows — `CreateProcess` runs it through `cmd.exe`, which re-parses the whole
  command line — so a path holding `&`, `^`, `%`, `(`, `)` or `"` is reported with
  `available: false` and `unsafe_reason: "cmd_metacharacter_in_path"` and is refused with
  `409 CONFLICT` if named. The same rule applies to the updater.

A `--port` value outside 1–65535 is discarded rather than believed, and `port_source` is
`null` when the port was not measurable.

`deep_link.reason` when `supported` is `false`: `user_workflow_has_no_deep_link` ·
`not_served_by_comfyui` (nested deeper than ComfyUI serves) · `name_not_url_addressable` ·
`package_disabled` · `no_path`.

**Addressable is not the same as served, so the address is confirmed before it is offered.**
A bundled example graph has a legal address only while ComfyUI is *loading the package that
owns it*: a package that is disabled, renamed, or failed to import leaves the address
answering 404, and ComfyUI's frontend reports that as a small toast on an empty canvas — from
the owner's side, "it opened ComfyUI and did nothing". So when ComfyUI is running, the plan
asks it: the template list route it serves for loaded packages, or its official template
index for a `source=default` link, and the name is matched **inside the answer** — it is never
interpolated into a URL the vault requests.

* `deep_link_check.checked` is `false` and `served` is `null` when ComfyUI was not up to be
  asked. The link is still offered: nothing was disproved;
* `served: true` — the address is confirmed and `open_method` stays `deep_link`;
* `served: false` — `open_method` becomes `manual`, `url` drops the query rather than
  carrying one that will not work, and `manual_hint` says why and names the file to pick.
  `deep_link_check.reason` is `package_not_loaded` · `template_not_listed` ·
  `not_in_the_official_template_index`, or a transport reason when ComfyUI did not answer.

`filename` is the file's own name — what the owner has to look for in ComfyUI's Workflows
sidebar whenever `open_method` is `manual`. The UI must show it; a `manual` open that does
not name the file is a blank ComfyUI with no explanation.

`copy.reason` when `possible` is `false`: `already_in_the_workflows_folder` ·
`destination_exists` · `no_comfyui_path`.

### `POST /api/v1/comfyui/open-workflow`

```jsonc
{"uid": "workflow:12",
 "launcher": "run_nvidia_gpu",
 "start": true,
 "confirm_launcher_path": "O:\\run_nvidia_gpu.bat",
 "copy_to_user_workflows": false,
 "confirm_copy_destination": null}
```

`confirm_launcher_path` **must** equal `launcher_confirm_path` from the plan (compared after
normalisation) whenever `start` is true; `confirm_copy_destination` **must** equal
`copy.destination` whenever `copy_to_user_workflows` is true. A mismatch is a `422` whose
`details` name the resolved path and the value received. Nothing is started and nothing is
written when a confirmation does not match — asserted by
`backend/tests/test_comfyui_open_workflow.py`.

Refusals: `409 CONFLICT` when ComfyUI is not running and `start` was not sent — the `details`
carry the launcher and its `confirm_launcher_path`, so the UI can ask the second question;
`409` when a copy would overwrite a file that is already there, which it **never** does;
`409` when ComfyUI is already running and a start was requested anyway, or while the updater
is running; `403 PATH_NOT_ALLOWED` if the copy destination is outside every configured root;
`422` if it is inside a root but outside `<comfyui>\user\default\workflows` itself, which is
the only folder this route may write into (`SECURITY_REVIEW` S-22). The copy is an
**exclusive create**, so "it never overwrites" holds even for a file that appears between the
plan and the write.

```jsonc
{"uid": "workflow:12", "name": "basic_flow",
 "url": "http://127.0.0.1:8188/?template=basic_flow&source=ComfyUI-Pack",
 "open_method": "deep_link", "deep_link": { /* as above */ },
 "deep_link_check": { /* as above */ }, "filename": "basic_flow.json",
 "manual_hint": null, "running": { /* the probe, as above */ },
 "port": 8188,
 "copied": false, "copy_destination": null, "copy_note": null,
 "started": true, "already_running": false, "ready": false,
 "launcher": "run_nvidia_gpu", "launcher_path": "O:\\run_nvidia_gpu.bat",
 "stream": "/api/v1/comfyui/launch/stream", "timeout_s": 300,
 "started_at": 1787441000000,
 "note": "ComfyUI is starting. Watch the launch stream and open the URL when it reports ready."}
```

**Liveness is measured here, not taken from the plan.** The plan is a snapshot, and a caller
may hold one for a while; a dialog that measured "not running" ten minutes ago will send
`start: true` with a perfectly valid confirmation. This route probes again first, and when
ComfyUI is up it answers `already_running: true`, `ready: true`, `started: false` and touches
no process — `start` is not even consulted. A stale plan can therefore never start a second
copy of ComfyUI. When a start *was* made, the caller waits on the launch stream and opens
`url` on `ready` — the server never opens a browser.

The caller is expected to open `url` in a **separate window** rather than a tab (window
features, not `target="_blank"`), and to open it **once** — `ready` and `done` both arrive on
a successful launch. A window opened from a finished background task has no user activation
behind it, so browsers block it by default: a client that cannot open the window must say so
and offer a control that opens it from a real click, never fail silently.

The process is started with a list argv and `shell=False`, in its own console window, from
the launcher's own working directory, with the environment inherited rather than assembled.
Nothing in the argv comes from the request: the executable is one of the paths discovery
found on disk, and the request can only name one of them. `confirm_launcher_path` is
compared after `realpath` normalisation, so a case variant, an 8.3 short name, a `\\?\`
prefix or a junction naming the **same file** is accepted — and the argv is still the
discovered path, never the caller's string, so a confirmation can loosen the spelling but
can never redirect the spawn. It is deliberately not killed when the vault exits — ComfyUI is the owner's program,
and its own console window is how they watch it and close it.

This capability is **not exposed over MCP.** Starting a process on a hallucinated tool call
is exactly the failure the C8 confirmation posture exists to prevent, and no agent-side
confirmation can be trusted to be a person.

### `GET /api/v1/comfyui/launch/status` · `GET /api/v1/comfyui/launch/stream` (SSE)

Stream events: `open`, `phase` (`starting`, `spawned`), `waiting`
(`{"elapsed_ms":42000,"port":8188}`, roughly every 2 s), `ready`, `error`, `done`,
`heartbeat`. `done` and the status route carry:

```jsonc
{"status": "ready",                 // idle | starting | ready | failed
 "running": false, "ready": true, "launcher": "run_nvidia_gpu",
 "path": "O:\\run_nvidia_gpu.bat", "port": 8188, "pid": 24680,
 "url": "http://127.0.0.1:8188/?template=basic_flow&source=ComfyUI-Pack",
 "error": null, "exit_code": null, "elapsed_ms": 62000, "started_at": 1787441000000,
 "finished_at": 1787441062000, "note": "ComfyUI is accepting connections."}
```

Readiness is measured, not assumed, and "ready" means **ComfyUI answered** — not that its
port accepted a connection. The wait polls once a second for a TCP connection *and* a
ComfyUI-shaped answer from the install's own `/system_stats`, because a socket that accepts
is not yet a server that serves: an address opened in that gap loads the page but not the
graph. A cold start loads every installed node package and can take minutes, so the wait runs
for up to **300 seconds** and then reports the timeout rather than pretending. A launcher
that exits before ComfyUI answers is reported with its exit code.

### `GET /api/v1/comfyui/templates`

The official ComfyUI templates shipped with this install. Query: `bundle` · `q` · `limit` · `offset`.

```jsonc
{"available":true,"reason":null,"package_version":"0.1.49",
 "path":"O:\\python_embeded\\Lib\\site-packages\\comfyui_workflow_templates_json\\templates",
 "bundles":[{"key":"media-image","count":187},{"key":"media-api","count":93}],
 "items":[{"id":"3d_hunyuan3d_image_to_model","title":"3d hunyuan3d image to model",
           "bundle":"media-image","filename":"....json","size":12530,"path":"...",
           "origin":"official_template","origin_label":"official template",
           "template_version":null}],
 "total":518,
 "note":"Shipped with ComfyUI itself and listed read-only. They are not indexed as vault assets, so vault file operations can never touch them."}
```

**Why catalogued and not indexed:** these files live inside the Python distribution that runs
ComfyUI. Indexing them as vault rows would make ComfyUI's own installed files reachable by vault
rename, move and delete. The vault must never be able to damage the installation it describes.
`available:false` always carries a `reason`.

### `GET /api/v1/comfyui/workflow-origins`

```jsonc
{"total":211,                 // indexed vault workflows only
 "indexed_total":211,
 "catalogued_total":518,      // official templates, counted separately
 "groups":[
   {"origin":"bundled","label":"bundled with ComfyUI-WanVideoWrapper",
    "package":"ComfyUI-WanVideoWrapper","count":44,"runnable":0,"broken":44},
   {"origin":"user","label":"user","package":null,"count":42,"runnable":15,"broken":27},
   {"origin":"official_template","label":"official template","count":518,
    "catalogued_only":true}
 ],
 "official_templates": { /* the catalogue, items capped */ }}
```

### `GET /api/v1/comfyui/path-policy`

What happens to existing rows when the ComfyUI path changes (C7.3). The Settings screen must state
this **before** the path is saved.

```jsonc
{"policy":"retain",
 "summary":"Rows indexed under the previous ComfyUI folder are kept. Nothing is deleted when you change the path.",
 "details":["The old root is retired, not removed...", "..."],
 "reindex_recommended":true,
 "current_roots":2,"retired_roots":0,"retired_bytes":0,
 "checked_at":1787441000000}
```

### Workflow `origin` on every workflow row

`GET /api/v1/workflows` and `GET /api/v1/workflows/{id}` now carry, on every item:

```jsonc
{"origin":"bundled",                                  // user | bundled | official_template
 "origin_package":"ComfyUI-WanVideoWrapper",
 "origin_label":"bundled with ComfyUI-WanVideoWrapper"}
```

`origin_label` is pre-composed exactly as C8.4 words it, so no client re-derives the phrasing.

---

## 20. Workflow "Enable" — resolve and fetch missing resources  *(REQUIREMENTS_R2 C9)*

**159 of 212 indexed workflows are unrunnable.** This section is the API that makes one runnable:
it reports what is missing, where each piece would go and how big it is, and then fetches only what
the user explicitly selected.

The contract is deliberately two-step, and the second step is deliberately awkward — the same shape
the C8 updater uses. A confirmation that does not name what will happen is not a confirmation.

1. `GET /api/v1/workflows/{id}/enable/plan` → the dependency report **plus a short-lived
   `plan_token`**. Nothing is downloaded. Nothing is written.
2. `POST /api/v1/workflows/{id}/enable/fetch` → takes that token, the `item_ids` the user picked,
   and `confirm: true`. A stale token, a superseded plan, an id that was not in the plan, or a
   missing confirmation is a `422` and issues **no** outbound request.

### Rules that hold on every route in this section

* **No route accepts a URL or a filesystem path.** Sources come from the workflow's own model
  manifest, from a `download_url` the vault already cached from Civitai (keyed on a hash the vault
  computed itself), or from the ComfyUI-Manager registry — never from the request body. No filename
  is ever sent to an external API (ARCHITECTURE §8.4 still holds).
* **Destinations are derived server-side** from the node input that referenced the file
  (`ckpt_name` → `checkpoints`, `lora_name` → `loras`, `unet_name` → `diffusion_models`,
  `vae_name` → `vae`, `clip_name` → `text_encoders`, …), validated with `validate_filename()` and
  proven inside a configured root **before the first byte is written**. A server-supplied
  `Content-Disposition` filename is a hint only and passes the same validation.
* **Hosts are allowlisted**: `civitai.com`, `huggingface.co`, `hf.co` and their sub-domains for
  models; `github.com`, `gitlab.com`, `codeberg.org` for registry-declared git remotes. `https`
  only, no credentials in the authority, no bare IP literals. Redirects are never followed blindly —
  each hop is re-validated (max 5) and an `Authorization` header never survives a host change. A hop
  that leaves the list aborts with `UPSTREAM_UNAVAILABLE` and the second request is never issued.
* **Verify, then place.** Bytes land in `<target>.part`; the finished file is re-hashed in full from
  disk and compared against the advertised size and, where published, SHA-256. Only a match reaches
  `os.replace`. A mismatch moves to `<root>/.vault-quarantine/` with a `reason.json`, writes a
  `scan_errors` row, and the item reports `INTEGRITY_MISMATCH`.
* **Never overwrite.** `on_conflict` accepts `fail` (default), `skip` or `keep_both`. There is no
  `overwrite` on this path.
* **Free space is checked** before the batch starts (advertised size + 5 %) and again every 256 MB
  during the transfer. Refusal is `507 INSUFFICIENT_SPACE` naming the shortfall.
* **Node packages are cloned or reported, never installed.** No `pip install`, no
  `requirements.txt`, no `install.py`, no `setup.py`, no post-clone hook, no submodules — the exact
  command is returned in `manual_steps` for the user to run themselves.

### `GET /api/v1/workflows/{id}/enable/plan`

Query: `on_conflict` = `fail` (default) `| skip | keep_both` — recorded in the plan.

```jsonc
{"workflow":{"uid":"workflow:12","id":12,"name":"wan22_animate_mix","is_runnable":false,
             "origin":"user"},
 "summary":{"total":14,"satisfied":10,"missing_models":2,"missing_node_packages":1,
            "fetchable":2,"not_fetchable":1,"download_bytes":13958643712,
            "items_with_unknown_size":0},
 "space":{"sufficient":true,"shortfall_bytes":0,"download_bytes":13958643712,"margin_pct":5,
          "volumes":[{"directory":"O:\\ComfyUI\\models\\diffusion_models","root_id":1,
                      "root_label":"ComfyUI","download_bytes":13958643712,
                      "required_bytes":14656575897,"free_bytes":313532612608,
                      "total_bytes":2000398934016,"sufficient":true,"used_pct":84.3}]},
 "models":[
   {"item_id":"mode_9f21c0a3be04d17c","kind":"model",
    "ref_name":"wan2.2_i2v_high_noise.safetensors","occurrences":1,
    "via":[{"class":"UNETLoader","input":"unet_name"}],
    "category":"diffusion_models",
    "destination":{"category":"diffusion_models","root_id":1,"root_label":"ComfyUI",
                   "directory":"O:\\ComfyUI\\models\\diffusion_models",
                   "abs_path":"O:\\ComfyUI\\models\\diffusion_models\\wan2.2_i2v_high_noise.safetensors",
                   "filename":"wan2.2_i2v_high_noise.safetensors"},
    "source":{"url":"https://huggingface.co/.../wan2.2_i2v_high_noise.safetensors",
              "host":"huggingface.co","provider":"workflow_manifest",
              "size":13958643712,"sha256":"a1b2...","hash_available":true,
              "declared_directory":"diffusion_models","notes":[]},
    "status":"fetchable","reason":null,"suggestions":[]}],
 "node_packages":[
   {"item_id":"node_5c7d1e9082f3a4b6","kind":"node_package","ref_name":"ComfyUI-Foo",
    "repo_url":"https://github.com/x/ComfyUI-Foo","host":"github.com",
    "class_types":["SomeUnknownNode"],"class_count":1,
    "destination":{"category":"custom_nodes","root_id":1,"root_label":"ComfyUI",
                   "directory":"O:\\ComfyUI\\custom_nodes",
                   "abs_path":"O:\\ComfyUI\\custom_nodes\\ComfyUI-Foo",
                   "filename":"ComfyUI-Foo"},
    "status":"fetchable","reason":null,
    "manual_steps":["git clone --depth 1 \"https://github.com/x/ComfyUI-Foo\" \"O:\\ComfyUI\\custom_nodes\\ComfyUI-Foo\""],
    "never_runs":["No pip install is ever run.","..."]}],
 "plan_token":"5c2b...","plan_expires_in_ms":900000,"plan_items":2,
 "policy":{"model_hosts":["civitai.com","hf.co","huggingface.co",".civitai.com",".huggingface.co",".hf.co"],
           "git_hosts":["codeberg.org","github.com","gitlab.com"],
           "scheme":"https","max_redirects":5,
           "never_runs":["..."],"on_conflict_allowed":["fail","skip","keep_both"],
           "git_available":true},
 "generated_at":1787441000000}
```

`status` per item is one of `fetchable` · `already_present` · `no_source` · `blocked`. `no_source`
and `blocked` items are still listed **with their destination**, because "here is where to put it
yourself" is the useful answer when the app cannot fetch it.

Issuing a new plan for a workflow **supersedes** the previous one; the old token stops working.
A plan lives 15 minutes and describes at most 200 items.

### `POST /api/v1/workflows/{id}/enable/fetch` → `202`

```jsonc
{"plan_token":"5c2b...","item_ids":["mode_9f21c0a3be04d17c"],"confirm":true,
 "on_conflict":"fail"}
```
```jsonc
{"batch_id":"7d1a2f9c4e08","workflow_id":12,"queued":1,"bytes_total":13958643712,
 "items":[{"item_id":"mode_9f21c0a3be04d17c","kind":"model",
           "ref_name":"wan2.2_i2v_high_noise.safetensors","host":"huggingface.co",
           "target_abs_path":"O:\\ComfyUI\\models\\diffusion_models\\wan2.2_i2v_high_noise.safetensors",
           "size":13958643712}],
 "stream":"/api/v1/enable/stream","started_at":1787441000000,"scan_job_id":41}
```

Failure modes: `422 VALIDATION_ERROR` (no `confirm`; unknown, expired or superseded token; an
`item_id` outside the plan; more than 200 items), `409 CONFLICT` (a fetch is already running, or the
destination exists and `on_conflict` is `fail`), `409 NOT_CONFIGURED`, `507 INSUFFICIENT_SPACE`
(`details` names `required_bytes`, `free_bytes`, `shortfall_bytes`).

### `POST /api/v1/workflows/{id}/enable/recheck`

C9.8 — is it runnable now, and is that answer stale?

```jsonc
{"workflow":{"uid":"workflow:12","id":12,"name":"wan22_animate_mix"},
 "is_runnable":false,"is_runnable_recorded":false,
 "missing_models":["wan2.2_i2v_high_noise.safetensors"],"missing_node_classes":[],
 "counts":{"missing_models":1,"missing_node_classes":0},
 "scan":{"running":true,"phase":"models","job_id":41},"stale":true,
 "message":"A scan is still running, so this answer may be out of date. Check again when it finishes.",
 "checked_at":1787441000000}
```

A placed file is invisible until it is indexed, so a completed fetch schedules an **incremental
scan**; `stale` is `true` while that scan runs.

### `GET /api/v1/enable/status`

Query: `batch_id`, `workflow_id` (both optional; omit for everything).

```jsonc
{"running":true,"states":{"done":1,"running":1,"queued":2},"queued":2,
 "bytes_total":13958643712,"bytes_done":4294967296,
 "active":[{"id":9,"kind":"model","ref_name":"...","host":"huggingface.co",
            "bytes_done":4294967296,"size":13958643712,"target_abs_path":"..."}],
 "items":[{"uid":"enable_job:9","id":9,"batch_id":"7d1a2f9c4e08","workflow_id":12,
           "item_key":"mode_9f21...","kind":"model","ref_name":"...",
           "category":"diffusion_models","source_host":"huggingface.co",
           "expected_size":13958643712,"bytes_done":4294967296,"state":"running",
           "error_code":null,"error_message":null,"target_abs_path":"...",
           "result":null,"finished_at":null}],
 "quarantine":[],"git_available":true}
```

Item `state` is one of `queued` · `running` · `done` · `failed` · `cancelled` · `quarantined` ·
`skipped`.

### `POST /api/v1/enable/cancel`

```jsonc
{"batch_id":"7d1a2f9c4e08"}      // omit batch_id to cancel every queued fetch
```
```jsonc
{"cancelled":3,"batch_id":"7d1a2f9c4e08"}
```

A cancelled download leaves only the `.part` file — never a partial file at the real name — and the
next fetch of the same item resumes it with HTTP `Range`, re-hashing the whole file at the end so a
resumed prefix is never trusted.

### `GET /api/v1/enable/stream`  (SSE)

Events: `open`, `phase`, `progress` (coalesced to ≤10 Hz per item), `item`, `done`, `heartbeat`,
`overflow`. Same envelope as `/index/stream` and `/hash/stream`.

### `GET /api/v1/enable/quarantine`

```jsonc
{"items":[{"id":"20260822-214455-3f9c1a02","root_id":1,
           "abs_path":"O:\\ComfyUI\\.vault-quarantine\\20260822-214455-3f9c1a02",
           "files":[{"name":"bad.safetensors.part","size":1048576}],"bytes":1048576,
           "reason":{"ref_name":"bad.safetensors","source_host":"huggingface.co",
                     "expected_sha256":"aaaa...","actual_sha256":"bbbb...",
                     "problems":["SHA-256 mismatch: expected aaaa..., got bbbb..."],
                     "intended_path":"O:\\ComfyUI\\models\\loras\\bad.safetensors"}}],
 "total":1,"bytes":1048576}
```

`.vault-quarantine` is excluded from every scan walk and from the storage footprint buckets, the
same way `.vault-trash` is.

---

## 21. MCP activity log  *(DECISIONS C5 rail 3)*

C5 grants an external MCP client the complete file-operation set — rename, move, delete, trash
restore, trash empty, create folder, tag assignment, plus hash and embedding job control — over a
1.5 TB library. Rail 3 makes every one of those calls append a row to `mcp_audit`, **with its
argument values**, and requires that row to be readable from Settings → Activity. This section is
that read surface. It is the whole of it.

**Read-only, by construction.** One route, one verb. No route in this API creates, edits, prunes,
purges or deletes an audit row, and none ever will: an audit log the application can erase is not
an audit log. Retention is stated rather than implemented — see "Retention" below.

### `GET /api/v1/mcp/audit`

Paged, newest first, with a summary the UI leads with (C11: summary first, detail on demand).

| Param | Type | Meaning |
|---|---|---|
| `limit` / `offset` | int | §0.3 pagination. `limit` 1–500, default 100 |
| `sort` | enum | `ts` `tool` `outcome` `affected` `elapsed`, `-` for descending. Default `-ts` |
| `tool` | string, repeatable or CSV | MCP tool names |
| `outcome` | enum, repeatable or CSV | `ok` `partial` `error` |
| `transport` | enum, repeatable or CSV | `http` `stdio` |
| `session_id` | string | One MCP session's calls |
| `since` / `until` | epoch ms | Inclusive at both ends; `until` < `since` → `VALIDATION_ERROR` |
| `q` | string | Free text over the **tool name and the affected uids**. `%` and `_` are literal characters, not wildcards |

Filters are AND across fields, OR within a repeated field, exactly as §0.4. Every sort and enum
token is checked against a frozen allowlist and passed to the query service as a *key*; nothing
from the request reaches SQL.

`200`
```jsonc
{ "items":[
    { "id":1167,"ts":1787460799975,"session_id":"fbd2e4d42000496d","transport":"http",
      "tool":"vault_delete","title":"Delete files",
      "arguments":{"uids":["workflow:214"],"mode":"permanent","confirm":true},
      "uids":["workflow:214"],"outcome":"ok","affected":1,"error_code":null,
      "elapsed_ms":5,"kind":"destructive","mutating":true,"destructive":true } ],
  "page":{"limit":100,"offset":0,"total":1167,"returned":100,"has_more":true},
  "summary":{
    "total":1167,"vault_total":1167,"filtered":false,"sessions":79,"affected":835,
    "first_ts":1787425687486,"last_ts":1787460799975,
    "by_outcome":{"ok":982,"partial":0,"error":185},
    "by_transport":{"http":1166,"stdio":1},
    "by_kind":{"destructive":436,"write":731,"read":0,"unknown":0},
    "by_tool":[{"tool":"vault_delete","title":"Delete files","count":276,"errors":50,
                "affected":222,"last_ts":1787460799975,"kind":"destructive",
                "mutating":true,"destructive":true}],
    "by_tool_truncated":false },
  "meta":{"elapsed_ms":4,"query_id":"…","sort":"-ts"},
  "retention":"append-only; rows are never edited, pruned or deleted" }
```

* `arguments` carries the **values** a mutating call was given. That is the one deliberate
  exception to MCP_SPEC §9's no-argument-logging rule (which still holds for read tools, and still
  holds for the server's own log line). It is the reason the log is worth keeping.
* `kind` is resolved from the live MCP tool catalogue, not stored on the row: `destructive` for a
  tool that can delete, move or rename; `write` for one that changes the vault without destroying
  anything; `read` for a read-only tool; `unknown` for a tool the current catalogue no longer
  carries — in which case `mutating` and `destructive` are `null` and the UI marks the entry as
  inferred (violet `~`, C4) rather than guessing what it did.
* `summary` is measured under the **same filters** as `items`, so the two can never disagree.
  `vault_total` is the one unfiltered figure, so the UI can say "12 of 1,167".
* `by_tool` names at most 24 tools; `by_tool_truncated` says when more exist.
* Ordering ties break on `id DESC`, not the `id ASC` the asset lists use: this is a log, and inside
  one millisecond the newest call must still read as the newest.

### Retention

**None. The table is append-only for the life of the vault, and no code path removes a row.** One
mutating tool call is one row of roughly 300–600 bytes, so a client that mutates a thousand times a
day costs well under a megabyte a year; the index `ix_mcp_audit_ts` keeps the query flat. If a
retention policy is ever wanted it belongs in this document first and in a deliberate, owner-driven
maintenance action second — never in a route this API exposes, and never as a side effect of
anything else.

---
