# Changelog

Geekatplay ComfyUI Asset Vault · **Geekatplay Studio — Vladimir Chopine**

---

## Unreleased

### Added

**Open in ComfyUI.** A workflow can now be opened where it is meant to run, from the details
panel and from the row and card actions in the Workflows tab. If ComfyUI is not running, the
vault offers to start it — and that offer is a separate question with its own answer.

The dialog shows the whole plan before anything happens: whether ComfyUI is answering, which
launcher would be run and its resolved absolute path, the port that launcher pins on its own
command line, the address that will be opened, and, when the file is not already in ComfyUI's
workflows folder, the exact destination a copy would be written to. Starting ComfyUI needs a
confirmation that repeats the launcher path; copying needs a confirmation that repeats the
destination; a copy never overwrites a file that is already there. Clicking Open starts nothing
on its own. Start-up progress is streamed and the tab opens when the port actually answers — a
cold start loads every installed node package, so the wait is measured, not assumed.

**Launcher discovery.** The `run_*.bat` scripts beside the ComfyUI folder are found on disk and
listed with their resolved paths, the same way the updater is discovered rather than assumed.
`main.py` with the interpreter that ships with the install is the fallback.

**Node Registry.** A read-only catalogue (Nodes → Registry) combining the official Comfy Registry
metadata cache with the local ComfyUI-Manager legacy class-to-package map, searchable and
filterable by source and installed state. It never installs anything on its own — installation
stays tied to a workflow's specific missing classes and a reviewed plan. **Resolve missing nodes**
turns that report into an action: it pins the remote's exact commit before showing the plan, clones
into a staging directory with credential helpers, `file://`/`ext::` transports, hooks, tags and
submodules all disabled, verifies the pinned commit after clone, and only then atomically releases
the result — never running `pip`, `requirements.txt`, `install.py`, or any submodule.

**Contextual card faces.** Assets with no real preview image no longer get a randomly-hued
generated gradient. They get a flat card face instead: a thin status bar across the top (green
usable, amber needs attention, red broken or missing), a category-tinted icon for the file kind
(checkpoint, LoRA, VAE, CLIP/text encoder, CLIP-vision, ControlNet, upscaler, embedding, GGUF/UNet,
motion module), and the format spelled out. Thumbnail generation is skipped entirely for assets the
UI now renders as a face — outputs, workflows-with-previews and models-with-previews keep real
generated thumbnails; node packages and preview-less models no longer trigger a server-side render.

**Search match transparency.** Every list result returned by a search now carries why it matched —
*name match*, *text match*, *semantic match*, or *text + semantic* — shown as a badge on the card.
The semantic (embedding) arm previously had no similarity floor, so a nearest-neighbour lookup
always returned its top results even for a query matching nothing at all. It now enforces a cosine
floor before offering a semantic result, configurable from Settings → Search → **Match strictness**
(Strict / Balanced / Loose / Widest), so a query with no real match returns nothing instead of the
least-unrelated files in the library.

**Community rating.** Civitai's rating and download count, already fetched and stored on a
hash-matched model, are now shown — a small star-and-count line on the card and in the details
panel's provenance section.

**More left-rail filters for Models** — Type (checkpoint/LoRA/VAE/…), Precision, and a
personal-rating filter (5★ / 4★+ / 3★+) — plus a **Geekatplay Spotlight** section in the Nodes and
Workflows rails to surface Geekatplay-authored packages and workflows.

**The status bar's integrity warning is now a link.** Clicking "N integrity issues" jumps straight
to the Models tab filtered to the affected files, instead of only announcing the count.

### Fixed — filters, and a staged-clone disk leak

Several grid and album filters were displayed while their query parameter was silently ignored by
the API, so **Missing files** and **Integrity issues** on Models returned the whole library instead
of the filtered one. Every filter across all five asset kinds was re-audited end to end — UI
control → HTTP parameter → SQL predicate — and the gaps closed; the full trace is in
[FILTER_AUDIT.md](FILTER_AUDIT.md). Node Packages and Workflows also gained `sort=-created`, so
**Recently added** works instead of failing with an unsupported-sort error.

A node-package clone staged under `custom_nodes/.vault-staging` was left on disk if the process was
killed mid-clone or an unhandled error occurred between clone and release — nothing ever swept it.
The clone path is now wrapped so a half-finished stage is always cleaned up, and a sweep on the next
clone removes anything still orphaned from a prior crash.

### Security — the launcher review (S-19 … S-24)

The launcher was reviewed the way the updater was, and it needed work. Three things now have to
be true before a file may be offered as a way to start ComfyUI, and the offer is refused with a
reason when they are not:

* the configured folder is a **real install** — `comfyui_version.py`, `main.py` and `models/`,
  the same proof the updater already required. A folder that merely passed the setup check
  could previously nominate, and run, its own batch file;
