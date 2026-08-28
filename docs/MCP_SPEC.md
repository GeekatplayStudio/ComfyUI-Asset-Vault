# MCP Server Spec — Geekatplay ComfyUI Asset Vault
Protocol: **Model Context Protocol, revision `2025-06-18`**.
Server name `geekatplay-comfyui-vault`, version `2.0.0`.

---

> **AMENDED — see `DECISIONS.md` C5.** The owner has granted this server FULL file-operation
> access, including delete. Every "read-only" claim below is superseded by DECISIONS.md C5,
> which adds 8 file-operation tools and promotes 3 job-control tools (**24 under C5**) plus an
> `mcp_audit` log, a 200-item batch cap, trash-by-default deletion, and `confirm:true` for
> permanent deletion. Read DECISIONS.md C5 BEFORE implementing anything in §5, §8, or §9.
>
> **EXTENDED — `REQUIREMENTS_R2.md` C9.9** adds the two workflow "Enable" tools
> (`enable_workflow_plan`, `enable_workflow_fetch`, §5.17–§5.18), which C9.9 requires to be
> available from MCP "subject to the same confirmation rules". **Total 26.**

## 1. What is wrong today

`backend/app/api/mcp_api.py` + `services/mcp_server.py` implement **only** `tools/list` and `tools/call`, and reject everything else with a bare `HTTPException(400)`. That is not an MCP server — no MCP client can connect to it, because:

1. There is **no `initialize` handshake**, so the client never learns the protocol version or the server's capabilities and aborts before it would ever call `tools/list`.
2. There is **no `notifications/initialized`** handling; JSON-RPC *notifications* (no `id`) must be accepted and answered with **HTTP 202 and an empty body**, not with a JSON-RPC result.
3. There is **no stdio transport**, which is how desktop MCP clients and IDE integrations launch a local server.
4. Errors use `-32601` (Method not found) for *tool* failures. Tool failures must be a **successful** `tools/call` result with `isError: true`; `-32601` is reserved for unknown JSON-RPC methods. A client seeing `-32601` for a bad argument will disable the tool.
5. `tools/call` results omit `structuredContent`, so an agent has to re-parse a JSON string out of a text block.
6. `inputSchema` objects omit `additionalProperties` and `$schema`; there are no `outputSchema` declarations at all.
7. `execute_mcp_tool` opens a DB connection per call with no read-only mode and no error isolation.

Everything below replaces both files.

---

## 2. Transports

### 2.1 Streamable HTTP — `/api/v1/mcp`

The MCP "Streamable HTTP" transport. One path, three verbs:

| Verb | Purpose |
|---|---|
| `POST` | Client→server JSON-RPC. Body is a single request/notification or a batch array. |
| `GET` | Opens a server→client SSE stream for notifications (progress, list-changed). `Accept: text/event-stream` required. |
| `DELETE` | Terminates the session. |

**Response mode selection on POST:**
* Body contains only notifications/responses → **`202 Accepted`, empty body.**
* Body contains ≥1 request and the server answers immediately → **`200` with `Content-Type: application/json`** carrying the JSON-RPC response (or array).
* Long-running tool (e.g. `vault_reindex`) → **`200` with `Content-Type: text/event-stream`**, emitting `notifications/progress` then the final response.

**Session:** on `initialize` the server returns `Mcp-Session-Id: <uuid4>`. Every subsequent request MUST echo it in the `Mcp-Session-Id` header. Unknown/expired session → `404` with JSON-RPC error `-32001`. Sessions expire after 30 min idle.

**Required headers on POST:** `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version: 2025-06-18` (after initialize).

### 2.2 stdio — `python -m app.mcp_stdio`

Newline-delimited JSON-RPC on stdin/stdout. **Nothing but JSON-RPC may ever be written to stdout** — all logging goes to stderr, and `logging.basicConfig` must be reconfigured to `stream=sys.stderr` before any import that might log. This is the single most common way a stdio MCP server breaks.

`backend/app/mcp_stdio.py` reuses the exact same tool registry and DB layer as the HTTP transport; only the framing differs. It opens the vault DB **read-only** and does not start uvicorn, the indexer, or any executor — it is a thin, fast, side-effect-free process.

Client configuration published in the README:
```json
{ "mcpServers": {
    "geekatplay-vault": {
      "command": "C:\\path\\to\\ComfyUIAssetManager\\venv\\Scripts\\python.exe",
      "args": ["-m","app.mcp_stdio"],
      "cwd": "C:\\path\\to\\ComfyUIAssetManager\\backend",
      "env": { "VAULT_DB": "C:\\path\\to\\ComfyUIAssetManager\\backend\\data\\vault.db" }
} } }
```

