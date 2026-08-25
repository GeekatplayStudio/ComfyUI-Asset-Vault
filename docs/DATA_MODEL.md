# Data Model — Geekatplay ComfyUI Asset Vault

Geekatplay Studio — Vladimir Chopine

SQLite (stdlib `sqlite3`), WAL. **Schema version 7** (v2 base + v3 album identity + v4 workflow origin + v5 the Enable fetch queue, §15–§16 + v6 provided nodes and subgraph counts, §5.2 / §6 + v7 Civitai rating/download count on `v_model_list`, §13). Canonical version lives in `PRAGMA user_version`.

DB file: `backend/data/vault.db` (renamed from `asset_vault.db`; see §14 migration).

---

## 0. Conventions

* **`uid`** — the universal asset address: `"<kind>:<id>"` e.g. `model:41`, `node_class:9182`, `output:930`. Used by FTS, embeddings, tags, albums, thumbnails, file ops, and MCP. `kind` ∈ `model | node_package | node_class | workflow | output | input`.
* **Timestamps** — `INTEGER` Unix epoch **milliseconds** UTC. Never floats, never local time.
* **File times** — `mtime_ns INTEGER` (NTFS 100 ns resolution; float seconds collide).
* **Paths** — `abs_path TEXT` stored **as-is for display**, plus `path_key TEXT` = `os.path.normcase(os.path.realpath(p))` which carries the `UNIQUE` constraint. Two columns because NTFS is case-insensitive but users expect their casing preserved.
* **Booleans** — `INTEGER` 0/1 with `CHECK (col IN (0,1))`.
* **JSON columns** — `TEXT` containing UTF-8 JSON, suffixed `_json`. Never queried with `LIKE` in a hot path; anything filterable gets a real column or a link table.
* **Enums** — `TEXT` with a `CHECK` constraint, vocabulary frozen in `ARCHITECTURE.md` §4.3.1.
* **Deletion** — soft first (`missing_since`), hard after retention. `ON DELETE CASCADE` everywhere a child cannot outlive its parent.

---

## 1. `schema_migrations` + `PRAGMA user_version`

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);
```
`PRAGMA user_version` is the authority (atomic, readable before any table exists). `schema_migrations` is the human-readable audit log. Migrations are ordered Python callables in `app/core/migrations/` named `m001_initial.py`, `m002_…`; the runner executes every `v > user_version` inside one transaction each and bumps `user_version`.

---

## 2. `config`

```sql
CREATE TABLE config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    value_type TEXT NOT NULL DEFAULT 'str'
               CHECK (value_type IN ('str','int','float','bool','json')),
    updated_at INTEGER NOT NULL
);
```
**The only persistent home of the ComfyUI path.** Keys: `comfyui_path`, `is_configured`, `auto_reindex`, `online_enabled`, `civitai_enabled`, `civitai_api_key`, `ollama_enabled`, `ollama_url`, `ollama_model`, `smart_search_enabled`, `embedding_model_id`, `embedding_state`, `hash_concurrency`, `hash_throttle_mbps`, `thumb_cache_max_mb`, `thumb_video_ffmpeg`, `page_size_default`, `watch_enabled`, `trash_mode`, `trash_retention_days`, `read_held_extra_paths`, `extra_workflow_dirs`, `ui_prefs_json`.

No index needed (tiny, PK lookups only).

---

## 3. `roots`

```sql
CREATE TABLE roots (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('comfyui','extra_models','extra_workflows','data')),
    path        TEXT NOT NULL,
    path_key    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    category    TEXT,             -- for extra_models: 'loras','checkpoints',…
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
    source      TEXT NOT NULL CHECK (source IN ('config','yaml','manual')),
    available   INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
    last_seen_at INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX ix_roots_kind ON roots(kind, available);
