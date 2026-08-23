# User Guide

Geekatplay ComfyUI Asset Vault · **Geekatplay — Vladimir Chopine**

Everything below describes the app running against a real ComfyUI 0.33.0 install with 237 models,
34 node packages, 1,866 node classes, 211 workflows and 3,834 outputs. The numbers you see are
your own.

---

## 1. The shell

Five regions, the same on every tab.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ GEEKATPLAY   Models 237  Nodes 34  Workflows 211  Outputs 3,834  Storage      │  top bar
│ ASSET VAULT  [ search ................... / ]  ( ) Smart   ♥ ⚙ Reindex  Hash  │
├───────────────┬──────────────────────────────────────────────┬───────────────┤
│ MODELS —      │ ▦ ☰  ──────●──── 180px  SORT ▾  GROUP ▾       │ DETAILS       │
│ GROUPS        │ 237 MODELS   Hash  Rename  Move  Delete   ⟳   │               │
│               ├──────────────────────────────────────────────┤ what it is,   │
│ ALBUMS        │ CATEGORY  Diffusion 61  Loras 59  …  BASE …  │ what it       │
│ FOLDERS       ├──────────────────────────────────────────────┤ contains,     │
│ BASE MODEL    │                                              │ where it came │
│               │            the grid, or the list             │ from, which   │
│               │                                              │ workflows use │
│               │                                              │ it            │
├───────────────┴──────────────────────────────────────────────┴───────────────┤
│ ● 2 integrity issues   237 items  1–100 shown  10 ms lexical   PER PAGE 100 ◀▶ │  status bar
└──────────────────────────────────────────────────────────────────────────────┘
```

Both side panels are draggable. Their widths, the tile size, the sort and grouping choices, and
the page size are remembered per tab.

### Amber means measured. Violet means inferred.

This is the single most useful thing to know about the interface, and it is a rule, not a theme.

* **Amber** — read from your own files or counted in the database. "0 references", a parsed
  parameter count, a computed hash, a byte total.
* **Violet** — a judgment. A base model guessed from tensor shapes, "probably stale", a
  name-similarity suggestion, a locally generated summary.

A detail row that is inferred says so, usually with the method and a confidence next to it —
`inferred from tensor shapes / confidence 0.00` is the app telling you it does not actually know.

---

## 2. Finding things

### Search

Type in the top bar, or press `/` from anywhere. Search covers all five asset kinds at once —
models, node packages, node classes, workflows, outputs — and the status bar shows how long it
took and which engine answered (`lexical` or `hybrid`).

**Smart** fuses semantic ranking with the lexical results, which helps with descriptive queries
("cyberpunk portrait lora") rather than exact names. It needs the optional embedding model
(INSTALL §5); until then the toggle reads as unavailable and tells you why. Search itself is never
blocked by this.

### Left rail

Contents change per tab, and every entry carries its own count and byte total.

* **Albums** — `All`, `Recently added`, `Favorites`, `Needs hashing`, `Updates available`,
  `Missing files`, `Integrity issues`, `Unused models`, `Untagged`, plus any you create.
* **Folders** — the real folder tree under each root, with per-folder counts and sizes.
* **Base model / Source / Authors / State / Media / When** — depending on the tab.

Clicking a rail entry filters the grid. Clicking it again clears it. **All** at the top of the
rail resets everything.

### Toolbar

Grid / list toggle, a tile-size slider (120–450 px), **SORT**, **GROUP**, the live count, the
bulk actions for the current selection, and a refresh button.

Grouping turns the grid into labelled bands — by category, base model, folder, precision, root,
hash state, first letter or date. It is the fastest way to see the shape of a library.

### Facet chips

The strip under the toolbar shows the top values in the current result set with their counts, so
you can narrow down without knowing the vocabulary in advance.

### Keyboard

| Key | Action |
|---|---|
| `/` | focus search |
| arrows, `Home`/`End`, `PgUp`/`PgDn` | move through the grid |
| `Enter` | open the focused asset |
| `Ctrl+A` | select everything on the page |
| `Delete` | delete the selection — always via a confirmation dialog |
| `F5` or `Ctrl+R` | re-index |
| `Esc` | close the top overlay, or clear the selection |

Keys are ignored while you are typing in a field.

---

## 3. Models

Every model file under every configured root — checkpoints, diffusion models, LoRAs, VAEs,
ControlNets, text encoders, CLIP and CLIP-vision, upscalers, and the rest.

Cards show the base-model family, the role, size, precision and parameter count, plus a hash-state
badge (`unhashed` · `queued` · `hashing` · `done` · `failed` · `stale`).

Select one and the **Details** panel gives you:

* **Technical** — architecture and role, base model, variant, modality, precision, quantisation,
  parameter count, tensor count, container format, prediction type, resolution hint, and whether
  it is a bundled checkpoint or an adapter.
* **What is inside** — for a bundled checkpoint, the component parts and their share of the
  parameters. This is why `flux1-dev-fp8` reports 11.9 B primary parameters rather than the
  16.87 B you get by naively summing every tensor in the file.
* **Detection signals** — which rule fired, or, honestly, `rule:none` with a confidence, when the
  family was guessed from shapes.
* **Verification** — integrity verdict, and the AutoV2 / SHA-256 hash once computed.
* **Newer version**, **Description**, **How to use it** (trigger words, recommended sampler / CFG
  / steps), **Build spec**, **Where it came from** — the Civitai side. Empty until the file has
  been hashed and matched.
* **Used in** — the workflows that reference this file and the outputs that came from it, with
  the counts in the section header.
* **File** — size, modified date, folder, absolute path, root.

Footer actions: **Rename**, **Move**, **Delete**. Header actions: **Preview**, **Favourite**,
**Reveal** (opens the containing folder in Explorer).

---

## 4. Nodes

Two views over `custom_nodes/`: the **packages** you installed, and the **node classes** they
register.

Package detail shows the git remote, branch and commit, the declared Python dependencies, the
author, whether it is enabled, and its class count. Class detail shows the display name, category,
inputs and outputs, and every workflow that uses it.

Node classes are found by **statically parsing the Python source**. No package code is imported
or executed, ever. That is why the count is trustworthy — 1,866 classes across 32 of 34 packages
on the reference install; the two that yield nothing genuinely register nothing.

Filter by **Official ComfyUI** versus **Custom packages** from the left rail. 841 of the 1,866
classes on the reference install are official.

---

## 5. Workflows

Workflows found in `user/default/workflows`, the root `workflows/` folder, and inside node
packages' `workflows/` and `example_workflows/` folders, plus graphs embedded in output images.
Each row is labelled by **origin** — yours, an official ComfyUI template, or bundled with a named
package.

### Why a workflow says it will not run

Select a workflow and open its dependency report. The header states the totals — on one real
example, `37 total · 23 satisfied · 14 missing`. Beneath it, two sections:

**Node packages** — every node class the graph uses that no installed package registers. The
workflow cannot execute at all without these; ComfyUI would refuse to load the graph.

**Model files** — every model the graph names that is not in your library, each with:

* the exact string the workflow asked for, e.g. `WanVideo/Lightx2v/lightx2v_I2V_14B_480p_…`,
* the **category it belongs in**, resolved from the node input that referenced it —
  `ckpt_name` → checkpoints, `lora_name` → loras, `unet_name` → diffusion_models,
  `vae_name` → vae, `clip_name` → text_encoders,
* which node class and input asked for it,
* **close matches** you already own, scored by name similarity.

Those suggestions are marked violet and labelled `this is a guess, not a match`, because they are.
A score of 1.00 usually means you have the exact file under a different sub-folder — a common
cause of a workflow that "should" work. Fixing it is normally a move or a rename, not a download.

On the reference install **159 of 211** workflows are missing at least one dependency, which is
ordinary for a library that has accumulated example graphs from 34 node packages.

Satisfied dependencies show the match method — `exact_relpath`, `basename`, and so on — so you can
tell a firm match from a loose one.

### Making a workflow runnable

The **Enable** flow fetches what a workflow is missing, into the right folders. It is deliberately
a two-step process, and the first step downloads nothing.

**Step 1 — the plan.** You get the whole report before anything happens: every missing model with
the destination folder it resolves to, every missing node package with the repository the
ComfyUI-Manager registry names for it, the total download size, how much of it is actually
fetchable, and the **free space on each target drive** with a 5% margin. On the example above:
`37 total · 23 satisfied · 3 missing models · 3 missing node packages · 2 fetchable · 4 not`.

Items that cannot be fetched are listed as such rather than silently dropped — a model nobody
publishes on a permitted host stays your problem, and the report says so.

**Step 2 — your explicit selection.** You tick the items you want. There is no "fetch everything"
shortcut: the fetch call needs the token from the plan you were shown, the specific item ids you
chose, and an explicit confirmation. A stale plan is refused, so you can never confirm one report
and have a different one act on it.

While it runs: progress streams live, it is cancellable, and multi-gigabyte downloads resume.
Afterwards the workflow is re-checked and the app tells you whether it is now runnable.

**What it will not do:**

* **Fetch from arbitrary URLs.** Models come only from Civitai or Hugging Face; node packages only
  from registry-declared repositories. Every redirect is re-checked against that list.
* **Overwrite.** On a name conflict you get fail, skip, or keep-both. There is no overwrite option
  on this path.
* **Trust what it downloaded.** Size and, where the source publishes one, hash are verified. A
  mismatch is quarantined and reported, never placed in your model folders.
* **Run a node package's installer.** Packages are cloned, full stop — no `pip install`, no
  `requirements.txt`, no `install.py`. Auto-running an untrusted repository's install script is
  remote code execution. The app tells you the command; you decide.

Available from the workflow's detail panel, and from MCP under the same confirmation rules.

---

## 6. Outputs

Everything ComfyUI has generated. Grouped by folder, date, source model, media kind or album.

Thumbnails come from a local cache, which is what keeps the grid smooth at 3,834 files —
scrolling measured a 17.9 ms p95 frame time. Files that cannot be thumbnailed get a generated
placeholder rather than a broken image.

Click any output for the **lightbox**: full-resolution image, `←`/`→` to move through the set,
`Esc` to close, and a metadata drawer with

* the positive prompt, with a **Copy** button,
* the negative prompt,
* seed, steps, CFG, sampler, scheduler,
* the model that produced it, with **Open model** to jump straight to it in the Models tab,
* **Open workflow** when the graph is recoverable from the file.

**Reveal** opens the containing folder. Where an output carries a full embedded graph you can
extract it into a standalone workflow.

2,660 of the 3,834 reference outputs carry usable generation metadata; the rest were saved without
it, and the panel says so rather than showing empty fields.

---

## 7. Storage and maintenance

The tab that answers "where did my terabyte go?". Five sections, chosen from the left rail.

### Overview

One picture and a small number of headline figures.

* **Footprint** — total ComfyUI size broken into models / outputs / inputs / custom nodes /
  cache & temp / program files, measured from disk (129 ms on the reference install), with what
  the vault's own database and thumbnails cost shown separately.
* **Per-drive headroom** — free and total for **every** configured root, because roots can live
  on different drives. The reference machine: `O:` at 85.4% used, 292 GB free of 2.00 TB.
* **Reclaimable** — the piles, each with a count, a byte total and a confidence:

| Group | On the reference install |
|---|---|
| Models referenced by no workflow and no output | **100 models · 485 GiB** *(measured)* |
| Duplicate or near-duplicate models | 4 · 5.9 GiB *(inferred)* |
| Superseded by a newer version | 1 *(inferred)* |
| Models untouched for 180+ days | 7 · 57.7 GiB *(measured)* |
| Outputs older than 180 days, orphaned outputs | 0 |

"Referenced by nothing" is a **measured** fact — it is a count of rows. "Probably stale" is a
judgment and is coloured accordingly. The rail lists the same groups so you can jump straight into
any one of them.

### Cleanup candidates

One paged table, sortable three ways, and this is the point of the view:

* **Score** — the combined reclaim score, 0–100, the default;
* **Size** — largest first;
* **Age** — oldest first.

Each row explains itself. `Referenced by no workflow and no output` (weight 35),
`Large file (43.0 GB)` (weight 25), `Possible duplicate — matched by name+size` (weight 10), and
so on, each tagged measured or inferred. Favourites and 4-plus-star items carry a large negative
weight and are shown flagged as **protected** rather than hidden.

Filter by kind, reason, category, role, media kind, root, folder, name, size range or age. As you
tick rows, a running total of the space you would reclaim updates. The batch cap is 200 items per
action.

Two buttons, deliberately far apart and visually distinct: move to trash (recoverable), and
remove from disk permanently (no way back). The permanent one demands a separate confirmation.

### Duplicates

Groups of files that are the same, each naming the method that found it. Only **sha256** groups
are exact — those need the files to have been hashed. `name+size` and `name across roots` are
candidates, and are marked as inferred.

### Trash

What the vault is currently holding, per root, with **Restore** per item and **Empty the trash**.
Retention is 30 days by default. This is the safety net behind every delete; check here before you
assume something is gone.

### ComfyUI install

Everything the app knows about the installation itself.

* **Version and flavour** — read from `comfyui_version.py`. The reference install reports
  **0.33.0**, flavour **portable**, with the evidence listed: embedded interpreter at
  `O:\python_embeded`, a portable `update` folder, launcher batch files beside the ComfyUI folder.
* **Packages** — frontend 1.49.6, embedded docs 0.5.10, and the workflow-template packages.
* **Git state** — branch, commit and remote where present.
* **Latest available** — a read-only check that needs outbound lookups enabled. Offline it reports
  `unknown` with a reason, and never guesses.
* **Updater** — the app *discovers* the real mechanism rather than assuming one. On the reference
  install it finds three portable updaters and recommends
  `O:\update\update_comfyui.bat`, with `update_comfyui_stable.bat` and
  `update_comfyui_and_python_dependencies.bat` as alternatives, each with a note on when to use it.

Running one requires an explicit confirmation that **names the resolved absolute path**. It is
refused while ComfyUI appears to be running. Output streams into the UI, the exit code is shown,
and you are reminded to restart ComfyUI and re-scan the vault afterwards. Nothing about this is
ever automatic or scheduled.

---

## 8. Renaming, moving, deleting

Available from the Details panel footer and from the toolbar for a multi-selection.

* **Rename** — offers to keep the extension and to rename matching sidecar files together.
* **Move** — pick a root and a target folder; missing folders can be created; you choose what
  happens on a name conflict.
* **Delete** — **trash by default**. Files go to `.vault-trash` inside the root they came from,
  and are restorable from Settings → Storage or the Storage tab's Trash section. Permanent
  deletion is a separate, explicit choice with its own confirmation.

Every operation is checked against the configured roots first. Anything that would land outside
them is refused. A retired root — one left behind by a path change — is read-only.

Thumbnails, album membership, tags, ratings and notes follow a file when it is renamed or moved.

---

## 9. Hashing and Civitai

**Hash** in the top bar. Choose a scope, watch the queue, cancel whenever you like. The job
survives an app restart and resumes. Cached against `(path, size, mtime)`, so a file is re-hashed
only if it actually changed.

Nothing about the app waits for this. A model is fully usable while unhashed; only the Civitai
fields are empty, and they fill in progressively as hashes land.

**The full 1.5 TB reference library takes roughly 2.8 hours.** The dialog says so. Start with one
category if you only care about part of the library.

Civitai matching is off by default and needs outbound lookups enabled. Only the hash is sent.

---

## 10. Re-indexing

**Reindex** in the top bar, or `F5`. Progress streams live — phase by phase through roots, walk,
models, nodes, workflows, outputs, links, index and prune — with a running count and an ETA, and
a cancel button.

Scans are incremental. A warm scan finishing in **0.31–0.37 s** is normal; a full re-parse takes
**13.8–17.4 s**. Files whose size and mtime are unchanged are skipped entirely.

**A vanished file is flagged, never deleted.** The prune phase marks it as missing and it appears
in the `Missing files` album with its ratings, tags and album membership intact, so reconnecting a
drive restores it exactly as it was.

Settings → Jobs controls whether a scan runs automatically at startup when files changed, and
whether folders are watched for changes.

---

## 11. Health

The pulse icon in the top bar opens the health drawer: one line per check, each with a status and,
where useful, an action.

Checks: ComfyUI root reachable · database mode and size · embedding model present · Civitai
enabled · Ollama reachable · file-integrity failures (with the offending files named) · partial
downloads left behind (`.crdownload`, `.part`) · node packages whose git remote does not look like
the package name · thumbnail cache size against its cap.

Overall status is the worst individual check, so "error" often just means one unreadable file.
Open the drawer and read the line, do not guess.

The same report is available at `GET /api/v1/system/health`.

---

## 12. Settings

Four sections.

| Section | Contains |
|---|---|
| **Location** | ComfyUI path with live validation, the configured roots, `extra_model_paths.yaml` contents, Save path · Save and reindex now · Run the setup wizard |
| **Search** | Smart search enable/rebuild and its status, Civitai matching, the outbound-lookups master switch, local text generation with a Test button |
| **Jobs** | Reindex on startup when files changed, watch folders, also read `extra_model_paths.yaml.hold`, default delete mode, trash retention |
| **Storage** | Thumbnail cache size and Trim now, the trash list with Restore and Empty |

**Allow outbound lookups at all** is the master switch. With it off, nothing reaches the network,
regardless of the other toggles.

---

## 13. Connecting an assistant

The vault ships an MCP server with 26 tools — reading, searching, full file operations, and the
over both stdio and HTTP. Deletions are trash-backed, permanent deletion needs an explicit
confirmation, a single call can touch at most 200 items, and every mutation is written to an
`mcp_audit` row in the database with its arguments, the items it touched and the outcome.

**Read that log in Settings → Activity.** It opens with the headline figures — how many calls were
recorded, how many items they touched, how many failed — then the split between deletes/moves/
renames and other changes, then a table per tool. Filter by outcome, transport, tool or free text,
and click any line to see the exact arguments that call was given and the items it named. Deletes
are marked apart from other writes and failures apart from successes. The log is append-only:
nothing in the app can edit or remove a line of it.

See **[MCP_CLIENT_SETUP.md](MCP_CLIENT_SETUP.md)**.