---

## 3. Handshake

### 3.1 `initialize` (request)
```json
{ "jsonrpc":"2.0","id":1,"method":"initialize",
  "params":{ "protocolVersion":"2025-06-18",
             "capabilities":{"roots":{"listChanged":true},"sampling":{}},
             "clientInfo":{"name":"mcp-client","version":"1.0.0"} } }
```

### 3.2 `initialize` (result)
```json
{ "jsonrpc":"2.0","id":1,
  "result":{
    "protocolVersion":"2025-06-18",
    "capabilities":{
      "tools":{"listChanged":true},
      "resources":{"subscribe":false,"listChanged":true},
      "prompts":{"listChanged":false},
      "logging":{},
      "completions":{}
    },
    "serverInfo":{"name":"geekatplay-comfyui-vault","title":"Geekatplay ComfyUI Asset Vault","version":"2.0.0"},
    "instructions":"Local ComfyUI installation manager: models, node packages and node classes, workflows, and generated outputs. Use vault_stats first to see scale, vault_search for open-ended questions, and the list_*/get_* tools for precise lookups. This server CAN modify the library — rename, move, delete (trash-backed by default), and organize assets. Destructive operations are logged and recoverable from trash unless permanent deletion is explicitly confirmed. Prefer trash over permanent deletion. Never delete more than the user asked for."
  } }
```

**Version negotiation:** if the client's `protocolVersion` is unsupported, respond with the **newest version this server supports** rather than an error, and let the client disconnect if it disagrees. Supported: `2025-06-18`, `2025-03-26`, `2024-11-05`.

### 3.3 `notifications/initialized`
No `id`. → **HTTP `202`, empty body** (stdio: no output at all). Before this arrives, only `initialize` and `ping` are served; anything else → `-32002 Server not initialized`.

### 3.4 Other required methods
| Method | Behaviour |
|---|---|
| `ping` | `{"result":{}}` — always, even pre-initialize |
| `tools/list` | paginated via `cursor`; returns all 26 tools |
| `tools/call` | see §5 |
| `resources/list`, `resources/read`, `resources/templates/list` | see §6 |
| `prompts/list`, `prompts/get` | see §7 |
| `logging/setLevel` | accepts and applies; `{"result":{}}` |
| `completion/complete` | argument autocomplete for `category`, `base_model`, `package` |
| anything else | `-32601 Method not found` |

### 3.5 Server→client notifications (over the GET SSE stream)
`notifications/tools/list_changed` — never fires (static tool set).
`notifications/resources/list_changed` — after a scan completes.
`notifications/progress` — during `vault_reindex`.
`notifications/message` — logging, when `logging/setLevel` has been called.

### 3.6 JSON-RPC error codes
| Code | When |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request |
| `-32601` | Method not found — **never for tool errors** |
| `-32602` | Invalid params (schema violation on `tools/call` envelope) |
| `-32603` | Internal error |
| `-32001` | Invalid/expired session (HTTP `404`) |
| `-32002` | Server not initialized |

---

## 4. Type conventions used by every schema

```
Uid        : string, pattern "^(model|node_package|node_class|workflow|output):[0-9]+$"
Bytes      : integer, minimum 0
EpochMs    : integer
Limit      : integer, minimum 1, maximum 200, default 25
Offset     : integer, minimum 0, default 0
BaseModel  : enum (API_CONTRACT.md §16)
ModelRole  : enum (API_CONTRACT.md §16)
HashState  : enum (API_CONTRACT.md §16)
```
Every `inputSchema` and `outputSchema` sets `"type":"object"`, `"additionalProperties": false`, and declares `"$schema": "http://json-schema.org/draft-07/schema#"`. Unknown properties are a hard `-32602` — this catches agent hallucination early instead of silently ignoring a filter.

---

## 5. Tools

**26 tools.** The 13 specified below (§5.1–§5.13), the 8 file-operation tools defined in
`DECISIONS.md` C5.2 (`vault_rename`, `vault_move`, `vault_delete`, `vault_trash_list`,
`vault_trash_restore`, `vault_trash_empty`, `vault_create_folder`, `vault_assign_tags`),
the 3 job-control tools C5.2 promotes to writable — `vault_hash_enqueue`,
`vault_hash_cancel`, `vault_embeddings_rebuild` (§5.14–§5.16 below) — and the 2 workflow
"Enable" tools `REQUIREMENTS_R2.md` C9.9 requires (§5.17–§5.18).