```
`available=0` when the volume is disconnected — the trigger for "offline root" degradation instead of pruning.

---

## 4. Models

### 4.1 `models` — the logical, enriched record

```sql
CREATE TABLE models (
    id                  INTEGER PRIMARY KEY,

    -- identity
    name                TEXT NOT NULL,            -- display name (stem)
    canonical_key       TEXT,                     -- sha256 when known, else path_key of primary file
    primary_file_id     INTEGER REFERENCES model_files(id) ON DELETE SET NULL,

    -- classification (ARCHITECTURE.md §4.3.1 vocabulary)
    category            TEXT NOT NULL,            -- folder-derived: loras, checkpoints, vae, …
    model_role          TEXT NOT NULL DEFAULT 'unknown',
    base_model_family   TEXT NOT NULL DEFAULT 'Unknown',
    base_model_variant  TEXT,                     -- 'Pony','Illustrious','2.2-I2V',…
    modality            TEXT NOT NULL DEFAULT 'unknown',
    architecture_label  TEXT,                     -- human string: 'FLUX Transformer (dual-stream)'
    arch_source         TEXT NOT NULL DEFAULT 'none'
                        CHECK (arch_source IN ('none','metadata','structural','shape','prior','civitai')),
    arch_confidence     REAL NOT NULL DEFAULT 0.0,

    -- adapters
    is_adapter          INTEGER NOT NULL DEFAULT 0 CHECK (is_adapter IN (0,1)),
    adapter_format      TEXT,                     -- 'peft','kohya','diffusers','loha','lokr','oft'
    adapter_rank        INTEGER,
    adapter_alpha       REAL,

    -- technical spec
    is_bundled          INTEGER NOT NULL DEFAULT 0 CHECK (is_bundled IN (0,1)),
    components_json     TEXT,                     -- {"unet":{"params":…,"dtype":…},…}
    param_count_primary INTEGER,                  -- the UNet/DiT component
    param_count_total   INTEGER,
    tensor_count        INTEGER,
    precision           TEXT,                     -- fp32|fp16|bf16|fp8|int8|mixed
    quantization        TEXT,                     -- comfy_scaled_fp8|gguf_q4_k_m|…
    resolution_hint     TEXT,                     -- '1024x1024'
    prediction_type     TEXT,                     -- epsilon|v_prediction|flow
    header_metadata_json TEXT,                    -- verbatim __metadata__ (capped 64 KB)

    -- integrity
    integrity           TEXT NOT NULL DEFAULT 'ok'
                        CHECK (integrity IN ('ok','invalid_header','not_a_model',
                                             'truncated','unreadable','unsupported_format')),
    integrity_note      TEXT,

    -- enrichment (Civitai)
    civitai_model_id    INTEGER,
    civitai_version_id  INTEGER,
    civitai_url         TEXT,
    civitai_state       TEXT NOT NULL DEFAULT 'none'
                        CHECK (civitai_state IN ('none','pending','matched','not_found','error','stale')),
    civitai_checked_at  INTEGER,
    description         TEXT,
    description_source  TEXT,                     -- civitai|readme|ollama|user
    usage_notes         TEXT,                     -- "best way to use"
    trigger_words_json  TEXT,
    recommended_settings_json TEXT,
    download_url        TEXT,
    license_text        TEXT,
    nsfw                INTEGER NOT NULL DEFAULT 0 CHECK (nsfw IN (0,1)),
    rating              REAL,
    download_count      INTEGER,

    -- update tracking
    has_update          INTEGER NOT NULL DEFAULT 0 CHECK (has_update IN (0,1)),
    latest_version_name TEXT,
    latest_version_id   INTEGER,
    latest_version_notes TEXT,
    latest_version_benefits TEXT,                 -- "what it improves" (Ollama-summarized)

    -- user data
    user_notes          TEXT,
    user_rating         INTEGER CHECK (user_rating BETWEEN 0 AND 5),
    favorite            INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    color_label         TEXT,

    -- derived counters (maintained in phase 7)
    workflow_count      INTEGER NOT NULL DEFAULT 0,
    output_count        INTEGER NOT NULL DEFAULT 0,
    file_count          INTEGER NOT NULL DEFAULT 1,
    total_size          INTEGER NOT NULL DEFAULT 0,

    -- lifecycle
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    missing_since       INTEGER
);
```

### 4.2 `model_files` — the physical file

```sql
CREATE TABLE model_files (
    id             INTEGER PRIMARY KEY,
    model_id       INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    root_id        INTEGER NOT NULL REFERENCES roots(id)  ON DELETE CASCADE,

    abs_path       TEXT NOT NULL,
    path_key       TEXT NOT NULL UNIQUE,
    rel_path       TEXT NOT NULL,          -- relative to root
    folder         TEXT NOT NULL,          -- rel dir, powers folder grouping
    filename       TEXT NOT NULL,
    stem           TEXT NOT NULL,
    ext            TEXT NOT NULL,          -- '.safetensors'

    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    ctime_ns       INTEGER,
    fingerprint    TEXT NOT NULL,

    format         TEXT NOT NULL,          -- safetensors|gguf|torch_zip|torch_legacy|onnx|other
    header_parsed  INTEGER NOT NULL DEFAULT 0 CHECK (header_parsed IN (0,1)),
    parser_version INTEGER NOT NULL DEFAULT 0,

    -- hashing (ARCHITECTURE.md §4.2)
    hash_state     TEXT NOT NULL DEFAULT 'unhashed'
                   CHECK (hash_state IN ('unhashed','queued','hashing','done','failed','stale')),
    sha256         TEXT,
    autov2         TEXT,                   -- upper(sha256[:10])
    blake3         TEXT,                   -- reserved
    probe_sha256   TEXT,                   -- first+last 1 MiB, computed during scan; enables move-reuse
    hashed_at      INTEGER,
    hash_error     TEXT,
    hash_bytes_done INTEGER NOT NULL DEFAULT 0,

    -- sidecars
    preview_path   TEXT,
    sidecar_json   TEXT,

    first_seen_at  INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    missing_since  INTEGER
);
```

### 4.3 Model indexes and their driving queries

| Index | Driving query |
|---|---|
| `ux_model_files_pathkey` (UNIQUE, implicit) | scan upsert by path; file-op relocation |
| `ix_model_files_model` `(model_id)` | detail panel: list a model's files |
| `ix_model_files_fp` `(fingerprint)` | phase-2 incremental diff (the hottest scan lookup) |
| `ix_model_files_hash_state` `(hash_state, id)` | hash queue draining; "unhashed" facet count |
| `ix_model_files_sha` `(sha256)` WHERE `sha256 IS NOT NULL` | Civitai match; duplicate detection |
| `ix_model_files_probe` `(size, probe_sha256)` | move/rename hash reuse (§4.2) |
| `ix_model_files_root_folder` `(root_id, folder)` | folder-tree grouping in the left rail |
| `ix_models_cat_name` `(category, name COLLATE NOCASE)` | default list: filter by category, sort by name |
| `ix_models_family_cat` `(base_model_family, category)` | facet chips + `?base_model=` filter |
| `ix_models_role` `(model_role)` | role facet |
| `ix_models_updated` `(updated_at DESC)` | sort by date |
| `ix_models_size` `(total_size DESC)` | sort by size |
| `ix_models_fav` `(favorite, updated_at DESC)` WHERE `favorite=1` | Favorites album (partial index — tiny) |
| `ix_models_missing` `(missing_since)` WHERE `missing_since IS NOT NULL` | Missing facet |
| `ix_models_update` `(has_update)` WHERE `has_update=1` | "Updates available" album |
| `ix_models_integrity` `(integrity)` WHERE `integrity<>'ok'` | Health drawer |
| `ix_models_civitai` `(civitai_model_id)` | dedupe/version grouping |

`COLLATE NOCASE` on the name index is required for case-insensitive alphabetical sorting to be index-served rather than a filesort — with 231 models it is irrelevant, at 10k it is the difference between 2 ms and 40 ms.

---

## 5. Nodes

### 5.1 `node_packages`

```sql
CREATE TABLE node_packages (
    id                INTEGER PRIMARY KEY,
    folder_name       TEXT NOT NULL,
    path_key          TEXT NOT NULL UNIQUE,
    abs_path          TEXT NOT NULL,

    display_name      TEXT NOT NULL,
    author            TEXT,
    publisher_id      TEXT,                    -- pyproject [tool.comfy] PublisherId
    registry_id       TEXT,                    -- comfyregistry name
    description       TEXT,
    long_description  TEXT,                    -- README first section
    icon_url          TEXT,
    homepage_url      TEXT,
    license           TEXT,

    is_official       INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0,1)),
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    disabled_reason   TEXT,                    -- '.disabled suffix' | 'marker file'
    is_single_file    INTEGER NOT NULL DEFAULT 0 CHECK (is_single_file IN (0,1)),

    -- git
    repo_url          TEXT,
    repo_url_normalized TEXT,
    repo_url_suspect  INTEGER NOT NULL DEFAULT 0 CHECK (repo_url_suspect IN (0,1)),
    git_branch        TEXT,
    git_commit        TEXT,
    git_commit_at     INTEGER,
    git_dirty         INTEGER,
    last_fetch_at     INTEGER,

    -- update
    installed_version TEXT,
    latest_version    TEXT,
    latest_commit     TEXT,
    commits_behind    INTEGER,
    has_update        INTEGER NOT NULL DEFAULT 0 CHECK (has_update IN (0,1)),
    update_notes      TEXT,
    update_checked_at INTEGER,
    update_check_state TEXT NOT NULL DEFAULT 'none'
                      CHECK (update_check_state IN ('none','pending','ok','rate_limited','error','offline','suspect_remote')),

    -- deps & extraction
    python_deps_json  TEXT,
    deps_satisfied    INTEGER,                 -- 1/0/NULL(unknown)
    deps_missing_json TEXT,
    has_web_directory INTEGER NOT NULL DEFAULT 0 CHECK (has_web_directory IN (0,1)),

    class_count       INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'ok'
                      CHECK (extraction_status IN ('ok','partial','registry_only',
                                                   'no_classes_found','empty_package','error')),
    extraction_strategies_json TEXT,           -- ["S1","S3","S6"]

    file_count        INTEGER,
    total_size        INTEGER,
    folder_mtime_ns   INTEGER,
    fingerprint       TEXT NOT NULL,
    parser_version    INTEGER NOT NULL DEFAULT 0,

    workflow_count    INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    missing_since     INTEGER
);
```
`fingerprint` for a package = blake2b over the sorted `(rel_path, size, mtime_ns)` of every `.py`/`.toml`/`.txt`/`.md`/`.json`/`.js` in it. `.js` is in the list because a package can register node types from its shipped `web/` code (§5.2, `registration`); without it, editing that JavaScript would not change the fingerprint and the re-scan would skip the package. That is what makes node re-analysis incremental — measured at ~4k files across 35 packages, the fingerprint walk is ~0.05 s versus ~3 s of AST work.

### 5.2 `node_classes`

```sql
CREATE TABLE node_classes (
    id               INTEGER PRIMARY KEY,
    package_id       INTEGER NOT NULL REFERENCES node_packages(id) ON DELETE CASCADE,

    node_id          TEXT NOT NULL,            -- the NODE_CLASS_MAPPINGS key / V3 node_id
    class_name       TEXT,                     -- Python class
    display_name     TEXT,
    category         TEXT,                     -- 'image/transform'
    description      TEXT,

    input_types_json  TEXT,                    -- {"required":{"image":"IMAGE",…},"optional":{…}}
    return_types_json TEXT,
    return_names_json TEXT,
    output_node      INTEGER NOT NULL DEFAULT 0 CHECK (output_node IN (0,1)),
    function_name    TEXT,
    is_deprecated    INTEGER NOT NULL DEFAULT 0 CHECK (is_deprecated IN (0,1)),
    is_experimental  INTEGER NOT NULL DEFAULT 0 CHECK (is_experimental IN (0,1)),
    is_api_node      INTEGER NOT NULL DEFAULT 0 CHECK (is_api_node IN (0,1)),

    source_file      TEXT,
    source_lineno    INTEGER,
    source_strategy  TEXT NOT NULL,            -- 'S1'|'S2'|'S3'|'S4'|'S5'|'S6'|'S7'
    sources_json     TEXT,                     -- all contributing strategies
    confidence       TEXT NOT NULL DEFAULT 'declared'
                     CHECK (confidence IN ('declared','inferred','registry')),
    registration     TEXT NOT NULL DEFAULT 'python',   -- v6: 'python'|'javascript'|'frontend'

    workflow_count   INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,

    UNIQUE (package_id, node_id)
);
```

**`registration` (v6) — a node class does not have to come from Python.** Three values, and the distinction is the whole reason the "missing nodes" column can be trusted:

* `python` — a class found in the package's own source by S1–S5, or named by the ComfyUI-Manager registry (S6). The default, and what almost every row is.
* `javascript` — the package ships `web/**/*.js` that registers the type by name (`LiteGraph.registerNodeType("GetNode", …)`). `ComfyUI-KJNodes` registers `GetNode` and `SetNode` this way and defines neither in Python; both were reported as missing packages until v6. Found by strategy **S7** in `app/parsers/node_js.py`, which is `re` over decoded text — no JavaScript is ever executed, exactly as no Python under `custom_nodes/` is ever imported.
* `frontend` — provided by the ComfyUI web client itself: `Note`, `MarkdownNote`, `Reroute`, `PrimitiveNode`. They appear in no `/object_info` response and in no `.py` in any install, so no package can ever supply them. They are attached to the `__comfyui_core__` package with `source_strategy = 'S7'`.

A class that has *any* Python strategy behind it stays `python` even when its package's JavaScript also touches it — only a class with no Python definition anywhere is recorded as provided.

### 5.3 Node indexes

| Index | Driving query |
|---|---|
| `ux_node_classes_pkg_nodeid` (UNIQUE) | upsert; per-package class list |
| `ix_node_classes_nodeid` `(node_id)` | **the missing-node join** — `workflow_nodes.class_type → node_classes.node_id`. With 638 official + 943 custom = ~1,580 rows this is the hottest lookup in workflow analysis. |
| `ix_node_classes_category` `(category, display_name COLLATE NOCASE)` | Nodes tab grouped by category |
| `ix_node_classes_pkg` `(package_id, display_name COLLATE NOCASE)` | package detail |
| `ix_node_packages_name` `(display_name COLLATE NOCASE)` | default sort |
| `ix_node_packages_flags` `(is_official, enabled, has_update)` | facet chips |
| `ix_node_packages_repo` `(repo_url_normalized)` | registry enrichment join |
| `ix_node_packages_fp` `(fingerprint)` | incremental diff |

---

## 6. Workflows

> **Schema v4** adds `origin` and `origin_package` to this table — see §15.1.

```sql
CREATE TABLE workflows (
    id               INTEGER PRIMARY KEY,
    root_id          INTEGER REFERENCES roots(id) ON DELETE SET NULL,
    abs_path         TEXT NOT NULL,
    path_key         TEXT NOT NULL UNIQUE,
    rel_path         TEXT NOT NULL,
    folder           TEXT NOT NULL,
    name             TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'file'
                     CHECK (source IN ('file','embedded','api')),

    format           TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (format IN ('ui','api','both','unknown')),
    schema_version   TEXT,
    node_count       INTEGER NOT NULL DEFAULT 0,
    link_count       INTEGER NOT NULL DEFAULT 0,
    group_count      INTEGER NOT NULL DEFAULT 0,
    has_subgraphs    INTEGER NOT NULL DEFAULT 0 CHECK (has_subgraphs IN (0,1)),
    subgraph_count   INTEGER NOT NULL DEFAULT 0,   -- v6: definitions.subgraphs entries

    title            TEXT,                     -- workflow.extra.title or filename
    author           TEXT,
    description      TEXT,                     -- user or Ollama-generated "what it can do"
    description_source TEXT,
    capability_tags_json TEXT,                 -- ["txt2img","upscale","video","controlnet"]
    positive_prompt  TEXT,
    negative_prompt  TEXT,
    prompt_summary   TEXT,

    -- derived
    missing_node_count  INTEGER NOT NULL DEFAULT 0,
    missing_model_count INTEGER NOT NULL DEFAULT 0,
    is_runnable      INTEGER NOT NULL DEFAULT 1 CHECK (is_runnable IN (0,1)),
    base_model_family TEXT,                    -- inferred from loaders present
    modality         TEXT,

    preview_path     TEXT,
    graph_json       TEXT,                     -- full graph, capped 8 MB; NULL if larger (re-read on demand)
    graph_truncated  INTEGER NOT NULL DEFAULT 0 CHECK (graph_truncated IN (0,1)),

    size             INTEGER NOT NULL,
    mtime_ns         INTEGER NOT NULL,
    fingerprint      TEXT NOT NULL,
    parser_version   INTEGER NOT NULL DEFAULT 0,
    unresolved_inputs INTEGER NOT NULL DEFAULT 0,   -- B1 telemetry

    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    missing_since    INTEGER
);

