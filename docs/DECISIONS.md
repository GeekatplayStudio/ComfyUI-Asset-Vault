# Product Decisions

The confirmed product-behaviour decisions this build follows, and why. Where another document
(ARCHITECTURE.md, DATA_MODEL.md, API_CONTRACT.md, MCP_SPEC.md, BUILD_PLAN.md) states something
that conflicts with an entry here, this file is the one to trust — update the other document to
match rather than the reverse. See also [REQUIREMENTS_R2.md](REQUIREMENTS_R2.md) for the second
round of decisions (C6–C11), same standing as the ones below.

## Product decisions

### C1 — Hashing: background, opt-in, resumable

Indexing never blocks on hashing. Scans use local data only (safetensors headers, filesystem
metadata). Full-file SHA-256 / AutoV2 runs as a separate cancellable background job the user
starts, scoped per-folder/category or per-model. Cached and keyed on `(path, size, mtime)`;
invalidated when that fingerprint changes. The queue survives app restarts. Per-model hash state
(`unhashed | queued | hashing | done | failed | stale`) is surfaced in the UI, and a model card is
fully usable before its hash exists. Civitai enrichment fills in progressively.

Expectation to state plainly in the UI: the full 1.5 TB library is roughly a 2.8-hour job.

### C2 — Embeddings: bundled local ONNX, CPU-only

`Xenova/all-MiniLM-L6-v2` INT8, 384 dims, ~23 MB, via `onnxruntime` + `tokenizers`. No torch.
Downloaded on first explicit enable; manual offline placement supported and documented.
Embeddings are persisted, computed incrementally, and never recomputed per query.

Hybrid ranking = FTS5 lexical + vector cosine fused with RRF. The **Smart** toggle selects hybrid;
off is pure lexical. If onnxruntime or the model is unavailable, search silently falls back to
FTS5 and the UI shows Smart as unavailable with a reason — never an error toast.

### C3 — UI: reference layout, original Geekatplay identity

Keep the proven structure (left album/group tree with counts, top toolbar with search + Smart
toggle + grouping dropdown + facet chips + actions, center grid, right DETAILS panel, bottom
status bar with count + per-page selector). Original palette, typography, iconography, spacing,
and branding under **Geekatplay Studio — Vladimir Chopine**. Must not read as a reskin of the reference.

### C4 — Palette: Studio Graphite + Signal Amber + Vault Violet

Confirmed as specified in ARCHITECTURE §7.1. Cyan is retired.

The amber/violet split is a **functional convention, not decoration**:
**amber = verified local data read from your files; violet = AI-inferred or low-confidence data.**

### C5 — MCP: FULL file-operation access, including delete  ⚠️ OVERRIDES D7

**Deliberately reverses the safer, read-only-by-default option.** The risk was weighed
explicitly — a hallucinated tool call can destroy models that take hours to re-download — and full
access was chosen anyway, with real rails instead of a blanket restriction. Do not silently narrow
it back to read-only.

External MCP agents get the complete file-operation set: **rename, move, delete, trash restore,
trash empty, create folder, album/tag assignment, plus hash and embedding job control.**

Required safety rails (these are engineering quality, NOT a reduction of the granted capability —
they mirror what the UI itself does, so an agent is never *more* dangerous than a user click):

1. **Trash-backed by default.** `mode` defaults to `trash`; deletions are recoverable via
   `vault_trash_restore`. This is the same default the UI uses.