> These three are **first-class tools**, not arguments on `vault_reindex`: `tools/list` is
> how an agent discovers a capability, and nothing would lead it to guess that hashing hides
> behind a tool named "reindex". 13 + 8 + 3 = **24**, and C9.9's two Enable tools bring the
> shipped catalogue to **26**.

A read-only surface was the safer starting option; full access was chosen deliberately instead,
after weighing the risk explicitly (see DECISIONS.md C5). **That decision stands** — the mutating
tools are implemented completely, not narrowed back. The safety rails in DECISIONS.md C5
(trash-by-default, `confirm:true` for permanent deletion, `mcp_audit` logging, 200-item batch
cap, uid-only input, roots enforced via `core/pathsafe.py`) are mandatory and mirror the UI's
own semantics, so an MCP client is never more dangerous than a user click.

### 5.0 Common result envelope

Every `tools/call` returns **both** a human-readable text block and machine-readable structured content:

```json
{ "jsonrpc":"2.0","id":7,
  "result":{
    "content":[{"type":"text","text":"Found 34 FLUX.1 models (1.02 TB).\n1. flux1-dev-fp8 …"}],
    "structuredContent":{ "...matches outputSchema..." },
    "isError":false } }
```
Tool-level failures (not found, bad filter, feature unavailable) are `"isError": true` with an explanatory text block — **HTTP 200, JSON-RPC result, not a JSON-RPC error.**

The `text` block is a compact, token-efficient rendering (never a `json.dumps` of the whole payload — the current implementation dumps entire tables with `indent=2`, which can be hundreds of KB). Rule: text block ≤ 4,000 characters, truncated with `… (N more; increase offset)`.

---

### 5.1 `vault_search`
Hybrid semantic + lexical search across every asset type. The first tool an agent should reach for.

```json
{ "name":"vault_search",
  "title":"Search the vault",
  "description":"Search models, node packages, node classes, workflows and outputs by meaning and keyword. Use this for open-ended questions like 'a lora for anime style on FLUX' or 'workflows that do video upscaling'. Falls back to keyword-only search when the semantic index is unavailable.",
  "inputSchema":{
    "$schema":"http://json-schema.org/draft-07/schema#","type":"object",
    "properties":{
      "query":{"type":"string","minLength":1,"maxLength":512,"description":"Natural-language or keyword query."},
      "kinds":{"type":"array","items":{"enum":["model","node_package","node_class","workflow","output"]},
               "description":"Restrict to these asset kinds. Omit for all."},
      "semantic":{"type":"boolean","default":true,"description":"Use hybrid semantic ranking. Ignored (treated as false) when the embedding model is not installed."},
      "limit":{"type":"integer","minimum":1,"maximum":100,"default":20},
      "offset":{"type":"integer","minimum":0,"default":0}
    },
    "required":["query"],"additionalProperties":false },
  "outputSchema":{
    "$schema":"http://json-schema.org/draft-07/schema#","type":"object",
    "properties":{
      "query":{"type":"string"},
      "mode":{"enum":["lexical","hybrid"]},
      "semantic_available":{"type":"boolean"},
      "semantic_unavailable_reason":{"type":["string","null"]},
      "total":{"type":"integer"},
      "results":{"type":"array","items":{"type":"object","properties":{
        "uid":{"type":"string"},"kind":{"type":"string"},"title":{"type":"string"},
        "subtitle":{"type":["string","null"]},"snippet":{"type":["string","null"]},
        "score":{"type":"number"},"matched":{"type":"array","items":{"type":"string"}},
        "path":{"type":["string","null"]}},
        "required":["uid","kind","title","score"],"additionalProperties":false}}},
    "required":["query","mode","semantic_available","total","results"],"additionalProperties":false },
  "annotations":{"readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false} }
```

