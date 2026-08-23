# Changelog

Geekatplay ComfyUI Asset Vault · **Geekatplay — Vladimir Chopine**

---

## 2.0.0 — 2026-08-22

A rebuild, not a release. Version 1 installed cleanly and did nothing: a full scan against a real
ComfyUI installation crashed before the first commit, so **every table was empty**. The baseline
audit is preserved in `AUDIT.md`; the verification of this release is in `QA_REPORT.md`.

Every claim below was confirmed by executing it against a real install of ComfyUI 0.33.0 holding
237 models (1.589 TB), 34 node packages, 1,866 node classes, 211 workflows and 3,834 outputs.

### Fixed — the six defects that made v1 unusable

**B1 · Nothing was ever indexed.** In ComfyUI's API-format metadata a `CLIPTextEncode` text input
is often a link to another node, not a string — `['88:97', 0]`. The scanner bound that list
straight into SQLite and died with `type 'list' is not supported`. **126 of 400** sampled outputs
carried one. Because the commit came only at the very end, models, nodes and workflows already
inserted rolled back with it. Links are now resolved through the graph, and provenance records
`zeroed` and `empty` as distinct states. Prompt-collision count: **0**, from 126 crashes and a
later 102 mis-assignments.

**B2 · Civitai could never match.** AutoV2 is the first 10 hex characters of the **full-file**
SHA-256. The implementation hashed only the first 64 KB after the safetensors header — and read
past EOF, so it returned `E3B0C44298`, the SHA-256 of the empty string, for everything. Every
advertised Civitai feature was dead. Now computed over the whole file, in a background job.
Verified on three files: `flux2-vae-new` → `5628B30A8E`, matching an independent reference.

**B3 · Base-model detection was mostly wrong.** The heuristic looked at `tensor_keys[:100]`, and
safetensors key order is arbitrary, so the discriminating keys were usually outside the window.
`flux1-dev-fp8` was reported as a **VAE**. Detection now uses the full key set with explicit rules
and a stated confidence; `flux1-dev-fp8` reports FLUX.1 checkpoint with **11.9 B** primary
parameters rather than 16.87 B summed across every tensor in the bundle. Models whose architecture
label names a family other than their own: **0**, down from 5.

**B4 · Two thirds of node packages yielded nothing.** Class extraction only understood a literal
`NODE_CLASS_MAPPINGS = {...}` in `__init__.py`. Real suites build the mapping across modules with
imports, `.update()` and merges — so **21 of 32** packages reported zero classes, including
KJNodes, IPAdapter_plus and WanVideoWrapper. That also made "missing nodes" meaningless in
workflow analysis. Extraction now walks the source with Python's AST across the whole package.
**32 of 34** packages yield classes; the two that do not genuinely register none.

**B5 · The launcher never started the backend.** `start_app.bat` passed `--cwd backend` to
uvicorn. There is no such option; the process died on the command line. Now
`--app-dir backend`, on port 8127, with a real wait-for-listening loop, venv and `node_modules`
checks, a port-in-use check, and the tail of `backend_log.txt` printed on failure. The engine
answers `/api/v1/ping` about **one second** after launch.

**B6 · The ComfyUI path desynced on every restart.** The wizard wrote the path to the database,
but only an in-process constant was updated. After a restart the constant reverted to a default
that did not exist while the database held the real path — so re-index scanned nothing, the path
guard rejected **every** rename, move and delete with 403, and no thumbnail rendered. The constant
is gone; one service answers "where is ComfyUI", and every consumer reads it live. Albums stayed
stable at 10 across three real restarts.

### Fixed — security

**S-01 · NTFS junctions escaped every configured root.** A junction created with `mklink /J`
was followed by both directory walkers, so a scan could leave the ComfyUI folder entirely.
Reparse points are now detected and skipped at both sites. Verified by creating a real junction
and confirming the walker refuses to descend.

**S-02 · The MCP endpoint had no CSRF control.** A `text/plain` POST from another loopback
port — ComfyUI's own port serves third-party JavaScript from custom node packages — could call
`vault_delete`. The endpoint now requires `X-Vault-Request: 1` and a matching origin, and returns
**403** for both violations. Legitimate clients on both transports still work.

Both were carrying `xfail` markers. Those are gone, replaced by permanent regression gates: a
failure there is a reopened breach, not an expected condition.

Nine further findings, all Medium or below, are tracked in `SECURITY_REVIEW.md`.

### Changed — decisions that reshaped the product

| | |
|---|---|
| Port **8000 → 8127** | 8000 collides with everything. |
| Database `asset_vault.db` → **`vault.db`** | With a one-time import of the legacy file. |
| `/api/*` → **`/api/v1`** | Versioned, frozen contract, no aliases. |
| Hashing is **opt-in and background** | Reading 1.5 TB cannot happen inside a scan. Resumable, cancellable, cached on `(path, size, mtime)`. |
| Semantic search is **opt-in** | A ~23 MB local ONNX model, CPU only. No torch anywhere in the stack. |
| MCP gained **full file operations** | Reversing the read-only recommendation, deliberately and with rails. |
| Palette **Studio Graphite + Signal Amber + Vault Violet** | Amber for measured facts, violet for inferred ones — a functional convention, not decoration. |

### Added

**A real search engine.** v1 loaded every row of all four tables, rebuilt its vocabulary from
scratch and re-vectorised the whole corpus **on every keystroke**; the FTS5 table it created was
never populated or queried. Now: a maintained FTS5 index of 6,182 documents, and optional vector
embeddings fused with it by reciprocal rank fusion behind the **Smart** toggle. Lexical search
answers in about 9 ms p95, server-side.