CREATE TABLE workflow_nodes (
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    class_type    TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1,
    node_class_id INTEGER REFERENCES node_classes(id) ON DELETE SET NULL,
    resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1)),
    PRIMARY KEY (workflow_id, class_type)
) WITHOUT ROWID;
```

### 6.1 `workflow_dependencies` — powers "where is this used"

```sql
CREATE TABLE workflow_dependencies (
    id            INTEGER PRIMARY KEY,
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    dep_kind      TEXT NOT NULL CHECK (dep_kind IN ('model','node','embedding','input_file')),

    ref_name      TEXT NOT NULL,      -- as written in the graph: 'flux1-dev-fp8.safetensors'
    ref_category  TEXT,               -- 'checkpoints' — from the loader's class_type
    via_class     TEXT,               -- 'CheckpointLoaderSimple'
    via_input     TEXT,               -- 'ckpt_name'
    occurrences   INTEGER NOT NULL DEFAULT 1,

    model_id       INTEGER REFERENCES models(id)        ON DELETE SET NULL,
    node_class_id  INTEGER REFERENCES node_classes(id)  ON DELETE SET NULL,

    status        TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (status IN ('satisfied','missing','ambiguous','unknown')),
    match_method  TEXT,               -- 'exact_relpath'|'basename'|'basename_ci'|'fuzzy'|'none'
    UNIQUE (workflow_id, dep_kind, ref_name, via_input)
);
```

**Matching ladder (phase 7), in order, first hit wins:**
1. `normcase(ref_name)` == `normcase(model_files.rel_path)` within the loader's category folder → `exact_relpath`, `satisfied`.
2. `normcase(basename(ref_name))` == `normcase(model_files.filename)` and category matches → `basename`, `satisfied`.
3. Basename match ignoring category → `basename_ci`, `satisfied` (with a note).
4. Multiple basename matches → `ambiguous` (the UI lists candidates).
5. No match → `missing`.

This table is the join that answers both directions:
* **"Which workflows use this model?"** → `WHERE model_id = ?`
* **"What does this workflow need?"** → `WHERE workflow_id = ?`
* **"What's missing across the vault?"** → `WHERE status='missing'`

### 6.2 Workflow indexes

| Index | Driving query |
|---|---|
| `ux_workflows_pathkey` (UNIQUE) | upsert |
| `ix_workflows_name` `(name COLLATE NOCASE)` | default sort |
| `ix_workflows_mtime` `(mtime_ns DESC)` | sort by date |
| `ix_workflows_folder` `(root_id, folder)` | folder tree |
| `ix_workflows_runnable` `(is_runnable, missing_node_count)` | "Broken workflows" facet |
| `ix_wf_deps_model` `(model_id, workflow_id)` WHERE `model_id IS NOT NULL` | **"where is this model used"** — the exact index for `models/{id}/usage` |
| `ix_wf_deps_nodeclass` `(node_class_id)` WHERE `node_class_id IS NOT NULL` | "where is this node used" |
| `ix_wf_deps_wf_status` `(workflow_id, status)` | workflow detail dependency list |
| `ix_wf_deps_missing` `(dep_kind, ref_name)` WHERE `status='missing'` | vault-wide missing-deps report |
| `ix_wf_nodes_class` `(class_type)` | reverse node→workflow lookup |

---

## 7. Outputs

```sql
CREATE TABLE outputs (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    abs_path        TEXT NOT NULL,
    path_key        TEXT NOT NULL UNIQUE,
    rel_path        TEXT NOT NULL,
    folder          TEXT NOT NULL,     -- '' | '3d' | 'seedance_i2v' | … → the album tree
    filename        TEXT NOT NULL,
    ext             TEXT NOT NULL,

    media_kind      TEXT NOT NULL DEFAULT 'other'
                    CHECK (media_kind IN ('image','video','audio','model3d','text','other')),
    mime            TEXT,
    width           INTEGER,
    height          INTEGER,
    duration_ms     INTEGER,
    frame_count     INTEGER,
    has_alpha       INTEGER,
    color_mode      TEXT,

    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    created_at_file INTEGER NOT NULL,   -- st_ctime/birthtime ms
    fingerprint     TEXT NOT NULL,
    parser_version  INTEGER NOT NULL DEFAULT 0,

    -- generation metadata (all via graph_utils; NULL when link-valued/unresolvable)
    has_metadata    INTEGER NOT NULL DEFAULT 0 CHECK (has_metadata IN (0,1)),
    metadata_format TEXT,               -- 'comfy_prompt'|'comfy_workflow'|'a1111'|'exif'|'none'
    positive_prompt TEXT,
    negative_prompt TEXT,
    seed            TEXT,               -- TEXT: seeds exceed INT64 in some nodes
    steps           INTEGER,
    cfg             REAL,
    denoise         REAL,
    sampler         TEXT,
    scheduler       TEXT,
    model_name      TEXT,               -- as written in the graph
    model_id        INTEGER REFERENCES models(id) ON DELETE SET NULL,
    workflow_id     INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
    workflow_hash   TEXT,               -- blake2b of the normalized graph → groups reruns
    node_count      INTEGER,
    unresolved_inputs INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT,               -- {"positive_prompt":{"origin":"link","src":"88:97"}}
    prompt_graph_json TEXT,             -- capped 2 MB, else NULL + re-read on demand

    -- user
    album_id        INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    user_rating     INTEGER CHECK (user_rating BETWEEN 0 AND 5),
    favorite        INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    user_notes      TEXT,
    color_label     TEXT,

    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    missing_since   INTEGER
);