### 5.2 `list_models`
```json
{ "name":"list_models","title":"List models",
  "description":"List and filter installed models (checkpoints, LoRAs, VAEs, text encoders, ControlNets, upscalers, embeddings). Returns summary rows; call get_model for full detail.",
  "inputSchema":{"$schema":"…","type":"object","properties":{
     "category":{"type":"array","items":{"type":"string"},"description":"Folder categories, e.g. checkpoints, loras, vae, diffusion_models, text_encoders, controlnet."},
     "base_model":{"type":"array","items":{"type":"string"},"description":"e.g. SDXL, FLUX.1, WAN, SD1.5."},
     "role":{"type":"array","items":{"type":"string"}},
     "modality":{"type":"array","items":{"enum":["image","video","audio","3d","multimodal","text","unknown"]}},
     "precision":{"type":"array","items":{"type":"string"}},
     "hash_state":{"type":"array","items":{"enum":["unhashed","queued","hashing","done","failed","stale"]}},
     "is_adapter":{"type":"boolean"},
     "has_update":{"type":"boolean"},
     "integrity_issues_only":{"type":"boolean","default":false},
     "name_contains":{"type":"string"},
     "min_size_bytes":{"type":"integer","minimum":0},
     "max_size_bytes":{"type":"integer","minimum":0},
     "sort":{"enum":["name","-name","size","-size","modified","-modified","params","-params"],"default":"name"},
     "limit":{"type":"integer","minimum":1,"maximum":200,"default":50},
     "offset":{"type":"integer","minimum":0,"default":0}},
   "additionalProperties":false},
  "outputSchema":{"$schema":"…","type":"object","properties":{
     "total":{"type":"integer"},"returned":{"type":"integer"},"offset":{"type":"integer"},
     "total_bytes":{"type":"integer"},
     "models":{"type":"array","items":{"type":"object","properties":{
        "uid":{"type":"string"},"name":{"type":"string"},"filename":{"type":"string"},
        "category":{"type":"string"},"role":{"type":"string"},
        "base_model":{"type":"string"},"base_model_confidence":{"type":"number"},
        "modality":{"type":"string"},"precision":{"type":["string","null"]},
        "params_display":{"type":["string","null"]},"size_bytes":{"type":"integer"},
        "hash_state":{"type":"string"},"autov2":{"type":["string","null"]},
        "integrity":{"type":"string"},"has_update":{"type":"boolean"},
        "workflow_count":{"type":"integer"},"output_count":{"type":"integer"},
        "rel_path":{"type":"string"}},
        "required":["uid","name","category","role","base_model","size_bytes","hash_state"],
        "additionalProperties":false}}},
   "required":["total","returned","models"],"additionalProperties":false},
  "annotations":{"readOnlyHint":true,"idempotentHint":true,"openWorldHint":false} }
```

### 5.3 `get_model`
Input `{"uid"?: Uid, "name"?: string}` — exactly one required (`oneOf`). `name` does a case-insensitive exact-then-fuzzy match; multiple hits → `isError:true` listing candidates.

Output covers the whole DETAILS panel: `identity`, `technical` (tensor_count, components[], precision, quantization, prediction_type, detection{source,confidence,signals[]}), `files[]`, `hash{state,sha256,autov2}`, `civitai{state,url,description,trigger_words,recommended_settings,rating}`, `update{has_update,latest_version_name,benefits}`, `usage_notes`, `download{url,source}`, `usage{workflow_count,output_count,workflows[]}`, `integrity{status,note}`, `tags[]`, `abs_path`.

Explicit behaviour when unhashed:
```json
"civitai":{"state":"none","reason":"not_hashed",
           "note":"Civitai lookup requires the full-file SHA-256. Hashing is a background job started from the app UI; this server cannot start it."}
```

### 5.4 `list_node_packages`
Input: `official` (bool), `enabled` (bool), `has_update` (bool), `author`, `name_contains`, `extraction_status`, `sort` (`name|-classes|-updated`), `limit`, `offset`.
Output per package: `uid`, `folder_name`, `display_name`, `author`, `description`, `is_official`, `enabled`, `class_count`, `extraction{status,strategies,confidence}`, `repo{url,suspect,branch,commit,commit_at}`, `update{state,has_update,commits_behind}`, `version`, `deps[]`, `workflow_count`.
Includes the synthetic `__comfyui_core__` package (638 classes) unless `official:false`.

### 5.5 `list_node_classes`
Input: `package_uid`, `category`, `name_contains`, `official` (bool), `include_deprecated` (default `false`), `confidence` (`declared|inferred|registry`), `limit`, `offset`.
Output per class: `uid`, `node_id`, `display_name`, `class_name`, `category`, `description`, `package{uid,name,official}`, `inputs{required,optional}`, `outputs{types,names}`, `output_node`, `flags{deprecated,experimental,api_node}`, `confidence`, `source{file,lineno,strategy}`, `workflow_count`.

**This is the tool that makes the vault genuinely useful to an agent building ComfyUI graphs** — it exposes ~1,580 node signatures (638 official + ~943 custom), which is exactly what an agent needs to author a valid workflow.

