# Changelog

Geekatplay ComfyUI Asset Vault · **Geekatplay Studio — Vladimir Chopine**

---

## 2.5.1 — 2026-11-20

### Fixed: the workflow details panel crashed for every workflow

Clicking any workflow or template showed "The details panel hit an error - Cannot access 'C'
before initialization". In 2.5.0, `WorkflowDetails` declared a local `const api` (the "runs on a
paid API" flag). That shadowed the imported `api` service. The loader that fetches the details
then read the local before it was assigned, and the panel died before it could render. The
flag is now `paidApi`, and the same name is changed in `AssetCard` so the trap cannot return.
`WorkflowDetails.test.jsx` renders the panel through the real data hook. It reproduces the
exact error against the old code. Nothing covered this panel before.

With the panel back, **Open in ComfyUI** is reachable again. For a template it opens
`/?template=<name>` in ComfyUI, which loads the graph itself. If ComfyUI is not running, the dialog
offers to start it, naming the exact launcher (`run_nvidia_gpu.bat`) for you to confirm. It then
waits for ComfyUI to answer and opens the template. The card's open icon now says so in its
tooltip.

### Fixed: "the app will not start" after the 2.5 upgrade

The engine was starting; the launcher just stopped waiting. Measured on the reference vault, where
the database is on a spinning hard disk:

- The 2.5 database upgrade builds new indexes over 2.2 million outputs. It took 16 s and left
  1.3 GB in the write-ahead log (`vault.db-wal`).
- `stop_app.bat` ended the engine with `taskkill /F`. That skips the shutdown checkpoint, so the
  log stayed at full size.
- On the next start, SQLite folded the whole log back into the database during the first write,
  with no log line and almost no CPU. That took **15 minutes**.
- `start_app` gave up after 45 seconds and printed "The engine did not start". Its clean-up step
  found nothing listening, so the engine kept running with nobody waiting for it. It came up 15
  minutes later and no browser window opened.

What changed:

- **The launchers wait for a live engine.** `start_app.bat`, `.ps1` and `.sh` keep waiting while
  the engine process is alive, up to an hour, and print a progress line from the log every
  15 seconds. A crash is still reported straight away, with the last 20 log lines. If an engine
  from an earlier launch is still getting ready, the launcher waits for it instead of starting
  a second one on the same database.
- **The engine says what it is doing.** A leftover log over 64 MB is folded explicitly before
  migrations run, with a log line that says why and how long it took. The log is then truncated
  to 0 bytes. Any log a migration writes is folded right after the migration, so an upgrade no
  longer leaves the next start to pay for it.
- **`stop_app` stops cleanly.** New `POST /api/v1/system/shutdown` (requires `X-Vault-Request`,
  not exposed over MCP) starts uvicorn's normal graceful exit, which includes the checkpoint.
  The new `stop_app.ps1` calls it, waits up to five minutes, and forces the stop only as a
  fallback. `stop_app.bat` calls `stop_app.ps1` and keeps `taskkill` only for when PowerShell
  cannot run. `stop_app.sh` now gives SIGTERM five minutes instead of five seconds before it
  escalates.