CREATE TABLE output_models (
    output_id INTEGER NOT NULL REFERENCES outputs(id) ON DELETE CASCADE,
    model_id  INTEGER REFERENCES models(id) ON DELETE CASCADE,
    ref_name  TEXT NOT NULL,
    role      TEXT,                     -- 'checkpoint'|'lora'|'vae'|'controlnet'
    strength  REAL,
    PRIMARY KEY (output_id, ref_name)
) WITHOUT ROWID;
```

### 7.1 Output indexes

| Index | Driving query |
|---|---|
| `ux_outputs_pathkey` (UNIQUE) | upsert |
| `ix_outputs_created` `(created_at_file DESC, id DESC)` | **the default grid order.** Composite with `id` so pagination is deterministic when timestamps tie (very common — batch renders share a second). |
| `ix_outputs_folder_created` `(folder, created_at_file DESC)` | album/folder-filtered grid |
| `ix_outputs_kind_created` `(media_kind, created_at_file DESC)` | media-kind facet |
| `ix_outputs_name` `(filename COLLATE NOCASE)` | sort by name |
| `ix_outputs_size` `(size DESC)` | sort by size |
| `ix_outputs_model` `(model_id, created_at_file DESC)` WHERE `model_id IS NOT NULL` | "outputs made with this model" (model detail) |
| `ix_outputs_workflow` `(workflow_id)` WHERE `workflow_id IS NOT NULL` | "outputs from this workflow" |
| `ix_outputs_wfhash` `(workflow_hash)` | group reruns of the same graph |
| `ix_outputs_fav` `(favorite, created_at_file DESC)` WHERE `favorite=1` | Favorites |
| `ix_outputs_album` `(album_id, created_at_file DESC)` WHERE `album_id IS NOT NULL` | album grid |
| `ix_outputs_fp` `(fingerprint)` | incremental diff |
| `ix_output_models_model` `(model_id, output_id)` | reverse usage |

---

## 8. Albums, groups, tags

```sql
CREATE TABLE albums (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('folder','smart','manual','system')),
    scope       TEXT NOT NULL CHECK (scope IN ('models','nodes','workflows','outputs','all')),
    icon        TEXT,
    color       TEXT,
    query_json  TEXT,               -- for kind='smart': a saved filter expression
    sort_order  INTEGER NOT NULL DEFAULT 0,
    item_count  INTEGER NOT NULL DEFAULT 0,   -- denormalized for the rail; refreshed per scan
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (parent_id, scope, name)
);
CREATE INDEX ix_albums_tree ON albums(scope, parent_id, sort_order);