### 5.6 `get_node_class`
Input `{"node_id": string, "package_uid"?: Uid}`. Returns the full class record plus `workflows_using[]` and `similar_classes[]` (same category). Ambiguous `node_id` across packages → all matches with `isError:false` and a `disambiguation` array.

### 5.7 `list_workflows`
Input: `name_contains`, `folder`, `base_model`, `runnable` (bool), `uses_node_class`, `uses_model_uid`, `capability_tag`, `sort`, `limit`, `offset`.
Output per workflow: `uid`, `name`, `rel_path`, `folder`, `format`, `title`, `description`, `capability_tags[]`, `base_model`, `modality`, `node_count`, `missing_node_count`, `missing_model_count`, `is_runnable`, `prompt_summary`, `modified_at`, `size_bytes`.

### 5.8 `inspect_workflow`
The headline tool. Input `{"uid"?: Uid, "name"?: string, "include_graph"?: bool (default false), "max_nodes"?: int (default 200)}`.

```json
{ "workflow":{"uid":"workflow:12","name":"…","rel_path":"…","format":"ui",
              "description":"…","capability_tags":["video","character-replacement"],
              "base_model":"WAN","modality":"video","node_count":84,"is_runnable":false},
  "prompts":{"positive":"…","negative":"…","unresolved_count":2},
  "nodes":[{"class_type":"WanVideoSampler","count":1,"status":"satisfied",
            "package":{"uid":"node_package:9","name":"ComfyUI-WanVideoWrapper","official":false}}],
  "dependencies":{
    "summary":{"total":14,"satisfied":12,"missing":2,"ambiguous":0},
    "models":[{"ref_name":"wan2.2_i2v_high_noise.safetensors","category":"diffusion_models",
               "via_class":"UNETLoader","via_input":"unet_name","status":"missing",
               "uid":null,"suggestions":[{"uid":"model:88","name":"wan2.2_i2v_low_noise.safetensors","similarity":0.82}]}],
    "nodes":[{"class_type":"SomeUnknownNode","status":"missing",
              "install_hint":{"package":"ComfyUI-Foo","repo_url":"https://github.com/x/ComfyUI-Foo",
                              "source":"comfyui_manager_registry"}}],
    "embeddings":[],"input_files":[]},
  "can_run":false,
  "blockers":["2 model files are missing","1 node class is not installed"],
  "graph":null }
```
`install_hint` is resolved from ComfyUI-Manager's `extension-node-map.json` (5,590 repos), which is how an agent can answer "what do I need to install to run this?" rather than merely "something is missing."

### 5.9 `find_model_usage`
Input `{"uid"?: Uid, "name"?: string, "include_outputs"?: bool (default true), "limit"?: Limit}`.
Output: `model{uid,name}`, `workflows[]` (with `occurrences`, `via[]`, `match_method`), `outputs{count, recent[]}`, `co_occurring_models[]` (models that appear in the same workflows — genuinely useful for "what pairs with this LoRA").

### 5.10 `query_outputs`
Search generated outputs by generation metadata.
Input: `prompt_contains`, `negative_contains`, `model_uid`, `model_name_contains`, `workflow_uid`, `media_kind[]`, `sampler`, `seed`, `steps_min`/`steps_max`, `cfg_min`/`cfg_max`, `width_min`/`width_max`, `height_min`/`height_max`, `created_after`/`created_before` (EpochMs), `folder`, `favorite`, `min_rating`, `has_metadata`, `sort`, `limit`, `offset`.
Output per item: `uid`, `filename`, `rel_path`, `media_kind`, `width`, `height`, `duration_ms`, `size_bytes`, `created_at`, `positive_prompt`, `negative_prompt`, `seed`, `steps`, `cfg`, `sampler`, `scheduler`, `model_name`, `model_uid`, `workflow_uid`, `loras[]`, `unresolved_fields[]`.

`unresolved_fields[]` names the metadata fields that were link-valued and could not be resolved (B1 transparency) — an agent must know that a `null` prompt means "was a graph link", not "was empty".

### 5.11 `vault_stats`
No required input; optional `{"breakdown": ["category","base_model","modality","hash_state","media_kind"]}`.
Output: the whole `v_vault_stats` view plus requested breakdowns, `roots[]`, `last_scan{}`, `capabilities{semantic_search, civitai, ollama, hashing_available}`, and `disk{models_bytes, outputs_bytes, thumb_cache_bytes}`.
**This is the recommended first call** — the `instructions` string says so.