2. **`confirm: true` required for `mode: "permanent"`.** Absent → tool error, no deletion.
3. **Audit log.** Every mutating tool call appends to `mcp_audit` (see C5.1) with timestamp,
   session id, tool name, full arguments, affected uids, and outcome. Readable from
   Settings → Activity. Argument *values* ARE logged for mutations (this is the one deliberate
   exception to MCP_SPEC §9's no-argument-logging rule, which continues to apply to read tools).

   **Satisfied — both halves.** Writing: `services/mcp_audit.py`, called from every audited tool.
   Reading: `GET /api/v1/mcp/audit` (API_CONTRACT §21, query service
   `services/queries/mcp_audit_query.py`) and the **Settings → Activity** view
   (`frontend/src/components/activity/`), which leads with a summary — calls, items touched,
   failures, what was called, per-tool table — and opens one entry's full argument values on
   demand. Filters: tool, outcome, transport, session, time window, and free text over the tool
   name and affected uids.

   The read surface is **one route and one verb**. No endpoint creates, edits, prunes or deletes an
   audit row, and the UI offers no control that could: a log the app can erase is not a log. The
   table is append-only for the life of the vault; retention is documented in API_CONTRACT §21 and
   deliberately not implemented. `backend/tests/test_mcp_audit_log.py` asserts both the read
   behaviour and the absence of any write path.
4. **Batch cap.** A single mutating call affects at most 200 items; beyond that → tool error
   instructing the agent to page. Prevents one bad call from touching the whole library.
5. **Roots still enforced.** Every mutation goes through `core/pathsafe.py` and the normal
   `services/file_ops.py`. MCP gets no privileged path. `uid`-only input still holds — no tool
   accepts a client-supplied filesystem path.
6. **`mcp_read_only=true`** (config) and `--read-only` (stdio flag) remain available to switch the
   server back to read-only. Default is **off** — full access is the shipped default.

#### C5.1 — Additional schema

```sql
CREATE TABLE mcp_audit (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,          -- epoch ms
  session_id  TEXT,
  transport   TEXT NOT NULL,             -- 'http' | 'stdio'
  tool        TEXT NOT NULL,
  arguments   TEXT NOT NULL,             -- JSON
  uids        TEXT,                      -- JSON array
  outcome     TEXT NOT NULL,             -- 'ok' | 'partial' | 'error'
  affected    INTEGER DEFAULT 0,
  error_code  TEXT,
  elapsed_ms  INTEGER
);
CREATE INDEX ix_mcp_audit_ts ON mcp_audit(ts DESC);
CREATE INDEX ix_mcp_audit_tool ON mcp_audit(tool, ts DESC);
```

#### C5.2 — New MCP tools (added to the 13 in MCP_SPEC §5, **total 24**)

Each delegates to the SAME `services/file_ops.py` functions the REST routers use — no parallel
implementation, no duplicated SQL. Each carries
`annotations: {"readOnlyHint": false, "destructiveHint": true, "idempotentHint": false, "openWorldHint": false}`
(`vault_create_folder` and `vault_assign_tags` set `destructiveHint: false`).

| Tool | Input | Notes |
|---|---|---|
| `vault_rename` | `uid`, `new_name`, `keep_extension?`, `rename_sidecars?` | single item |
| `vault_move` | `uids[]`, `target_root_id`, `target_folder`, `create_missing?`, `on_conflict?` | ≤200 uids |
| `vault_delete` | `uids[]`, `mode?` (`trash`\|`permanent`), `confirm?` | `permanent` demands `confirm:true` |
| `vault_trash_list` | `limit?`, `offset?` | read-only |
| `vault_trash_restore` | `ids[]`, `on_conflict?` | |
| `vault_trash_empty` | `ids?`, `older_than_days?`, `confirm` | `confirm:true` mandatory |
| `vault_create_folder` | `root_id`, `folder` | |
| `vault_assign_tags` | `uids[]`, `add[]?`, `remove[]?` | metadata only |

Plus these three, promoted to writable job control and exposed as **first-class tools**, not as
arguments on `vault_reindex` — `tools/list` is how an agent discovers a capability:

| Tool | Input | Notes |
|---|---|---|
| `vault_hash_enqueue` | `scope?` (`all`\|`unhashed`\|`category`\|`folder`\|`ids`), `category?`, `folder?`, `uids?`, `root_id?`, `priority?` | delegates to `HashService.enqueue`; ≤200 uids |
| `vault_hash_cancel` | `batch_id?`, `uids?` | neither ⇒ cancel the whole queue |
| `vault_embeddings_rebuild` | `kinds?`, `force?` | delegates to `EmbedService.rebuild` |

These three carry
`annotations: {"readOnlyHint": false, "destructiveHint": false, "openWorldHint": false}`
(`idempotentHint` is `false` for `vault_hash_enqueue`, `true` for the other two). They write
`mcp_audit` rows like every other mutating tool and are refused in read-only mode.

**Count, stated plainly so nobody re-derives it:** 13 (MCP_SPEC §5.1–§5.13) + 8 (the table
above) + 3 (this table) = **24 tools**. MCP_SPEC §10 conformance check 4 expects 24.

#### C5.3 — Spec passages superseded

These MCP_SPEC.md statements are **void**; C5 replaces them:

- §3.2 `instructions` — "All tools are read-only; this server cannot modify, delete, or download anything."
- §5 opening — "13 tools, **all read-only.**" and the paragraph declining `delete_model`/`rename_file`/`move_file`.
- §5.13 — "The only non-read-only tool."
- §8 — "Every handler receives a **read-only** connection… `vault_reindex` is the sole handler with a write path."
- §9 — "**No mutation** except `vault_reindex`."

Replacement `instructions` string:

> "Local ComfyUI installation manager: models, node packages and node classes, workflows, and
> generated outputs. Use vault_stats first to see scale, vault_search for open-ended questions,
> and the list_*/get_* tools for precise lookups. This server CAN modify the library — rename,
> move, delete (trash-backed by default), and organize assets. Destructive operations are logged
> and recoverable from trash unless permanent deletion is explicitly confirmed. Prefer trash over
> permanent deletion. Never delete more than the user asked for."

Security posture that still holds unchanged from MCP_SPEC §9: loopback-only bind, Origin
validation, bearer token when `ALLOW_LAN=1`, uid-only input, no file-content reads, no network
egress on the agent's behalf, rate limiting.

## Foundational decisions (D1–D10)

| # | Decision | Status |
|---|---|---|
| D1 | Port 8000 → **8127** | adopted |
| D2 | DB `asset_vault.db` → **`vault.db`**, config-only legacy import | adopted |
| D3 | ONNX model downloaded on first explicit enable, not vendored | adopted (= C2) |
| D4 | `extra_model_paths.yaml.hold` ignored; `read_held_extra_paths` toggle default OFF | adopted |
| D5 | Scan **both** `user\default\workflows` (20) and root `workflows\` (28) | adopted |
| D6 | No video frame extraction without a user-installed ffmpeg on PATH | adopted |
| D7 | MCP exposes no destructive tools | **REJECTED — see C5** |
| D8 | `/api/v1` replaces `/api/*`; no aliases | adopted |
| D9 | New deps: `PyYAML`, `onnxruntime`, `tokenizers`, dev `pytest-asyncio`, `ruff` | adopted |
| D10 | Studio Graphite + Signal Amber palette | adopted (= C4) |

## Standing constraints

- Stack fixed: Python 3.12 + FastAPI + SQLite + React 18 + Vite. No TypeScript, no ORM, no
  Electron, no cloud service.
- Windows-first. Long paths, non-ASCII/emoji/CJK filenames, and file locking (ComfyUI may be
  running) must all be handled.
- Local-first and offline-capable. Civitai and Ollama are optional and degrade gracefully.
- Branding throughout: **Geekatplay Studio — Vladimir Chopine**.