* the script **says it starts ComfyUI**. `run_*.bat` is matched in the folder *above* the
  ComfyUI folder, which on a portable build is a drive root, so a name match alone is not
  enough. All four of the launchers on the owner's install — including the hand-written one —
  are unaffected;
* the path **holds no character `cmd.exe` would read as syntax**. Windows runs a batch file
  through the command interpreter, which re-reads the whole line, so a launcher named
  `run_a&something.bat` used to run two commands where the dialog showed one file. It is now
  refused, and so is the same shape in an updater's folder.

Copying a workflow into the install is checked against the workflows folder itself rather than
against the vault's roots in general, and is an exclusive create, so "it never overwrites" holds
even for a file that appears while the dialog is open. A launcher script naming an impossible
port no longer strands the start-up state, and a start that cannot be watched now ends and says
so instead of blocking every later one.

### Fixed — Open in ComfyUI, after the first real use

Three defects, all reported from one session and all reproduced before they were changed.

**ComfyUI was already running and the vault offered to start it anyway.** The plan carries
"is ComfyUI running", and the front end cached that plan for the life of the tab — so an
answer measured once, while ComfyUI was down, was replayed for every later open. The dialog
now measures on every open and carries a **Check again** control, and the server measures
again at the moment of the request: a stale plan cannot start a second copy of ComfyUI, and
does not try.

The probe itself is stronger. It reaches **both loopback addresses**, `127.0.0.1` and `::1`,
because a portable launcher passes `--listen 0.0.0.0` and binds only one of them; it also
looks at whatever port the launcher scripts pin, not only 8188 and 8189; and it confirms what
answered by asking ComfyUI's own status endpoint rather than trusting that a taken port is
ComfyUI. The two claims stay separate: *a port is taken* still blocks an update, while *it
answered as ComfyUI* is what decides that nothing needs starting.

**ComfyUI opened, and the workflow did not.** Two causes, one for each half. The vault
declared ComfyUI ready the moment a TCP connection was accepted — before the server was
actually serving — so the address could arrive too early to load anything; readiness now
means ComfyUI answered. And a bundled example graph has an address only while ComfyUI is
loading the package that owns it, which the vault never checked: when ComfyUI is running, the
address is now confirmed against it first, and a graph it is not serving is opened as a plain
ComfyUI window that says which file to pick and why the link could not be used.

**It opened a tab; the owner asked for a window.** It is a window now, sized and centred with
real window features. It is opened once per launch rather than twice, and if the browser
blocks it — which it will, for a window opened when a background task finishes — the dialog
says so plainly and gives a button that opens it from a real click.

### Honest about what ComfyUI supports

The ComfyUI frontend this install serves (1.49.6) can be told by URL to open an official
template or a node package's example graph, and **nothing else** — it has no URL parameter for
a workflow in your own workflows folder. That was established by reading the frontend's own
sources and ComfyUI's server routes, not guessed. So 147 of the 211 indexed workflows open
themselves, and for the rest the vault opens ComfyUI and names the file to pick from the
Workflows sidebar rather than handing over a link that would quietly do nothing.

---

## 2.1.0 — 2026-08-23

Preview and playback. Everything an output folder can hold now shows itself: video and audio play
in place, 3D models render, and text is formatted rather than guessed at.

### Added

**Inline playback for video and audio.** A play button on every playable asset — grid tile, list
row and details panel — that turns into a real player where it stands. The poster stays mounted
until playback is asked for, so a grid of 3,834 outputs never holds thousands of media elements,
only the one being watched.

**One player at a time, anywhere in the app.** Enforced once at the shell by a capture-phase
`play` listener; media events do not bubble, so a document listener only sees them on the way
down. The grid, list, details panel and lightbox therefore need to know nothing about each other.
Pause is pause — including when the coordinator pauses one because another started — and a
separate **stop** button rewinds and hands the poster back.

**Video poster frames** via ffmpeg where it is installed, seeking one second in so the thumbnail
is not the opening black frame. Absent ffmpeg, videos fall back to a placeholder and say so.

**3D preview** for `.glb`, `.gltf` and `.fbx` — orbit, zoom, auto-rotate, framed from the model's
bounding box — in the details panel and the lightbox. Plain `three.js`, dynamically imported: the
entry bundle stays at ~347 kB and `three` (717 kB) never touches the cold path.

**3D poster thumbnails.** The browser hands one rendered frame back to the vault, so the grid gets
a real picture of the model. There is no server-side GL stack; the poster comes out of work the
browser had already done. Accepted only for 3D assets, only as a PNG data URL, under a size cap,
and re-encoded server-side rather than trusted as it arrived.

**Formatted text preview** for `.txt`, `.json` and anything else that decodes as text, with JSON
pretty-printed. Whether a file is text is decided **from its bytes, not its extension**: the `.pt`
tensor files in an output folder are PyTorch pickles, up to a couple of hundred megabytes, and are
reported as binary instead of rendered as mojibake.