### 5.12 `get_index_status`
Returns the live scan/hash/embedding state so an agent can tell "the vault has 0 models" (nothing indexed yet) from "the vault has no models" (genuinely empty) — a distinction the current server cannot make.
Output: `configured`, `comfyui_path`, `scan{active, phase, done, total, last_completed_at, error_count}`, `hash{queued, running, done, percent}`, `embeddings{state, embedded, pending}`, `health{status, issues[]}`.

### 5.13 `vault_reindex`
A mutating tool (see also the 8 file-operation tools in `DECISIONS.md` C5.2). Input `{"mode": "incremental"|"full" (default "incremental"), "phases"?: string[], "wait"?: bool (default false)}`.
Scan control only — hashing and embeddings have their own tools (§5.14–§5.16); there is exactly one way to reach each capability.
`annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}`.
With `wait:false` → returns `{"job_id":19,"started":true}` immediately.
With `wait:true` → the HTTP response becomes an SSE stream emitting `notifications/progress` (`progressToken` echoed from `params._meta.progressToken`) and finally the result. On stdio, progress notifications are written as normal JSON-RPC notification lines.
**Disabled by default when the server is reachable from anything other than loopback** (§9), and always disabled in `--read-only` mode.

### 5.14 `vault_hash_enqueue`
Queues the background full-file SHA-256 / AutoV2 job (DECISIONS C1). Delegates to the same
`HashService.enqueue` that `POST /api/v1/hash/enqueue` calls.
Input `{"scope": "all"|"unhashed"|"category"|"folder"|"ids" (default "unhashed"), "category"?, "folder"?, "uids"? (≤200 model uids), "root_id"?, "priority"? (0–100, default 5)}`.
`scope:"category"` requires `category`, `scope:"folder"` requires `folder`, `scope:"ids"` requires `uids`.
Output `{batch_id, queued, bytes_total, eta_ms, scope, message, audit_id}`.
`annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}`.
The description must state plainly that the whole library is roughly a 2.8-hour job, so an agent scopes it rather than queueing everything.

### 5.15 `vault_hash_cancel`
Input `{"batch_id"?: string, "uids"?: string[] (≤200)}`. With neither, the whole queue is cancelled. Hashes already computed are kept.
Output `{cancelled, batch_id, message, audit_id}`.
`annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}`.

### 5.16 `vault_embeddings_rebuild`
Recomputes the local ONNX embedding index behind the Smart toggle (DECISIONS C2). Delegates to
`EmbedService.rebuild`, exactly as `POST /api/v1/embeddings/rebuild` does.
Input `{"kinds"?: ("model"|"node_package"|"node_class"|"workflow"|"output")[], "force"?: bool (default false)}`.
Output `{state, started, embedded, pending, message, audit_id}`.
`annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}`.
When the model is not installed the result is `isError:true` carrying the reason — never a JSON-RPC error, and never a silent no-op.

All three write an `mcp_audit` row and are refused in `--read-only` / `mcp_read_only=true` mode.

### 5.17 `enable_workflow_plan`  *(REQUIREMENTS_R2 C9)*

The dependency report for one workflow, and the **only** way to obtain the `plan_token` that
`enable_workflow_fetch` requires. Read-only: it opens no socket and writes no file. Delegates to the
same `app/enable/report.py` that `GET /api/v1/workflows/{id}/enable/plan` serves.

Input `{"workflow_uid": "workflow:<id>"}` — uid only, exactly like every other tool. There is no
argument for a URL, a path, a host or a folder.

Output `{workflow, summary, space, models[], node_packages[], plan_token, plan_expires_in_ms,
plan_items, policy, generated_at}` — see API_CONTRACT §20 for the field-by-field shape. Each item
carries its derived `destination`, its `source` (URL, host, size, whether a hash is published) and a
`status` of `fetchable` · `already_present` · `no_source` · `blocked`.

`annotations: {"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}`.

The text block ends with an instruction to the agent: show this to the user and let *them* choose
`item_ids`. An agent that fetches everything it sees has not obtained consent.

### 5.18 `enable_workflow_fetch`  *(REQUIREMENTS_R2 C9)*

Downloads the selected items into the correct ComfyUI folders and clones the selected node packages.
Delegates to `EnableService.fetch`, the same entry point `POST /api/v1/workflows/{id}/enable/fetch`
uses — no parallel implementation.