CREATE TABLE album_items (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    uid      TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (album_id, uid)
) WITHOUT ROWID;
CREATE INDEX ix_album_items_uid ON album_items(uid);

CREATE TABLE tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL UNIQUE,   -- lower(name)
    color      TEXT,
    source     TEXT NOT NULL DEFAULT 'user'
               CHECK (source IN ('user','civitai','derived','ollama')),
    use_count  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE asset_tags (
    uid    TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (uid, tag_id)
) WITHOUT ROWID;
CREATE INDEX ix_asset_tags_tag ON asset_tags(tag_id, uid);
```

**System albums** (`kind='system'`, auto-maintained, shown at the top of the left rail with live counts):
`All`, `Recently added`, `Favorites`, `Needs hashing`, `Updates available`, `Missing files`, `Integrity issues`, `Unused models`, `Broken workflows`, `Untagged`.

**Folder albums** (`kind='folder'`) mirror the on-disk tree — this is what gives the rail its "album tree with counts" for `models/loras/…` and `output/seedance_i2v/…`.

---

## 9. Search

```sql
CREATE VIRTUAL TABLE search_fts USING fts5(
    uid UNINDEXED, kind UNINDEXED,
    title, subtitle, body, tags,
    tokenize = "unicode61 remove_diacritics 2 tokenchars '-_.'",
    prefix = '2 3 4'
);

CREATE TABLE search_docs (
    uid        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    text_hash  TEXT NOT NULL,      -- blake2b of the composed doc; skips no-op reindex
    fts_rowid  INTEGER,
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX ix_search_docs_kind ON search_docs(kind);

CREATE TABLE embeddings (
    uid        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    model_id   TEXT NOT NULL,       -- 'all-MiniLM-L6-v2-int8'
    dim        INTEGER NOT NULL,    -- 384
    vec        BLOB NOT NULL,       -- float32 LE, L2-normalized, dim*4 bytes
    text_hash  TEXT NOT NULL,
    created_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX ix_embeddings_kind ON embeddings(kind);
CREATE INDEX ix_embeddings_model ON embeddings(model_id);

CREATE TABLE embed_queue (
    uid        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 10,
    enqueued_at INTEGER NOT NULL
) WITHOUT ROWID;
```

`search_docs` exists so an unchanged item is never re-tokenized or re-embedded: phase 8 computes `text_hash` and skips on match. `embeddings.model_id` means switching embedders later invalidates cleanly (`DELETE WHERE model_id <> ?`).

`search_fts` is a plain (not external-content) FTS5 table: contentless/external-content tables cannot be updated in place by `uid`, and the writer-thread design makes explicit `DELETE`+`INSERT` pairs trivially safe.

---

## 10. Jobs

```sql
CREATE TABLE scan_jobs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('full','incremental','targeted')),
    scope_json    TEXT,                 -- {"phases":["models"],"roots":[1]}
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','completed','cancelled','failed','interrupted')),
    phase         TEXT,
    phase_cursor_json TEXT,
    items_total   INTEGER NOT NULL DEFAULT 0,
    items_done    INTEGER NOT NULL DEFAULT 0,
    items_skipped INTEGER NOT NULL DEFAULT 0,
    error_count   INTEGER NOT NULL DEFAULT 0,
    stats_json    TEXT,
    trigger       TEXT,                 -- 'user'|'startup'|'wizard'|'watch'|'api'
    started_at    INTEGER,
    finished_at   INTEGER,
    heartbeat_at  INTEGER,
    duration_ms   INTEGER,
    error_message TEXT,
    created_at    INTEGER NOT NULL
);
CREATE INDEX ix_scan_jobs_status ON scan_jobs(status, created_at DESC);