**Open in ComfyUI** for any indexed workflow, from the details panel and the Workflows tab. If
ComfyUI is not running the app offers to start it, naming the launcher it discovered, waits for
the port, then opens the workflow. Starting ComfyUI and copying a file into the install are
separate explicit consents; clicking Open does neither on its own.

The deep-link support was established by reading the frontend this install serves, not assumed:
ComfyUI 1.49.6 exposes **no** URL parameter for user-saved workflows — only for templates bundled
with node packages. On this library that is 147 of 211 workflows opening directly and 64 opening
ComfyUI with the filename to pick from its sidebar, and the app says which case applies rather
than handing over a link that silently does nothing.

**Support and profile links** — a compact pair in the top bar, the full set with the copyright in
the rail footer, all from one `services/links.js` so the two cannot drift apart.

### Fixed

**Grid collapsed to one column when switching from list.** `useElementWidth` measured
`clientWidth`, which includes padding; the scroll container is padded in grid mode and flush in
list mode, so the JS column count disagreed with CSS `auto-fill` by exactly one column and the
extra card wrapped into the absolutely positioned row below. Verified across 20 width × tile-size
combinations.

**List rows pitched at a grid card's height.** Row height was measured monotonically, so a ~307 px
card height survived the switch to 34 px list rows. Height is now stored with the geometry it was
measured in, and observed with a `ResizeObserver` rather than sampled once — a one-shot read
landed before thumbnails laid out and cards overlapped on first paint.

**List columns did not line up.** Empty cells were skipped rather than holding their slot, so each
row shifted differently; cells also sized to their own content. Cells now keep their slot and share
a fixed basis. The actions column additionally needed `min-width: 0` — `flex-basis` alone does not
clamp content, so rows with a play button were 6 px wider and dragged the column with them.

**Only the thumbnail was clickable.** Clicking a card's title or size line did nothing. The card
owns the handler now; the thumb's click bubbles to it, so selection still fires once and the thumb
keeps its focus ring.

**Stale thumbnails after a renderer change.** Thumbnails are served `immutable` with a one-year
max-age, so improving the renderer would never have reached an existing client. `THUMB_VERSION`
now appears in the cache key, the ETag and the URL, with a test pinning the backend and frontend
constants together.

**3D placeholders could overwrite a stored poster.** A placeholder fetched after the poster was
stored claimed the same cache slot. Placeholders for 3D models are no longer cached at all.

### Security

Every open finding from the internal audit is closed in this release; each fix replaced its
`xfail` marker with a permanent regression test, and there is no won't-fix.

- **S-03** SSE subscriber cap (32) with a real `503` from all five stream routes.
- **S-04** the updater is now gated on the target actually being a ComfyUI install
  (`comfyui_version.py` + `main.py` + `models/`), so a staged directory cannot nominate its own
  `update_comfyui.bat`.
- **S-05** decompression-bomb limits: a 64 Mpx cap, a header-level size check that records an
  oversized image as a scan error instead of decoding it, and a `formats=` allowlist at every
  `Image.open`. A 321-byte PNG used to allocate ~480 MB.
- **S-06** MCP session cap (64) with least-recently-seen eviction.
- **S-07** an 8 MB request-body limit, enforced before parsing and including chunked bodies with
  no `Content-Length`. The ceiling is 8 MB rather than 2 MB because the 3D poster is 4 MB once
  base64'd — a test pins that relationship so the two cannot drift.
- **S-08** SSRF guard on the Ollama URL, at both the schema and the point of use. Loopback and
  private ranges stay free so a LAN Ollama still works; link-local, public addresses, embedded
  credentials, paths and **all hostnames except `localhost`** are refused, keeping DNS out of the
  trust path. Prompts are never sent to a host that was not approved.
- **S-09** dependency floors raised (`pillow>=12.3.0`, `starlette>=1.3.1`, `fastapi>=0.135`,
  `pydantic>=2.4.0`) and **`python-multipart` removed** — verified unused, and carrying nine
  advisories.
- **QA-3** node mappings defined in a sibling module under a non-standard name are now resolved
  through the import table, so classes are keyed on their registered node_id.
- **pathsafe** filename components are capped at 255 characters instead of failing later as a raw
  `OSError`.

### Changed

- `test_build` measures the **entry** chunk rather than the largest file, and asserts `three` never
  enters the entry graph — picking the biggest file silently made the budget meaningless the
  moment a lazy chunk outgrew the app.
- The `subprocess` allowlist grew from three call sites to four, deliberately and with its own
  tests: `jobs/video_frame.py` is the only module permitted to start ffmpeg, with a fixed argv,
  `shell=False`, a timeout, an output cap, and local paths only.
- New dependency: `three` (frontend only, dynamically imported).

---

## 2.0.0 — 2026-08-22

A rebuild, not a release. Version 1 installed cleanly and did nothing: a full scan against a real
ComfyUI installation crashed before the first commit, so **every table was empty**.

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
`TROUBLESHOOTING.md`.

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

The original release — kept here for the record. Superseded entirely by the 2.0.0 rebuild above.