Input `{"workflow_uid": "workflow:<id>", "plan_token": str, "item_ids": str[1..200],
"confirm": true, "on_conflict"?: "fail"|"skip"|"keep_both"}`.

Output `{batch_id, workflow_id, queued, bytes_total, items[], stream, started_at, scan_job_id,
audit_id}`.

`annotations: {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": true}`
— `openWorldHint` is `true` because this is the one tool that reaches the public internet.

Every rule from SECURITY_REVIEW §5 applies to the MCP path with no relaxation whatsoever (R10):

* **uid input only** — never a URL, never a filesystem path. Sources come from the workflow's own
  model manifest, a `download_url` the vault already cached, or the ComfyUI-Manager registry.
* **`confirm: true` is mandatory** and the `plan_token` requirement is **not** waived for agents. A
  stale, superseded or foreign token is a tool error and issues no request.
* **200-item batch cap**, the same cap every other mutating tool carries.
* **`mcp_audit` row** with argument values, per DECISIONS C5 rail 3.
* **Refused in `mcp_read_only=true` / `--read-only` mode**, like every mutating tool.
* **Allowlisted hosts only**, redirects re-validated per hop, destination derived server-side,
  verify-then-place with quarantine on mismatch, free space checked first.
* **No installer is ever run** — not `pip install`, not `requirements.txt`, not `install.py`, not a
  post-clone hook, and submodules are never fetched. The exact command is returned for the user.


---

## 6. Resources

Resources give an agent bulk context without a tool round-trip.

| URI | Name | MIME | Content |
|---|---|---|---|
| `vault://stats` | Vault statistics | `application/json` | same as `vault_stats` |
| `vault://models/index` | Model index | `application/json` | compact array: `[uid, name, category, base_model, size]` for every model (231 rows ≈ 18 KB) |
| `vault://nodes/index` | Node class index | `application/json` | `[node_id, display_name, category, package]` for all ~1,580 classes ≈ 120 KB |
| `vault://workflows/index` | Workflow index | `application/json` | 48 rows |
| `vault://health` | Health report | `application/json` | `/system/health` payload |

Templates (`resources/templates/list`):
| URI template | Content |
|---|---|
| `vault://model/{id}` | full `get_model` payload |
| `vault://workflow/{id}` | full `inspect_workflow` payload |
| `vault://workflow/{id}/graph` | raw graph JSON |
| `vault://node-class/{node_id}` | full class record |

`resources/read` returns `{"contents":[{"uri":"…","mimeType":"application/json","text":"…"}]}`.
`subscribe` is **not** supported (`"subscribe": false`); `notifications/resources/list_changed` fires after each scan so clients can re-read.

---

## 7. Prompts

| Name | Arguments | Purpose |
|---|---|---|
| `diagnose_workflow` | `workflow` (required, completable) | "Inspect this workflow, list every missing dependency, and tell me exactly what to install." |
| `recommend_model` | `goal` (required), `base_model` (optional, completable) | "Given what's installed, recommend models for this goal and explain why." |
| `vault_overview` | none | "Summarize this ComfyUI installation: scale, what it can do, and what is broken." |
| `find_unused` | `kind` (optional) | "Which models are indexed but referenced by no workflow and no output?" |

`completion/complete` serves argument values for `workflow` (workflow names), `base_model`, `category`, and `package` — capped at 100 values with `hasMore`.

---

## 8. Implementation layout

```
backend/app/mcp/
  __init__.py
  protocol.py     # JSON-RPC envelope, error codes, version negotiation, session store
  registry.py     # TOOLS: list[ToolDef]; RESOURCES; PROMPTS. Pure data + handler refs.
  handlers.py     # one function per tool; takes (params, ctx) -> (text, structured)
  http.py         # FastAPI router: POST/GET/DELETE /api/v1/mcp
  stdio.py        # newline-delimited loop
backend/app/mcp_stdio.py   # `python -m app.mcp_stdio` entrypoint
```

Rules:
* `handlers.py` calls the **same service layer** as the REST routers (`services/models_query.py`, `services/workflow_query.py`, …). No SQL is duplicated between MCP and REST — divergence between the two surfaces is the failure mode to avoid.
* Read handlers receive a **read-only** connection (`mode=ro`). Mutating handlers
  (`vault_reindex` + the C5.2 file-operation tools) receive a normal read-write connection and
  MUST route through the existing `services/file_ops.py` / `IndexerService` — never their own SQL
  or their own filesystem calls. Every mutating handler writes an `mcp_audit` row.