`tests/test_clean_shutdown.py` adds 12 tests. Docs updated: INSTALL, TROUBLESHOOTING ("It says
'still starting' for minutes"), API_CONTRACT §1, SECURITY_REVIEW §8.5, README.

---

## 2.5.0 — 2026-11-20

### Added — official ComfyUI templates, linked to your models and images

The ~700 templates that ship inside ComfyUI's own Python (`comfyui_workflow_templates*`) were
listed by filename only. They are now indexed **read-only** next to your workflows, so they link
to everything else. USER_GUIDE §5 · DATA_MODEL §22 · ARCHITECTURE §3.9 · SECURITY_REVIEW §8.4.

- **Templates → models.** Every model a template loads — including loaders inside subgraphs —
  resolves against your library through the same matching ladder as your own workflows. A
  template reads **ready** or **needs N models**; a missing model links to where ComfyUI
  downloads it (*Get it from huggingface.co*) and names the `models/` folder it belongs in. Only
  `https` links are kept, and the vault never downloads on its own. On the reference install:
  744 of 1,042 template model references already resolve; 119 of 272 local templates are ready.
- **Images → workflows and templates.** ComfyUI stamps every graph with an id, keeps it when a
  template is opened, and embeds the graph in every PNG. Outputs now link back to the workflow
  file that rendered them (yours first, then bundled, then the template) and to the template they
  came from, even through a saved copy. This also fills **Open workflow** on outputs and **Outputs
  from this workflow** on workflows, which were never populated before. On the reference install
  771 outputs link to a workflow and 719 trace to a template.
- **Your copies.** A workflow saved from a template says *Based on the ComfyUI template …*; the
  template lists your copies.
- **Models → templates.** A model's panel lists the templates that use it, ready ones first.
- **Template details** from ComfyUI's `index.json`: title, description, category, tags, model
  families, model size, local or partner **API**, minimum ComfyUI version, tutorial link, and the
  template's own preview image as its thumbnail.
- **Workflows tab:** a **ComfyUI templates** button, and Source (*Mine / Bundled with nodes /
  ComfyUI templates*), **Ready to run** and **Has outputs** filters. The default list is unchanged:
  your workflows and bundled examples.
- **API:** `GET /workflows` gains `origin`, `ready`, `has_outputs` and `group=origin`; items gain
  `read_only`, `template`, `based_on_template`; detail gains `graph_uuid`, `copies` and a filled
  `outputs_recent`; dependency entries gain `download`. `GET /outputs` gains
  `from_workflow_graph`; output detail gains `workflow`, `template`, `workflow_uuid`.
  `GET /models/{id}/usage` gains `templates`; stats gain `templates`. **MCP** `list_workflows`
  gains `origin` and `ready` inputs and `origin`, `read_only`, `output_count` outputs;
  `vault_stats` gains `counts.templates`.

### Changed

- **Counts stay honest.** Template references are counted in a new `models.template_count`, not
  in `workflow_count`, so **Storage → unused models** still means unused by *your* work. The
  status-bar workflow count and **Needs attention** exclude templates; node packages' "used by"
  counts exclude them too.
- `workflows_broken` no longer counts workflows whose file has gone missing.
- Any workflow's `properties.models` declarations now count as dependencies when the widget walk
  misses them (loaders inside subgraphs), and supply the download link for a missing model.

### Fixed

- The template catalogue (Storage → ComfyUI, `GET /comfyui/templates`) listed `index.json` and
  its 16 translations as if they were templates.

### Upgrade notes

- Schema **v10** (`m010_official_templates`): additive only — three columns, one table, one view
  rebuilt. Measured at 11.8 s on a 2.4 GB vault.
- The first scan after upgrading re-reads all workflows (cheap) and **only the outputs that carry
  a ComfyUI graph** — 4,870 of 2.2 million on the reference library; every other output is
  stamped current by the migration.
- Templates appear after that first scan. Run one from the status bar, or wait for the automatic
  scan at startup.

---

## 2.4.1 — 2026-11-20

A dependency-security refresh and a one-step install.

### Security — dependencies

- **Pillow floor raised to 13.0.0.** Pillow 12.3.0 carries about forty published advisories that
  13.0.0 fixes, and every one sits behind `Image.open` on files the vault did not write. The
  `test_pillow_floor_excludes_known_cves` gate now enforces 13.0.
- The venv was brought to the newest releases that satisfy the requirements, including the
  indirect `certifi` and `filelock` fixes. `pip-audit` of the venv before the refresh listed
  56 advisories (3 packages); after it, none.
- Left alone on purpose: **React 19** (a major migration of its own) and **`lucide-react` 1.x**
  (drops the brand icons the left rail uses). `lucide-react` stays on 0.577.

### Changed — installation is one step

- **`start_app.bat` / `start_app.ps1` / `start_app.sh` install on first run.** With no `venv`
  yet they run the installer themselves and then start the app: double-click, done.
- **Python is installed for you on Windows.** If no Python 3.11+ is found, the installers run
  `winget install Python.Python.3.12` (per-user, silent) and carry on. They also ignore the
  Microsoft Store "python" placeholder that a fresh Windows leaves on PATH, which used to be
  reported as "Python too old".
- **Re-running the installer upgrades packages.** It now passes `--upgrade`, so the INSTALL
  guide's "re-run the installer to update" is true.
- `install_dependencies.bat --auto` skips its closing pause, for use from the launcher.
- Windows `.bat` files are normalised to CRLF, as `.gitattributes` has always required; LF-only
  batch files can break `call :label`.
- `frontend/package-lock.json` records the Node `engines` requirement.

---

## 2.4.0 — 2026-11-19

### Added — System Advisor

A new **Advisor** tab that answers *what can this machine actually run, and how safely?*
USER_GUIDE §14 · API_CONTRACT §22 · ARCHITECTURE §14 · SECURITY_REVIEW §8.3.

- **Hardware profile and live dashboard**: processor, memory, every GPU with its real VRAM, and
  every volume, on Windows, Linux and macOS. Live load, memory, GPU memory, temperature, disk and
  network throughput where the platform reports them — a reading that is unavailable is shown as
  missing, never as zero.
- **A memory budget that never plans 100%**: a system reserve plus a configurable headroom
  (GPU 15%, system memory 25% by default, each 5–50%). What is left is the *safe* budget; the
  headroom is the gap between *safe* and *maximum*.
- **Verdicts**: Optimal, Good fit, Maximum, Offload, CPU only, Too large, Add-on, Unknown — per
  model and per variant, with the reason. Multi-GPU machines are planned against the largest card
  for diffusion models and the pooled figure for language models; Apple unified memory is one pool
  and its GPU share is flagged as an estimate.
- **Recommendations** per model type: the optimum picks, the maximum that still runs on the GPU,
  and a stretch option that offloads. For language models a 4-bit-or-better variant is preferred
  over a larger one below 4 bits.
- **Model catalog**: about a thousand models shipped with the app (1,060 at release), so the
  Advisor works offline on first launch. Filter by type, verdict, source and safety score.
  Adult-flagged and safety-stripped models are left out by default — pinned ones included.
- **My models**: the models already indexed in the vault, assessed against the same budget.
- **Safety score**, 0–99 with a letter grade, itemised factor by factor: file format, the host's
  own scan, publisher, adoption, age, custom code, checksum, licence. Advisory, from published
  facts — **not a malware scan**, and never 100.
- **Safe settings**: ComfyUI launch arguments and Ollama / llama.cpp values derived from the same
  budget, each with its reason. Text to copy; nothing on the machine is changed.
- **Report**: saves `system-advisor-report-YYYY-MM-DD.md` — machine profile, memory plan, the
  per-type optimum / maximum / stretch picks with links, and the safe settings — built in the
  browser; nothing is uploaded.
- **Catalog refresh**: from Hugging Face, Civitai, the ComfyUI-Manager model list, OpenModelDB and
  the Ollama library, plus GitHub repository leads. Manual (**Update now**) or self-initiated when
  the data is over 24 hours old — 45 seconds after startup, every six hours after that, and when
  the Advisor is opened on stale data. Only while **Allow outbound lookups at all** is on. A
  source that fails keeps the entries it had; a cancelled run leaves the previous catalog.
- **Pins**: add a Hugging Face repository as `owner/name` and it is read at every refresh.
- **Models tab**: the details pane gained an **On this machine** block that links into the
  Advisor.
- **API**: 18 operations under `/api/v1/advisor` (API_CONTRACT §22). No route accepts a URL.
- **Configuration**: four new keys — `advisor_auto_update`, `advisor_vram_headroom_pct`,
  `advisor_ram_headroom_pct`, `advisor_kinds`. **No database migration**; the schema stays at v9.
  The catalog and the updater's state are JSON files under `backend/data/advisor/`
  (DATA_MODEL §21).
- **Dependency**: `psutil>=7.0` for the live readings. Optional at run time — without it the
  profile, the budget and every verdict still work.

### Security

- **A fifth `subprocess` call site**, `advisor/probe.py`: read-only hardware inventory from a
  frozen table of nine commands over six programs. The entry point takes a row name and nothing
  else; list argv, no shell, closed stdin, timeout, capped output.
- **A fourth host allowlist**, `KIND_CATALOG`: six exact hosts, no suffix rule, checked before
  every request and on every redirect hop. Requests carry no credentials and nothing about the
  machine. What comes back is size-capped JSON treated as data; links that are not plain https
  are blanked when a catalog is read.

### Changed — frontend toolchain

- Vite 4 → 8, `@vitejs/plugin-react` 6, Vitest 1 → 5, jsdom 31, three 0.187, lucide-react 0.577
  (the last line with the brand icons the rail uses; 1.x removed them). `npm audit` went from 6
  advisories (2 critical) to 0.
- **Building the interface from source now needs Node.js 20.19+ (or 22.12+).** Release archives
  ship it pre-built; running the app still needs no Node.
- The entry bundle went from 381.5 kB to about 366 kB.
- Testing Library cleanup is registered in `src/test/setup.js`.

### Tests

- **Backend**: seven new modules for the Advisor — hardware parsers fed canned `nvidia-smi`,
  `system_profiler`, `/proc` and `lspci` text; the budget and verdict arithmetic, including that
  more headroom never improves a verdict; the safety score's bounds; one normaliser per source
  against hand-written documents; the frozen probe table and the catalog allowlist; the shipped
  seed data; and the API end to end with a fake machine, a tmp catalog and no network.
- `tests/security/test_no_code_execution.py` now pins **five** `subprocess` modules and
  `test_dos_and_secrets.py` **nine** network-capable modules; `API_CONTRACT.md` conformance covers
  the new routes.
- **Screenshots**: six Advisor screenshots (`docs/screenshots/13`–`18`) added to the README and the
  user guide. The twelve older ones predate the Advisor tab and are unchanged.
- **Test temp files are no longer kept**: `pytest.ini` sets `tmp_path_retention_policy = failed`.
  The shared `temp_vault` fixture writes a real ~76 MB model file per test and pytest kept the
  last three runs, which reached 76 GB of temp data on a development machine.
- **`windows_paths` marker**: the tests that feed Windows paths to the path layer are skipped with
  a reason on macOS and Linux and run unchanged on Windows, so a non-Windows run no longer reports
  them as failures.
- `tests/test_build.py` finds `venv/bin/python` as well as `venv\Scripts\python.exe`, so the ruff
  and import gates run on macOS and Linux instead of skipping.
- **Frontend**: 7 Vitest files, 39 tests, including the Advisor's charts, catalog panel and
  report builder.

## 2.3.1 — 2026-09-06

### Fixed — "Recently added" sort on the Models tab returned a 500

Reported by a user who ran the code through a static analyser and traced it to the schema:
`v_model_list` never selected `models.created_at`, although the query layer had mapped the
documented `created` sort onto that column since the first release and the Models tab offered
it as **Recently added**. Choosing it raised `no such column: created_at` and the request ended
as an opaque internal error. Node Packages and Workflows read their base tables directly and
were not affected; the MCP `list_models` tool does not expose `created` and was not affected.

- **Database migration (v9)**: `v_model_list` is rebuilt with `created_at` (§20 in
  DATA_MODEL). Non-destructive — a view rebuild, no table changes. As with every schema bump the
  upgraded DB is refused by older builds, so this ships as 2.3.1 rather than as a silent patch.
- **Regression test**: `tests/unit/test_sort_vocabulary.py` executes every advertised sort key of
  every scope, ascending and descending, against a migrated DB — so a future view rebuild that
  drops a column cannot slip through again.
- **Docs**: DATA_MODEL's version header (stuck at v7) and view definition brought current; the
  API_CONTRACT sort table now lists `created` for `node_packages` and `workflows`, which
  FILTER_AUDIT §7 added but the table never recorded.

## 2.3.0 — 2026-09-05

### Added — Custom Output Folders support

- **Unlimited custom output folders on any drive**: Map arbitrary external directories where ComfyUI saves generated images, videos, audio, 3D models, or text files.
- **Full indexing & model linking**: Custom output folders are parsed for prompt metadata, seeds, samplers, generation graphs, and model links, making them fully searchable with lexical and semantic smart search.
- **Folder Watching**: Background watcher polling automatically detects new creations in custom output folders and indexes them incrementally.
- **Interactive Configuration UI**: Added a dedicated *Custom Output Folders* section in Settings with dynamic multi-folder additions and automatic cleanup of empty paths on save and close.
- **Database migration (v8)**: Rebuilt roots table check constraint to support `extra_outputs` roots while preserving offline drive metadata.

## 2.2.0 — 2026-08-29

### Fixed — ComfyUI Desktop's data folder was rejected at setup

Reported by a Desktop user: pointing the wizard at the folder Desktop's own onboarding
created failed with *"This does not look like a ComfyUI installation (expected
main.py/nodes.py plus a models/ directory)"*. Desktop keeps `main.py` inside the Electron
app and puts only the assets — `models/`, `custom_nodes/`, `output/`, `input/`, `user/` —
in the folder the user picks, which is everything the indexer actually reads. That layout
is now accepted as its own kind (`install_kind: "data_folder"`): models, outputs, workflows
and custom nodes are indexed in full, and the validator says up front what is *not*
available without the source tree — ComfyUI version detection, launching, updating, and the
built-in node-class list. A bare `models/` folder with nothing else beside it is still
rejected, and the launcher and updater keep their own, stricter install proof
(`comfyui_version.py` + `main.py`), so nothing security-reviewed was loosened.