CREATE TABLE scan_errors (
    id       INTEGER PRIMARY KEY,
    job_id   INTEGER NOT NULL REFERENCES scan_jobs(id) ON DELETE CASCADE,
    phase    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    abs_path TEXT,
    code     TEXT NOT NULL,
    message  TEXT NOT NULL,
    traceback_head TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX ix_scan_errors_job  ON scan_errors(job_id, code);
CREATE INDEX ix_scan_errors_code ON scan_errors(code, created_at DESC);

CREATE TABLE hash_jobs (
    id            INTEGER PRIMARY KEY,
    model_file_id INTEGER NOT NULL REFERENCES model_files(id) ON DELETE CASCADE,
    batch_id      TEXT,
    priority      INTEGER NOT NULL DEFAULT 10,
    state         TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued','running','done','failed','cancelled')),
    size          INTEGER NOT NULL,
    bytes_done    INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT,
    error_message TEXT,
    enqueued_at   INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER,
    UNIQUE (model_file_id)
);
CREATE INDEX ix_hash_jobs_pick  ON hash_jobs(state, priority, enqueued_at);
CREATE INDEX ix_hash_jobs_batch ON hash_jobs(batch_id, state);
```
`ix_hash_jobs_pick` is exactly the queue-drain query: `SELECT … WHERE state='queued' ORDER BY priority, enqueued_at LIMIT 1` — fully index-served.

---

## 11. Caches

```sql
CREATE TABLE thumb_cache (
    uid         TEXT NOT NULL,
    size        INTEGER NOT NULL,          -- 160|320|640
    cache_path  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,             -- invalidation
    bytes       INTEGER NOT NULL,
    width       INTEGER, height INTEGER,
    generated_at INTEGER NOT NULL,
    last_access_at INTEGER NOT NULL,       -- LRU GC
    PRIMARY KEY (uid, size)
) WITHOUT ROWID;
CREATE INDEX ix_thumb_lru ON thumb_cache(last_access_at);

CREATE TABLE http_cache (
    cache_key   TEXT PRIMARY KEY,          -- 'civitai:hash:ABC…' | 'github:kijai/ComfyUI-KJNodes'
    provider    TEXT NOT NULL,
    status      INTEGER NOT NULL,
    body_json   TEXT,
    etag        TEXT,
    fetched_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    error       TEXT
) WITHOUT ROWID;
CREATE INDEX ix_http_cache_exp ON http_cache(provider, expires_at);

CREATE TABLE trash_items (
    id             INTEGER PRIMARY KEY,
    uid            TEXT NOT NULL,
    kind           TEXT NOT NULL,
    original_path  TEXT NOT NULL,
    trash_path     TEXT NOT NULL,
    size           INTEGER NOT NULL,
    root_id        INTEGER REFERENCES roots(id) ON DELETE SET NULL,
    payload_json   TEXT,                   -- the deleted DB row, for full restore
    deleted_at     INTEGER NOT NULL,
    purge_after    INTEGER NOT NULL
);
CREATE INDEX ix_trash_purge ON trash_items(purge_after);
```
`payload_json` lets restore rebuild the DB row without waiting for a rescan.

---

## 12. Derived-count maintenance

`models.workflow_count`, `models.output_count`, `node_classes.workflow_count`, `node_packages.workflow_count`, `albums.item_count` are **denormalized** and refreshed by a single set of `UPDATE … FROM (SELECT … GROUP BY …)` statements at the end of phase 7, plus incrementally on file ops.

**Decision: denormalized counts, no triggers.** Counting on read would make the left rail (which shows a count for every one of ~60 groups) issue 60 aggregate queries per render; maintaining them via triggers would fire on every one of ~4,000 scan upserts. One batch recompute at the end of the scan costs ~30 ms and is exact.

---

## 13. Views

```sql
CREATE VIEW v_model_list AS
SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, m.base_model_variant,
       m.modality, m.architecture_label, m.arch_source, m.precision, m.quantization,
       m.param_count_primary, m.param_count_total, m.total_size, m.favorite, m.user_rating,
       m.has_update, m.integrity, m.arch_confidence, m.workflow_count, m.output_count,
       m.is_bundled, m.is_adapter, m.civitai_state, m.civitai_model_id, m.civitai_url,
       m.updated_at, m.missing_since, m.color_label,
       m.rating, m.download_count,                    -- v7: Civitai community stats
       f.abs_path, f.rel_path, f.folder, f.filename, f.ext, f.size AS file_size,
       f.hash_state, f.autov2, f.sha256, f.mtime_ns, f.preview_path, f.root_id, f.id AS file_id
FROM models m
LEFT JOIN model_files f ON f.id = m.primary_file_id;

CREATE VIEW v_vault_stats AS SELECT
  (SELECT COUNT(*) FROM models      WHERE missing_since IS NULL) AS models,
  (SELECT COUNT(*) FROM model_files WHERE missing_since IS NULL) AS model_files,
  (SELECT COALESCE(SUM(size),0) FROM model_files WHERE missing_since IS NULL) AS models_bytes,
  (SELECT COUNT(*) FROM model_files WHERE hash_state='done')     AS models_hashed,
  (SELECT COUNT(*) FROM node_packages WHERE missing_since IS NULL) AS node_packages,
  (SELECT COUNT(*) FROM node_classes) AS node_classes,
  (SELECT COUNT(*) FROM workflows WHERE missing_since IS NULL)   AS workflows,
  (SELECT COUNT(*) FROM workflows WHERE is_runnable=0)           AS workflows_broken,
  (SELECT COUNT(*) FROM outputs   WHERE missing_since IS NULL)   AS outputs,
  (SELECT COALESCE(SUM(size),0) FROM outputs WHERE missing_since IS NULL) AS outputs_bytes,
  (SELECT COUNT(*) FROM embeddings) AS embedded,
  (SELECT COUNT(*) FROM models WHERE integrity<>'ok') AS integrity_issues;
```
The list endpoints select from `v_model_list`, never from `models JOIN model_files` ad hoc — one definition of "the primary file" everywhere.

---

## 14. Migration from the existing schema (v0/v1 → v2)

The existing DB (`backend/data/asset_vault.db`) has **0 rows in `models`, `nodes`, `workflows`, `output_assets`** (audit-confirmed — the scan never committed). Therefore:

**`m001_initial`** creates schema v2 from scratch in a **new file `backend/data/vault.db`**.

**`m002_import_legacy`** runs once at startup if `asset_vault.db` exists:
1. Copy the `config` table (this preserves the user's `comfyui_path`, `is_configured`, Ollama settings — the only data of value).
2. `SELECT COUNT(*)` each legacy asset table. If all are 0 (the expected case), skip data import entirely.
3. If any are non-empty (a user on a partially working build), import with a best-effort field map: `models.base_model → base_model_family` (normalized through the vocabulary table), `nodes → node_packages` + explode `node_classes` JSON into `node_classes` rows, `output_assets.prompt → outputs.positive_prompt`. Every imported row gets `parser_version = 0`, guaranteeing a full re-parse on the next scan.
4. Rename the legacy file to `asset_vault.db.v1.bak`; never delete it.

**Applied so far:** `m001_initial` (v1), `m002_import_legacy` (v2), `m003_album_identity` (v3), `m004_workflow_origin` (v4 — see §15), `m005_enable_jobs` (v5 — see §16), `m006_provided_nodes` (v6 — see §17), `m007_community_stats` (v7 — see §18).

**Forward path:** each future migration is `mNNN_<name>.py` exporting `VERSION`, `NAME`, and `def up(conn)`. The runner is transactional per migration and refuses to start if `user_version > CODE_SCHEMA_VERSION` (a newer DB opened by an older build) with a clear error rather than corrupting it.

**Rebuild-safe:** FTS5 and `embeddings` are treated as derived caches. `POST /api/v1/search/rebuild` drops and repopulates both from the base tables, so a migration never has to migrate them.

---

## 15. Schema v4 — workflow origin, and the storage view's derived data

`SCHEMA_VERSION = 4`. Applied by `m004_workflow_origin`, which is forward-only and touches no
existing column.

### 15.1 `workflows.origin` / `workflows.origin_package` *(REQUIREMENTS_R2 C8.4)*

```sql
ALTER TABLE workflows ADD COLUMN origin         TEXT NOT NULL DEFAULT 'user';
ALTER TABLE workflows ADD COLUMN origin_package TEXT;
CREATE INDEX IF NOT EXISTS ix_workflows_origin
    ON workflows(origin, name COLLATE NOCASE);
```

| column | values | meaning |
|---|---|---|
| `origin` | `user` \| `bundled` \| `official_template` | where the graph came from |
| `origin_package` | node-package folder name, else `NULL` | set only when `origin = 'bundled'` |

* **`user`** — the owner's own graphs: `<root>\workflows`, `<root>\user\default\workflows`, or a
  manually added workflow root. A bundle the owner *copied* into their own folder stays theirs.
* **`bundled`** — physically inside `custom_nodes\<pkg>\…`. This is a measured fact: the file lives
  in that package's directory. `origin_package` is `<pkg>`.
* **`official_template`** — shipped by ComfyUI itself in the `comfyui_workflow_templates*`
  distributions. **These are catalogued read-only and are not indexed as `workflows` rows**, so the
  value does not normally appear in this table; it exists so the vocabulary is complete and the API
  can use one enum across both sources. Reason: those files live inside the Python distribution
  that runs ComfyUI, and making them vault assets would put ComfyUI's own installed files inside the
  reach of vault rename/move/delete.

**No `CHECK` constraint, deliberately.** SQLite cannot add one to an existing table without a full
table rebuild, and rebuilding `workflows` — which carries ratings, tags, notes and album membership —
to gain a constraint the single writer already enforces is a bad trade. The vocabulary lives in
`app/parsers/workflow_origin.py`, which is the *same* classifier the migration backfill and the
indexing phase both call, so a backfilled row and a freshly scanned row can never disagree.

`PARSER_VERSION_WORKFLOW` is intentionally **not** bumped: the migration backfills every existing
row from its stored `rel_path`, so forcing a re-parse of every workflow would buy nothing.

Measured on the owner's install after the migration: 212 rows → 42 `user`, 170 `bundled` across 23
packages (largest: `ComfyUI-WanVideoWrapper` 44, `ComfyUI-Geekatplay-VideoEditorSuite` 17).

### 15.2 `roots.available` now means "configured **and** reachable" *(C7.3)*

No schema change; a semantic one, and it is the mechanism behind the retention guarantee.

The roots phase already set `available = 0` for a configured root that is not reachable. It now
**also** sets `available = 0` for any root row whose `path_key` is absent from the current
configuration — i.e. a root the owner has pointed away from.

That single change is what makes "rows for the old root are retained" a guarantee rather than an
accident: phase 9 (`prune`) skips every table row whose root is unavailable, so a retired root's
models, workflows and outputs are never flagged `missing_since`, and therefore never hard-deleted by
the 30-day sweep — even if that drive is later disconnected. Root rows themselves are never
deleted, so the UI can always name what is being held (`GET /api/v1/storage/roots`,
`GET /api/v1/comfyui/path-policy`).

Known limitation, stated rather than hidden: `node_packages` has no `root_id`, so packages under a
retired root are still swept normally. Node packages are re-derived by a scan and carry no user
metadata, so nothing irreplaceable is lost.

### 15.3 Storage & maintenance derives, it does not store *(C10)*

The Storage tab adds **no persistent tables**. Every figure is derived at request time from data the
index already holds, plus `shutil.disk_usage` per volume. That is a deliberate choice: a cached
"reclaimable bytes" column would be wrong the moment a file changed, and wrong numbers on a delete
screen are worse than slow ones.

Reads used:

| figure | source |
|---|---|
| space per bucket | `os.scandir` walk of `models/`, `output/`, `input/`, `custom_nodes/`, `temp/`+`user/`, and everything else in the root; cached ~120 s, `?refresh=true` re-walks |
| indexed bytes | `v_vault_stats`, `model_files.size`, `outputs.size` |
| free/total per root | `shutil.disk_usage`, probed **once per distinct volume** — roots can be on different drives (C10.1) |
| unused models | `models.workflow_count = 0 AND models.output_count = 0` — maintained by the links phase, so this is measured, not inferred |
| stale | `model_files.mtime_ns`, `outputs.created_at_file` |
| duplicates | `model_files.sha256` (exact) → `(filename, size)` → filename across differing `root_id` (both inferred) |
| superseded | `models.has_update` / `latest_version_name` (Civitai, measured), plus a version-suffix reading of `model_files.stem` (inferred) |
| orphan outputs | `outputs.model_id IS NULL AND outputs.model_name IS NOT NULL` |
| trash | `trash_items` + the on-disk size of each root's `.vault-trash` |

**One transient temp table.** `storage_model_signals` is created with `CREATE TEMP TABLE` on the
read-only connection and repopulated per request:

```sql
CREATE TEMP TABLE IF NOT EXISTS storage_model_signals (
    model_id              INTEGER PRIMARY KEY,
    dup_method            TEXT,     -- 'sha256' | 'name+size' | 'name across roots'
    dup_group             TEXT,
    dup_confidence        TEXT,     -- 'measured' | 'inferred'
    superseded_by         TEXT,
    superseded_source     TEXT,     -- 'civitai' | 'filename'
    superseded_confidence TEXT
);
```

It lives in SQLite's separate `temp` database — which `PRAGMA temp_store = MEMORY` keeps in RAM — so
it is writable even though the connection opened the main database `mode=ro`, and it never touches
`vault.db`. It exists so `reclaim_score` can be computed **inside** the statement and therefore
sorted and paged correctly; binding a few thousand ids into every query instead would not scale and
would not page. The Python-side signal computation is cached on a cheap fingerprint of
`(models.count, max(updated_at), model_files.count, max(id), hashed count)`, so a paged UI computes
it once.

**`reclaim_score`** (0–100, clamped) is a documented sum, returned to the client as
`meta.weights` so the UI can explain any row without re-deriving the formula:

| signal | weight | confidence |
|---|---:|---|
| no workflow **and** no output reference | +35 | measured |
| size bucket (1 → 25, stepping at 128 MB … 32 GB) | +1…25 | measured |
| age bucket (0 → 20, stepping at 30 / 90 / 180 / 365 / 730 days) | 0…20 | measured |
| duplicate by SHA-256 | +15 | measured |
| duplicate by name+size or name across roots | +10 | inferred |
| superseded by a newer version | +10 | measured (Civitai) / inferred (filename) |
| failed integrity check | +10 | measured |
| output whose source model is gone | +25 | measured |
| output that is not an image or video | +10 | measured |
| output with no workflow and no metadata | +5 | measured |
| **favourite, or rated 4+** | **−40** | measured |

Protected items are **flagged, never hidden** — omitting a favourited 46 GB file from a "largest
files" view would misreport the disk. `include_protected=false` excludes them from a cleanup flow.

The version heuristic deliberately refuses to read precision and quantisation suffixes as versions:
`model_fp8` is not superseded by `model_fp16`, and `_q4_k_m`, `_bf16`, `_pruned`, `_distilled` and
friends are variants the owner chose. Suggesting their deletion would cost hours of re-downloading.

### 15.4 Cleanup writes through the existing path only

`POST /api/v1/storage/cleanup` calls `services/file_ops.delete()` — the same function the UI's
delete button and the MCP `vault_delete` tool call. No new SQL, no second trash, no privileged path.
The rails (explicit selection required, `model`/`output` only, 200-item cap, trash by default,
`confirm:true` for permanent) live in `services/storage_service.cleanup()` rather than in the router,
so every surface inherits them identically.

Sizes are read **before** the delete and attributed per item, so `freed_bytes` on a partial failure
reports only what actually went rather than pro-rating the estimate.

### 15.5 Read-only connections must never hold a transaction

`storage_model_signals` (§15.3) is written on the thread-local **read-only** connection. That is
legal — the `temp` database is writable — but it exposed a defect worth recording, because the
symptom looked nothing like the cause.

Python's `sqlite3` legacy transaction control issues an implicit `BEGIN` before any
INSERT/UPDATE/DELETE, **including a write to a TEMP table**, and never commits it. The connection
therefore sat inside a transaction, WAL pinned its read snapshot, and every subsequent `SELECT` on
that worker thread served data frozen at that instant: `/outputs` `page.total`, `/system/stats`,
facet counts, album counts, the left-rail tree. The database was correct throughout; only the
readers were stuck, and only a restart cleared it. In a Storage & Maintenance view this is the worst
possible failure — the owner deletes 40 GB and the app insists the space is still occupied.

Fixed structurally in `core/db`, not at the call site:

```python
# _configure(), read-only connections only
conn.isolation_level = None      # autocommit: an implicit BEGIN cannot happen

# get_ro()
elif conn.in_transaction:        # belt and braces: an explicit BEGIN from any
    conn.rollback()              # caller cannot outlive its request
```

**Rule for every future query module: a read-only connection must be in autocommit and must never be
left inside a transaction.** If you write to a TEMP table on one, commit before returning
(`storage_query._stage_signals` does, defensively, even though autocommit already guarantees it).

`dbmod.data_version()` exposes `PRAGMA data_version`, which SQLite bumps whenever *another*
connection commits. Since every write in this app goes through the single writer thread on its own
connection, that integer changes on every committed mutation from every path. Derived caches key on
it rather than being invalidated by hand at each mutation site — the same reasoning as
`search/sync.write_synced`, which reindexes inside the write instead of trusting callers to
reindex afterwards. `storage_service.footprint` and `storage_query.model_signals` both use it.

Free space is deliberately **not** cached: `storage_service.volumes()` re-probes on every read.

## 16. Schema v5 — the workflow "Enable" fetch queue *(REQUIREMENTS_R2 C9)*

`PRAGMA user_version = 5`, applied by `m005_enable_jobs`. One new table, created only if absent.
Nothing existing is altered and no row is rewritten, so the migration is forward-only, idempotent,
and cannot touch a rating, a tag or an album.

### 16.1 `enable_jobs`

```sql
CREATE TABLE enable_jobs (
    id              INTEGER PRIMARY KEY,
    batch_id        TEXT NOT NULL,           -- one per confirmed plan
    workflow_id     INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
    item_key        TEXT NOT NULL,           -- the plan's item_id
    kind            TEXT NOT NULL CHECK (kind IN ('model','node_package')),
    ref_name        TEXT NOT NULL,           -- as the workflow refers to it
    category        TEXT,                    -- derived ComfyUI model folder
    provider        TEXT,                    -- workflow_manifest | vault_cache
                                             -- | comfyui_manager_registry
    source_url      TEXT NOT NULL,           -- allowlisted, https, server-derived
    source_host     TEXT NOT NULL,
    expected_size   INTEGER NOT NULL DEFAULT 0,
    expected_sha256 TEXT,                    -- NULL when the source publishes none
    root_id         INTEGER,
    target_abs_path TEXT NOT NULL,           -- derived server-side, never client input
    part_abs_path   TEXT,                    -- '<target>.part' while in flight
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued','running','done','failed','cancelled',
                                     'quarantined','skipped')),
    bytes_done      INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    error_message   TEXT,
    result_json     TEXT,                    -- FetchResult / CloneResult, plus on_conflict
    enqueued_at     INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    UNIQUE (batch_id, item_key)
);
CREATE INDEX ix_enable_jobs_pick  ON enable_jobs(state, enqueued_at);
CREATE INDEX ix_enable_jobs_batch ON enable_jobs(batch_id, state);
CREATE INDEX ix_enable_jobs_wf    ON enable_jobs(workflow_id, state);
```

`ix_enable_jobs_pick` is exactly the queue-drain query
(`SELECT … WHERE state='queued' ORDER BY enqueued_at, id LIMIT 1`), the same shape
`ix_hash_jobs_pick` serves for hashing — this is deliberately the *same* job pattern as
`hash_jobs` (§10), not a second one.

**Why this is a table and not an in-memory list.** A model download is multi-gigabyte and the
owner's drive is 85 % full; a crash, a cancel or an app restart mid-transfer must leave enough
state to resume. `bytes_done` is checkpointed while bytes arrive, and startup recovery moves any
`running` row back to `queued` — the partial bytes on disk are kept because the fetcher re-hashes
the **whole** file at completion and therefore never trusts a resumed prefix.

**What is deliberately *not* stored.** The `plan_token` (see API_CONTRACT §20) lives only in
process memory. It is consent, not data: a restart should invalidate it, and a token that survived
a restart would let a plan the user never re-read start a download.

### 16.2 `scan_jobs` / `scan_errors` carry the Enable job's failures

Each confirmed fetch opens **one** `scan_jobs` row — `kind='targeted'`, `phase='enable'`,
`trigger='enable'`, `scope_json = {"phases":["enable"],"workflow_id":…,"batch_id":…,"items":…}` —
and closes it as `completed` or `failed` when the batch drains.

A download that fails verification writes a `scan_errors` row against that job with
`phase='enable'`, `kind='download'`, `code='INTEGRITY_MISMATCH'` and `abs_path` pointing at the
quarantine slot. Transport and filesystem failures write the same shape with their own code. This
reuses the existing error surface on purpose: a quarantined download belongs in the same list as
every other problem the vault found, not in a private log the owner never opens.

`scan_errors.code` remains free text at the schema level; the scan pipeline still normalises its
own codes through `errors.SCAN_ERROR_CODES`, and the Enable path adds `INTEGRITY_MISMATCH` to what
that column may contain.

### 16.3 `<root>/.vault-quarantine/` — a directory, not a table

A file that fails size or hash verification is moved to
`<root>/.vault-quarantine/<yyyymmdd-HHMMSS>-<8hex>/<name>.part` with a sibling `reason.json`
(`ref_name`, `source_url`, `source_host`, `intended_path`, expected vs actual size and SHA-256,
and the list of problems). It is never placed in a model folder, and it is never deleted
automatically.

Like `.vault-trash`, the directory is excluded from every scan walk (`walker.SKIP_DIRS`) and from
the storage footprint buckets, so quarantined bytes never masquerade as library content. `.part`
was already in `walker.SKIP_EXTS`, so an in-flight download is invisible to the indexer without any
further change.

### 16.4 No new columns on `workflows`

"Is this workflow runnable now?" is still derived from `workflow_dependencies.status` and the
`is_runnable` column phase 7 maintains (§6, §12). The Enable path adds no denormalised state of its
own: after a fetch places files it schedules an incremental scan, and the recheck endpoint reports
both the freshly computed answer and whether that scan is still running.

---

## 17. Provided nodes and subgraphs (v6)

`PRAGMA user_version = 6`, applied by `m006_provided_nodes`. Two `ALTER TABLE … ADD COLUMN`
statements, both with a default: forward-only, idempotent, and no existing row is rewritten.

Both columns exist for the same reason — the vault was reporting node packages as missing when
nothing was missing at all. Three distinct causes produced that, and each one is a different kind of
"this is not an installable dependency":

**1. Subgraph instances → `workflows.subgraph_count`.** A workflow declares its reusable subgraphs
under `definitions.subgraphs`, each with a UUID `id` and a `name`; a node that instantiates one
carries that UUID as its `type`. Those UUIDs were being written to `workflow_nodes` and
`workflow_dependencies` as node classes and then, inevitably, reported missing — 21 distinct UUIDs
over 61 dependency rows on the owner's library, 8 in a single file. `workflow_graph` now collects
every declared id first (walking nested `definitions` as well as the top level, bounded on depth and
count) and treats a matching `type` as an internal reference: counted as a subgraph, never recorded
as a dependency. `subgraph_count` is how many definitions the file declares, so the UI can say
"8 subgraphs" instead of silently dropping them.

**2. Frontend virtual nodes → `node_classes.registration = 'frontend'`.** `Note`, `MarkdownNote`,
`Reroute` and `PrimitiveNode` are drawn by the web client and exist in no `.py` anywhere.
`MarkdownNote` alone produced 117 "missing" rows.

**3. JavaScript-registered nodes → `node_classes.registration = 'javascript'`.** Discovered
statically per package by `app/parsers/node_js.py` (strategy S7) rather than hard-coded, so the list
cannot rot as packages change.

---

## 18. Community stats on the model list (v7)

`PRAGMA user_version = 7`, applied by `m007_community_stats`. No table changes — `v_model_list`
is dropped and recreated with `models.rating` and `models.download_count` appended to its
`SELECT` list. Both columns existed since v1 (populated by the Civitai enrichment job once a
model is hash-matched) but were never selected by the view the list endpoints read from, so the
UI had no way to show them. Recreating a view is cheap and non-destructive; nothing in the base
tables moves.

Neither column changes the matching ladder in §6.1: the frontend and JavaScript classes are real
`node_classes` rows, so phase 7 resolves them through the same `node_id` join as any other class and
`missing_node_count` falls out unchanged. A package that genuinely is not installed is still
`missing` — the fix makes the question correct, it does not stop asking it.