* Every handler is wrapped: an unexpected exception becomes `isError:true` with `"Internal error (request <id>); see server log."` — never a traceback, never a JSON-RPC `-32603` leak to the agent.
* Response size cap: `structuredContent` is capped at **512 KB** serialized; beyond that the handler reduces `limit` and sets `"truncated": true` in the payload.

---

## 9. Security posture

* **Bind loopback only.** The HTTP transport inherits uvicorn's `127.0.0.1` bind. If `ALLOW_LAN=1` is ever set, `/api/v1/mcp` requires `Authorization: Bearer <token>` where the token is generated on first launch and shown in Settings; without a token the MCP router refuses to mount and logs a warning.
* **Origin validation** on every MCP HTTP request: reject when `Origin` is present and is not `http://localhost:*` / `http://127.0.0.1:*`. This blocks DNS-rebinding attacks against a loopback MCP server — the canonical Streamable-HTTP threat.
* **No filesystem paths as input.** Tools take `uid` or `name`; the server resolves paths internally. Absolute paths appear only in *output*, and only for assets already in the index.
* **No file content is served.** There is no `read_file` tool and no way to exfiltrate a checkpoint through MCP.
* **Mutation is enabled by default** per `DECISIONS.md` C5: rename, move, delete (trash-backed),
  trash restore/empty, create folder, tag assignment, and job control. `--read-only` (stdio) /
  `mcp_read_only=true` (config) switches the server back to a read-only surface; default is off.
* Permanent deletion requires `confirm:true`; trash is the default mode. A single mutating call
  affects at most 200 items. All mutations are recorded in `mcp_audit` **including argument
  values** — the deliberate exception to the no-argument-logging rule below, which still
  applies to read tools. The owner reads that log in Settings → Activity, over the read-only
  `GET /api/v1/mcp/audit` (API_CONTRACT §21); no route can edit or delete a row.
* **No process may be started from MCP, ever.** Two REST routes can start a program — the
  ComfyUI updater and the ComfyUI launcher behind the open-workflow route (API_CONTRACT §19) —
  and neither is exposed as a tool, in either direction, in any mode. Both require a
  confirmation field that repeats the resolved absolute path of the executable, and no
  agent-supplied confirmation can be trusted to be a person. The C5 grant is deliberately a
  grant over the *library*: an agent may destroy and restore vault data, which is recoverable
  and audited, and may not run code on the owner's machine, which is neither. The same rule
  covers the one write into the ComfyUI installation (copying a workflow into the user
  workflows folder), which is likewise REST-only.
* **No network egress on behalf of the agent.** Tools never trigger a Civitai/GitHub/Ollama call; they report cached state only. An agent cannot use the vault as an SSRF pivot.
* **Rate limiting:** 120 tool calls/minute per session; over → `isError:true` with `retry_after_ms` (not a JSON-RPC error, so the agent can back off gracefully).
* Logging: every tool call logs `{session, tool, arg_keys, elapsed_ms, result_bytes, is_error}` — **argument values are not logged** (prompts may be sensitive).

---

## 10. Conformance checklist (owned by the `qa` agent)

1. `initialize` → correct `protocolVersion`, `capabilities`, `serverInfo`, `instructions`; `Mcp-Session-Id` header present.
2. `notifications/initialized` → HTTP 202, zero-length body.
3. Any method before `initialized` (except `initialize`/`ping`) → `-32002`.
4. `tools/list` → **26** tools (13 + 8 file-operation + 3 job-control + 2 workflow-Enable); every schema validates against JSON Schema draft-07; every tool has `outputSchema` and `annotations`; exactly **12** carry `readOnlyHint:false` and exactly 4 carry `destructiveHint:true`. `enable_workflow_fetch` is the only tool with `openWorldHint:true`.
5. `tools/call` with a bad argument → `result.isError === true`, HTTP 200, **no** `error` member.
6. `tools/call` with an unknown tool name → `isError:true` (not `-32601`).
7. Unknown JSON-RPC method → `-32601`.
8. Batch request → array response, order-independent, notifications omitted from the array.
9. Missing/invalid `Mcp-Session-Id` → HTTP 404 + `-32001`.
10. `Origin: http://evil.test` → HTTP 403.
11. stdio: stdout contains **only** valid JSON-RPC lines across a full session (assert by parsing every line); all log output lands on stderr.
12. `DELETE /api/v1/mcp` terminates the session; the next request with that id → 404.
13. Every tool returns identical data to its REST counterpart for the same filters (parity test).
14. Cold stdio start-to-first-response < 800 ms.