### Added — run from anywhere, map folders from any drive

**The wizard can be skipped.** The app no longer locks you into the first-run wizard: **Set up
later** opens the vault immediately, with a persistent banner explaining that nothing can be
indexed until a folder is set — and a button that opens **Settings → Location** directly. The
same banner system reports the other two states that need you: the configured ComfyUI folder
going unreachable (drive offline, renamed, unplugged), and any extra scan folder being offline.
Each case says plainly that your library rows are kept, never wiped, while a drive is away.

**Any folder on any drive can be mapped in.** Settings → Location grew a scan-folder manager:
add a models folder (with its category, so classification and placement stay correct) or a
workflows folder from anywhere — a second drive, a NAS share, a folder shared with another UI —
and remove hand-added ones again. The mapping is deliberately robust: an offline drive stays in
the configuration and shows as *offline* until it returns; unplugging it never deletes the
mapping or the indexed rows. The health drawer gained a matching `scan_roots` check that counts
and names offline roots, and the ComfyUI-folder check now distinguishes *not configured* from
*configured but unreachable*.

Both the wizard and the folder manager now say out loud that the **first scan of a large
library — or one on a slow or network drive — can take a while**; it runs in the background,
and later scans only touch what changed.

### Fixed — the ComfyUI folder picker, and an audit pass over the installer and engine

