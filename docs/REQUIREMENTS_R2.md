# Product Decisions — Round 2

Required scope, same standing as [DECISIONS.md](DECISIONS.md). Where these extend
ARCHITECTURE.md / API_CONTRACT.md / DATA_MODEL.md, that document is kept in sync in the same
change.

## C6 — No AI-assistant attribution anywhere in the product

No reference to any AI coding assistant or AI vendor as an author, contributor, or tool — in
README, docs, code comments, UI strings, commit messages, or `package.json`. Applies to product
files only, **never** to third-party dependency source under `venv/` or `node_modules/`.

MCP client names must be generic ("desktop MCP client", "mcp-client"), never a vendor brand.

The product is authored by **Geekatplay Studio — Vladimir Chopine**, full stop.

## C7 — Changing the ComfyUI path must fully work, end to end

The owner must be able to point the app at a different ComfyUI installation from Settings and
re-index against it. Required behaviour, **verified by execution, not assumed**:

1. Settings accepts a new path with live validation (exists / looks like a ComfyUI install /
   which subfolders were found) *before* saving.
2. Saving persists it and **every** consumer picks it up with no restart — indexer, file-ops root
   guard, thumbnails, search, MCP. This is defect B6's blast radius; a regression here is severe
   (it previously caused 403s on every file operation).
3. Re-index is offered immediately after a path change, and the UI states plainly what happens to
   existing data — rows for the old root are either pruned or retained. Pick one, document it,
   and make the UI say which.
4. Multiple roots remain supported (`extra_model_paths.yaml`); changing the primary root must not
   silently orphan extra roots.
5. The `node_registry` root-keying bug must stay fixed — a path change previously killed
   node-class enrichment silently (1,866 → 1,855). A regression test changes the path and
   asserts enrichment still runs.

## C8 — Official ComfyUI workflows, version awareness, and updating ComfyUI

1. **Detect the installed ComfyUI version.** `C:\ComfyUI\comfyui_version.py` exposes
   `__version__` (currently **0.33.0**). Also record frontend/comfy package versions where
   available, and the install flavour (portable vs git checkout vs desktop).
2. **Show the latest available version** and whether an update exists. Read-only check; must
   degrade gracefully offline and must **never** auto-update.
3. **Offer to run the official updater.** Portable installs ship an update batch file (typically
   `update\update_comfyui.bat` beside the `ComfyUI` folder); git installs use `git pull`.
   **Discover the real mechanism for THIS install rather than assuming.** Requirements:
   - Explicit user confirmation naming exactly what will be run, with the resolved absolute path.
   - Never run automatically, never on a schedule, never from MCP without confirmation.
   - Stream output to the UI; surface the exit code; tell the user to restart ComfyUI afterwards.
   - Refuse if ComfyUI appears to be running.
4. **Index the official/bundled workflows and templates**, not just user workflows — including
   `custom_nodes/*/workflows` and `*/example_workflows` (211 workflows are already found, many
   from custom node packages). Label each workflow's origin: `user`, `official template`, or
   `bundled with <package>`. The owner explicitly wants official ComfyUI workflows visible
   alongside their version.

## C9 — Workflow "Enable": resolve and install a workflow's missing resources

**159 of 211** indexed workflows are currently unrunnable. For any workflow, the owner wants a
single action that makes it runnable by fetching what it needs into the correct folders.

1. **Show the full dependency report first** — every missing model (with resolved category and
   therefore target folder), every missing node package (with repo URL from the ComfyUI-Manager
   registry), and the total download size. Nothing downloads before the user sees this.
2. **Explicit per-item consent.** The user selects what to fetch. Show source URL, file size, and
   destination path for each. No silent or bulk-implicit downloads.
3. **Correct placement** — resolve the ComfyUI model folder from the node input that references
   it (`ckpt_name` → `checkpoints`, `lora_name` → `loras`, `unet_name` → `diffusion_models`,
   `vae_name` → `vae`, `clip_name` → `text_encoders`, …). Never write outside a configured root.
4. **Verify what was downloaded** — check size and, where the source publishes one, the hash.
   Quarantine and report a mismatch rather than placing a bad file.
5. **Resumable, cancellable, with progress**, reusing the existing job/SSE infrastructure. Model
   downloads are multi-GB.
6. **Sources limited to known model hosts** (Civitai, Hugging Face) plus registry-declared git
   repos for node packages. No arbitrary URL fetching.
7. **Node packages: clone/report only — never execute their installers or `pip install`
   automatically.** Tell the user the exact command to run. Auto-running an untrusted repo's
   `requirements.txt` is remote code execution.
8. Re-check the workflow afterwards and report whether it is now runnable.
9. Available from the UI; from MCP it is subject to the same confirmation rules.

## C10 — Storage & Maintenance view (new top-level tab)

The owner's drive is **86% full — 1.6 TB used, 272 GB free of 1.9 TB**. This is a primary
feature, not a footnote.

Must show:

1. **Space summary** — total ComfyUI footprint broken out by models / outputs / inputs /
   custom_nodes / cache, plus drive free/total for **every configured root** (roots can live on
   different drives).
2. **Largest files, sorted by size** — across models AND outputs, filterable by kind and folder,
   with a cumulative "reclaimable" total for the current selection.
3. **Oldest / stale content, sorted by age** — and "stale" must mean more than mtime. Rank by
   usefulness signals the vault already holds:
   - models referenced by **no** workflow and **no** output
     (`workflow_count == 0 AND output_count == 0`) — the "Unused models" album already exists,
   - last-modified / last-accessed age,
   - superseded models where a newer version is known,
   - duplicate or near-duplicate files (same hash, or same name across roots),
   - outputs older than N days, and outputs whose source model is gone.
4. **Cleanup actions** — multi-select with a running total of space to be reclaimed, trash-backed
   by default, `confirm: true` for permanent. Reuse the existing `file_ops` and trash. Never
   delete anything without explicit selection and confirmation. Show what trash itself is holding.
5. **Combined ordering** — the owner explicitly wants to sort by size AND by age/staleness in the
   same view. Make both first-class sort keys, with a combined "reclaim score" as the default.
6. Covers models, LoRAs, and output assets alike.

## C11 — UI arrangement: dense but not crowded

The maintenance data must be "well arranged, easy to see, detailed, but not overcrowded."
Rules for the storage view's design and implementation:

- **Progressive disclosure**: a summary layer that answers "where did my terabyte go?" at a
  glance, with detail on demand — never a wall of numbers.
- One primary visual for the space breakdown; tables carry the detail.
- Reuse the existing grid / list / details / status structure. Do not invent a new interaction
  model.
- Destructive actions visually distinct and never adjacent to routine controls.
- Respect the amber/violet convention: **amber for measured facts, violet for inferred
  judgments** — "0 references" is measured, "probably stale" is inferred.