**An incremental, parallel, resumable scanner.** Nine named phases with live progress over SSE and
a working cancel. Files unchanged by size and mtime are skipped entirely. Cold full scan
**13.8–17.4 s**; warm incremental **0.31–0.37 s**. Errors are recorded per file and readable at
`/api/v1/index/errors` — a scan never dies on one bad file.

**A thumbnail cache.** v1 would have streamed 3,569 full-size originals through the grid. Grid
scroll over 3,834 outputs now measures a 17.9 ms p95 frame time.

**Storage and maintenance**, a top-level tab. Footprint by category, free space per drive for
every root, and reclaim candidates sortable by **score, size or age** — all three first-class,
because that is what the owner asked for. On the reference install it identifies **100 models
totalling 485 GiB** referenced by no workflow and no output. Duplicates, trash and cleanup live
here, trash-backed, capped at 200 items per action, never without an explicit selection.

**ComfyUI version awareness.** Reads `comfyui_version.py` (0.33.0), the install flavour with its
evidence, the frontend and template package versions, and git state. Checks for a newer release
read-only, degrading to `unknown` offline. **Discovers** the real updater for the install rather
than assuming one — three portable updaters found on the reference machine, with a recommendation
and a note on each. Running one needs confirmation naming the resolved absolute path, is refused
while ComfyUI appears to be running, streams its output and reports its exit code. It is never
automatic and never scheduled.

**Workflow dependency reports.** For each workflow, every missing node class with its registry
repository URL, and every missing model with the category it belongs in — resolved from the node
input that referenced it — plus close matches from your own library, scored and clearly labelled
as guesses. **159 of 211** workflows on the reference install need something.

**Workflow "Enable".** A two-step flow that makes an unrunnable workflow runnable. Step one is a
plan that downloads nothing: every missing item, its resolved destination folder, the total size,
and free space per target drive with a 5% margin. Step two fetches **only** the items you ticked,
against the token from the plan you were actually shown — a stale plan is refused and there is no
fetch-everything shorthand. Sources are limited to Civitai and Hugging Face for models and
registry-declared repositories for packages, with every redirect re-checked. Downloads are
verified by size and, where published, hash; a mismatch is quarantined rather than placed.
`on_conflict` offers fail, skip or keep-both — `overwrite` does not exist on this path. Node
packages are **cloned, never installed**: auto-running an untrusted repository's `requirements.txt`
is remote code execution. Afterwards the workflow is re-checked and the result reported.

**Wider workflow discovery.** Both `user\default\workflows` and the root `workflows\` folder, plus
`custom_nodes/*/workflows` and `*/example_workflows`, plus graphs embedded in outputs. Each
workflow is labelled by origin: yours, an official template, or bundled with a named package.
211 found, against 20 in v1.

**A real MCP server.** 26 tools over stdio and Streamable HTTP, with resources and prompts. Every
mutation is written to an `mcp_audit` row with its arguments, the items it touched and the
outcome.

**The audit log is readable.** `GET /api/v1/mcp/audit` (paged, filterable by tool, outcome,
transport, session, time window and free text, with a summary) and **Settings → Activity**, which
leads with the headline figures and opens one entry's full arguments on demand. Deletes read
differently from other writes, failures from successes. The log is append-only in both directions:
no route and no control can edit or delete a row.

**Path changes that actually work.** Live validation before saving; every consumer — indexer, path
guard, thumbnails, search, MCP — picks up a new path with no restart. Old roots are **retired, not
deleted**: their content keeps ratings, tags, notes and album membership, and is excluded from the
missing-file sweep. `GET /api/v1/comfyui/path-policy` states this in the app's own words.

**Multi-root support** from `extra_model_paths.yaml`, with per-root volume reporting.
`extra_model_paths.yaml.hold` is ignored unless you opt in.

**Windows-first handling** throughout: long paths, non-ASCII / CJK / emoji filenames, and file
locking while ComfyUI is running (`FILE_LOCKED`, HTTP 423, retryable).

### Removed

* `settings.COMFYUI_PATH` — the source of B6.
* `services/workflow_parser.py` — replaced by the graph parsers. Its stale imports were what made
  the test suite die at collection.
* Cyan, from the palette.
* `--cwd`, port 8000, `asset_vault.db` and the unversioned `/api/*` paths, from everywhere
  including the documentation.

### Known open

**`/ping` tail latency during a forced full reindex.** p95 of 96–147 ms against a 50 ms budget,
max 874 ms, with 29 of 235 samples over budget. The median stays at 13.3 ms and warm incremental
scans are unaffected, so the interface stutters rather than stalling. `/ping` touches no database,
so this is event-loop starvation from thread contention, not query cost. It is a regression from
23 ms measured earlier in the build; the likely contributors are the per-entry reparse-point check
added for S-01 and increased worker-thread contention. **It has not been diagnosed to a specific
cause, and the budget has deliberately not been relaxed to hide it.** See
`TROUBLESHOOTING.md` and `QA_REPORT.md`.

**Two launcher tests skip** for environmental reasons — the live test cannot attribute a ping to
the launcher when something already holds 8127.

### Verified state

```
pytest tests           exit 0
ruff check backend/app clean
npm run build          348 kB entry + 64.67 kB storage chunk
```

| | |
|---|---|
| Models | 237 · 1.589 TB |
| Node packages | 34 |
| Node classes | 1,866 (841 official) |
| Workflows | 211 · 159 with missing dependencies |
| Outputs | 3,834 |
| Search documents | 6,182 |
| Albums | 10, stable across restarts |

---

## 1.0.0

The original release. Preserved only as the baseline `AUDIT.md` measures against.