**A portable install's own folder was rejected.** A portable build unpacks so that `ComfyUI`
sits beside `python_embeded` and `update`, which makes the parent — often a drive root, like
`O:\` — the natural thing to type. The wizard and Settings only ever inspected the exact folder
given, so that parent failed with "does not look like a ComfyUI installation" even though the
real install was one level down. Pointing at the parent now finds the install inside it; the
live validator says what it found before anything is saved, and the saved path is always the
install itself, so every root, scan and `custom_nodes` lookup derived from it is correct.

**Python 3.10 was advertised and enforced, but never actually worked.** A module-scope
`import tomllib` — stdlib only from 3.11 — meant a 3.10 install would pass every installer check
and then fail with an `ImportError` the first time a custom node's `pyproject.toml` was read. All
three installers, the README and `docs/INSTALL.md` now state the real floor, Python 3.11.

**`start_app.ps1` had fallen behind `start_app.bat`/`.sh`.** It never applied a staged self-update
before starting the engine, so a downloaded update stayed pending forever for anyone using the
PowerShell launcher, and it required `frontend/node_modules` unconditionally — dead on arrival for
a release archive, which ships the interface pre-built and needs no Node.js. Both are now
consistent across all three launchers, along with `install_dependencies.sh` verifying the same
package set as the Windows installers and `start_app.bat` stopping the engine after a startup
timeout instead of leaving it running and blocking the next launch.

**Smaller fixes found in the same pass:** smart search stayed stuck reporting "downloading"
forever after a successful first-time model download; a model with no file rows could fall back
to an unrelated file by id and rename or delete the wrong thing; a crashed download job was
requeued on restart but its worker was never restarted to actually process it; a Civitai network
timeout or rate limit was recorded as "this model does not exist" instead of "unreachable", so it
never retried; SSE progress streams could crash outright on Python 3.10; a tag rename or delete
past the 500th-most-used tag answered 404 even though the tag existed; search result lists
silently dropped matches past the 900th; reordering or re-parenting an album could make it vanish
from the tree because its id was stored as text; MP4/MOV files never got a width or height due to
an off-by-eight in the box parser; a safetensors file could be misclassified as "not a model" by
an unlucky header length; and GIF outputs got no metadata because they were routed to the video
parser instead of Pillow.

Also scrubbed: documentation and code comments carried the developer's own machine paths
(`O:\ComfyUI`, a `D:\Projects\...` checkout) and one private library-size figure — replaced with
generic examples throughout.

### Added

**The vault can update itself.** A new **Settings → Updates** panel shows the
installed version, the newest release published on GitHub, and the changelog
entry for it, so you can read what changed before deciding. Two preferences:
*Tell me when a new release is published* (on by default, and still silent
unless outbound lookups are enabled) and *Download new releases automatically*
(off by default). Release notes are the changelog's own top section, published
by `make_release.ps1 -Publish`, so what the panel shows can never drift from
what actually changed.

Downloading and installing are deliberately two moments. A download is
verified against the checksum published with the release, unpacked into
`backend/data/updates/staged`, and left inert. The swap is performed by
`apply_update.py`, which the launcher runs **before** the engine starts — no
module loaded, no request in flight, nothing replaced underneath a running
app. The previous version is kept for rollback, and a failure mid-swap
restores it automatically rather than leaving half of two versions behind.

What an update may place is bounded before a byte is written: the engine, the
interface, the docs and the launcher scripts, and nothing else. **`backend/data`
and `venv` can never be written by a release** — the database, ratings, tags,
notes, thumbnails and Python environment are outside the allowed set. The
archive is additionally capped at 256 MB, 20 000 entries and a 100× expansion
ratio, with absolute paths, traversal and link entries refused outright.

The repository is a constant in the source, not a setting, and only
`api.github.com`, `github.com` and `*.githubusercontent.com` are allowlisted
for it — no configuration can aim the updater at another project. The checksum
proves the download arrived intact; it is not proof of authorship, because it
travels from the same host as the file, and the UI says so rather than
implying more.

**Your library — ratings, tags, colour labels and notes.** The user-metadata layer that has
always existed in the schema now has an interface. Every model and output details panel carries
a **Your library** section: a five-star rating (click the current star to clear it), a colour
label, tag chips with an add field and suggestions from your existing tags, and a free-text
notes box that saves when focus leaves it. This makes three things that already shipped finally
mean something: the left rail's **My rating** filter, the **Rating** sort, and the **Untagged**
album — which until now matched the entire library, because nothing could tag anything. Tags,
ratings and notes were always preserved across rename and move; now there is a way to create
them outside of an MCP client.

**Model update detection actually detects.** Matching a hashed file against Civitai answers
"which version is this file" — it never asked "what is the newest version". A second, cached
lookup against the model's version list now does, so `has_update` is finally written, the
**Updates available** album can populate, and the **Newer version** callout can fire. A failed
latest-version lookup never blocks the match itself.

**Node-package update checks are implemented.** "Check updates" previously ended in an
unconditional error — there was no success path in the build. Each package's recorded commit is
now compared against its remote tip with one `git ls-remote` through the same hardened runner
and host allowlist as the Enable clone path: nothing is cloned, nothing runs, and only the
repository URL leaves the machine. Batch checks drain on a background worker; a package whose
branch was renamed upstream falls back to comparing HEAD. `commits_behind` stays deliberately
empty — counting it would need a fetch, and "the remote tip is not the commit you have" is the
honest fact one ls-remote can establish.

**Watch folders, for real.** The Settings toggle was wired to nothing. A watcher thread now
polls while the toggle is on: every two minutes it runs the incremental scan (sub-second when
nothing changed), so a model that finished copying appears in the vault within the interval —
no F5. Flipping the toggle takes effect without a restart, like every other setting.

**Release packaging.** `make_release.ps1` produces a versioned archive with the interface
pre-built, so a user needs Python but never Node.js — Node remains a build-time tool for source
checkouts, and nothing about the product changes. All three launchers and installers detect a
pre-built interface and skip the Node requirement; a dev checkout with `node_modules` present
still rebuilds every start so source edits are never served stale.

**POSIX launchers and first portability steps.** `start_app.sh`, `stop_app.sh`,
`install_dependencies.sh` and `show_service_status.sh` mirror their Windows counterparts.
**Reveal** now opens Finder with the file selected on macOS and the containing folder on Linux
instead of refusing off-Windows. The Storage tab keys volumes by device id on POSIX (drive
letters do not exist there, and every root previously reported as its own disk). Path keys and
fingerprints now fold case on macOS, whose default filesystem is case-insensitive — without
this, one file spelled two ways would index as two records. The first-launch wizard no longer
suggests a drive letter that only ever existed on the reference machine. Linux/macOS remain
experimental until the platform test suites run there — the README says exactly what is and
is not covered.

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

### Verified state

Full suite, including the tests that read the real install (`-m live`) and the timing budgets
(`-m perf`), run clean on an idle machine — not just the default excluded-by-default subset:

```
pytest tests -m "not live and not perf"    1541 passed, 3 skipped
pytest tests -m "live or perf"              220 passed, 1 skipped
npm run vitest                                10 passed
ruff check backend                          clean
npm run build                               381 kB entry (budget 400 kB)
```

| | |
|---|---|
| Real cold full index | 12.975 s (budget 25 s) |
| Real warm incremental | 0.650 s (budget 1.5 s) |
| Real lexical search, p95 over HTTP | 14.9 ms (budget 50 ms) |
| Real `/models` p95 | 4.6 ms (budget 40 ms) |
| Real `/ping` p95 during a full scan | 3.75 ms (budget 50 ms) |

The skips are environmental, not gaps: two need Developer Mode for symlink/junction tests, one
hooks a walker entry point that doesn't exist, and one is the launcher test correctly declining
to attribute a ping to `start_app.bat` while a manually-started engine already held the port.

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
