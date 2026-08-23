"""Schema v2 - created from scratch.  See DATA_MODEL.md."""

from __future__ import annotations

import sqlite3

VERSION = 1
NAME = "initial"

SQL = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);

CREATE TABLE config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    value_type TEXT NOT NULL DEFAULT 'str'
               CHECK (value_type IN ('str','int','float','bool','json')),
    updated_at INTEGER NOT NULL
);

CREATE TABLE roots (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN
                   ('comfyui','extra_models','extra_workflows','data')),
    path         TEXT NOT NULL,
    path_key     TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    category     TEXT,
    is_default   INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
    source       TEXT NOT NULL CHECK (source IN ('config','yaml','manual')),
    available    INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0,1)),
    last_seen_at INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX ix_roots_kind ON roots(kind, available);

CREATE TABLE models (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    canonical_key       TEXT,
    primary_file_id     INTEGER,
    category            TEXT NOT NULL,
    model_role          TEXT NOT NULL DEFAULT 'unknown',
    base_model_family   TEXT NOT NULL DEFAULT 'Unknown',
    base_model_variant  TEXT,
    modality            TEXT NOT NULL DEFAULT 'unknown',
    architecture_label  TEXT,
    arch_source         TEXT NOT NULL DEFAULT 'none'
                        CHECK (arch_source IN ('none','metadata','structural','shape','prior','civitai')),
    arch_confidence     REAL NOT NULL DEFAULT 0.0,
    is_adapter          INTEGER NOT NULL DEFAULT 0 CHECK (is_adapter IN (0,1)),
    adapter_format      TEXT,
    adapter_rank        INTEGER,
    adapter_alpha       REAL,
    is_bundled          INTEGER NOT NULL DEFAULT 0 CHECK (is_bundled IN (0,1)),
    components_json     TEXT,
    param_count_primary INTEGER,
    param_count_total   INTEGER,
    tensor_count        INTEGER,
    precision           TEXT,
    quantization        TEXT,
    resolution_hint     TEXT,
    prediction_type     TEXT,
    header_metadata_json TEXT,
    detection_signals_json TEXT,
    integrity           TEXT NOT NULL DEFAULT 'ok'
                        CHECK (integrity IN ('ok','invalid_header','not_a_model',
                                             'truncated','unreadable','unsupported_format')),
    integrity_note      TEXT,
    civitai_model_id    INTEGER,
    civitai_version_id  INTEGER,
    civitai_url         TEXT,
    civitai_state       TEXT NOT NULL DEFAULT 'none'
                        CHECK (civitai_state IN ('none','pending','matched','not_found','error','stale')),
    civitai_checked_at  INTEGER,
    description         TEXT,
    description_source  TEXT,
    usage_notes         TEXT,
    trigger_words_json  TEXT,
    recommended_settings_json TEXT,
    download_url        TEXT,
    license_text        TEXT,
    nsfw                INTEGER NOT NULL DEFAULT 0 CHECK (nsfw IN (0,1)),
    rating              REAL,
    download_count      INTEGER,
    has_update          INTEGER NOT NULL DEFAULT 0 CHECK (has_update IN (0,1)),
    latest_version_name TEXT,
    latest_version_id   INTEGER,
    latest_version_notes TEXT,
    latest_version_benefits TEXT,
    user_notes          TEXT,
    user_rating         INTEGER CHECK (user_rating BETWEEN 0 AND 5),
    favorite            INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    color_label         TEXT,
    workflow_count      INTEGER NOT NULL DEFAULT 0,
    output_count        INTEGER NOT NULL DEFAULT 0,
    file_count          INTEGER NOT NULL DEFAULT 1,
    total_size          INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    missing_since       INTEGER
);

CREATE TABLE model_files (
    id             INTEGER PRIMARY KEY,
    model_id       INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    root_id        INTEGER NOT NULL REFERENCES roots(id)  ON DELETE CASCADE,
    abs_path       TEXT NOT NULL,
    path_key       TEXT NOT NULL UNIQUE,
    rel_path       TEXT NOT NULL,
    folder         TEXT NOT NULL,
    filename       TEXT NOT NULL,
    stem           TEXT NOT NULL,
    ext            TEXT NOT NULL,
    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    ctime_ns       INTEGER,
    fingerprint    TEXT NOT NULL,
    format         TEXT NOT NULL,
    header_parsed  INTEGER NOT NULL DEFAULT 0 CHECK (header_parsed IN (0,1)),
    parser_version INTEGER NOT NULL DEFAULT 0,
    hash_state     TEXT NOT NULL DEFAULT 'unhashed'
                   CHECK (hash_state IN ('unhashed','queued','hashing','done','failed','stale')),
    sha256         TEXT,
    autov2         TEXT,
    blake3         TEXT,
    probe_sha256   TEXT,
    hashed_at      INTEGER,
    hash_error     TEXT,
    hash_bytes_done INTEGER NOT NULL DEFAULT 0,
    preview_path   TEXT,
    sidecar_json   TEXT,
    first_seen_at  INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    missing_since  INTEGER
);
CREATE INDEX ix_model_files_model      ON model_files(model_id);
CREATE INDEX ix_model_files_fp         ON model_files(fingerprint);
CREATE INDEX ix_model_files_hash_state ON model_files(hash_state, id);
CREATE INDEX ix_model_files_sha        ON model_files(sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX ix_model_files_probe      ON model_files(size, probe_sha256);
CREATE INDEX ix_model_files_root_folder ON model_files(root_id, folder);
CREATE INDEX ix_model_files_filename   ON model_files(filename COLLATE NOCASE);

CREATE INDEX ix_models_cat_name  ON models(category, name COLLATE NOCASE);
CREATE INDEX ix_models_family_cat ON models(base_model_family, category);
CREATE INDEX ix_models_role      ON models(model_role);
CREATE INDEX ix_models_updated   ON models(updated_at DESC);
CREATE INDEX ix_models_size      ON models(total_size DESC);
CREATE INDEX ix_models_fav       ON models(favorite, updated_at DESC) WHERE favorite=1;
CREATE INDEX ix_models_missing   ON models(missing_since) WHERE missing_since IS NOT NULL;
CREATE INDEX ix_models_update    ON models(has_update) WHERE has_update=1;
CREATE INDEX ix_models_integrity ON models(integrity) WHERE integrity<>'ok';
CREATE INDEX ix_models_civitai   ON models(civitai_model_id);

CREATE TABLE node_packages (
    id                INTEGER PRIMARY KEY,
    folder_name       TEXT NOT NULL,
    path_key          TEXT NOT NULL UNIQUE,
    abs_path          TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    author            TEXT,
    publisher_id      TEXT,
    registry_id       TEXT,
    description       TEXT,
    long_description  TEXT,
    icon_url          TEXT,
    homepage_url      TEXT,
    license           TEXT,
    is_official       INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0,1)),
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    disabled_reason   TEXT,
    is_single_file    INTEGER NOT NULL DEFAULT 0 CHECK (is_single_file IN (0,1)),
    repo_url          TEXT,
    repo_url_normalized TEXT,
    repo_url_suspect  INTEGER NOT NULL DEFAULT 0 CHECK (repo_url_suspect IN (0,1)),
    git_branch        TEXT,
    git_commit        TEXT,
    git_commit_at     INTEGER,
    git_dirty         INTEGER,
    last_fetch_at     INTEGER,
    installed_version TEXT,
    latest_version    TEXT,
    latest_commit     TEXT,
    commits_behind    INTEGER,
    has_update        INTEGER NOT NULL DEFAULT 0 CHECK (has_update IN (0,1)),
    update_notes      TEXT,
    update_checked_at INTEGER,
    update_check_state TEXT NOT NULL DEFAULT 'none'
                      CHECK (update_check_state IN ('none','pending','ok','rate_limited','error','offline','suspect_remote')),
    python_deps_json  TEXT,
    deps_satisfied    INTEGER,
    deps_missing_json TEXT,
    has_web_directory INTEGER NOT NULL DEFAULT 0 CHECK (has_web_directory IN (0,1)),
    class_count       INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'ok'
                      CHECK (extraction_status IN ('ok','partial','registry_only',
                                                   'no_classes_found','empty_package','error')),
    extraction_strategies_json TEXT,
    source_breakdown_json TEXT,
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
CREATE INDEX ix_node_packages_name  ON node_packages(display_name COLLATE NOCASE);
CREATE INDEX ix_node_packages_flags ON node_packages(is_official, enabled, has_update);
CREATE INDEX ix_node_packages_repo  ON node_packages(repo_url_normalized);
CREATE INDEX ix_node_packages_fp    ON node_packages(fingerprint);

CREATE TABLE node_classes (
    id               INTEGER PRIMARY KEY,
    package_id       INTEGER NOT NULL REFERENCES node_packages(id) ON DELETE CASCADE,
    node_id          TEXT NOT NULL,
    class_name       TEXT,
    display_name     TEXT,
    category         TEXT,
    description      TEXT,
    input_types_json  TEXT,
    return_types_json TEXT,
    return_names_json TEXT,
    output_node      INTEGER NOT NULL DEFAULT 0 CHECK (output_node IN (0,1)),
    function_name    TEXT,
    is_deprecated    INTEGER NOT NULL DEFAULT 0 CHECK (is_deprecated IN (0,1)),
    is_experimental  INTEGER NOT NULL DEFAULT 0 CHECK (is_experimental IN (0,1)),
    is_api_node      INTEGER NOT NULL DEFAULT 0 CHECK (is_api_node IN (0,1)),
    source_file      TEXT,
    source_lineno    INTEGER,
    source_strategy  TEXT NOT NULL,
    sources_json     TEXT,
    confidence       TEXT NOT NULL DEFAULT 'declared'
                     CHECK (confidence IN ('declared','inferred','registry')),
    workflow_count   INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (package_id, node_id)
);
CREATE INDEX ix_node_classes_nodeid   ON node_classes(node_id);
CREATE INDEX ix_node_classes_category ON node_classes(category, display_name COLLATE NOCASE);
CREATE INDEX ix_node_classes_pkg      ON node_classes(package_id, display_name COLLATE NOCASE);

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
    title            TEXT,
    author           TEXT,
    description      TEXT,
    description_source TEXT,
    capability_tags_json TEXT,
    positive_prompt  TEXT,
    negative_prompt  TEXT,
    prompt_summary   TEXT,
    missing_node_count  INTEGER NOT NULL DEFAULT 0,
    missing_model_count INTEGER NOT NULL DEFAULT 0,
    is_runnable      INTEGER NOT NULL DEFAULT 1 CHECK (is_runnable IN (0,1)),
    base_model_family TEXT,
    modality         TEXT,
    preview_path     TEXT,
    graph_json       TEXT,
    graph_truncated  INTEGER NOT NULL DEFAULT 0 CHECK (graph_truncated IN (0,1)),
    size             INTEGER NOT NULL,
    mtime_ns         INTEGER NOT NULL,
    fingerprint      TEXT NOT NULL,
    parser_version   INTEGER NOT NULL DEFAULT 0,
    unresolved_inputs INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    missing_since    INTEGER
);
CREATE INDEX ix_workflows_name     ON workflows(name COLLATE NOCASE);
CREATE INDEX ix_workflows_mtime    ON workflows(mtime_ns DESC);
CREATE INDEX ix_workflows_folder   ON workflows(root_id, folder);
CREATE INDEX ix_workflows_runnable ON workflows(is_runnable, missing_node_count);
CREATE INDEX ix_workflows_fp       ON workflows(fingerprint);

CREATE TABLE workflow_nodes (
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    class_type    TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 1,
    node_class_id INTEGER REFERENCES node_classes(id) ON DELETE SET NULL,
    resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1)),
    PRIMARY KEY (workflow_id, class_type)
) WITHOUT ROWID;
CREATE INDEX ix_wf_nodes_class ON workflow_nodes(class_type);

CREATE TABLE workflow_dependencies (
    id            INTEGER PRIMARY KEY,
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    dep_kind      TEXT NOT NULL CHECK (dep_kind IN ('model','node','embedding','input_file')),
    ref_name      TEXT NOT NULL,
    ref_category  TEXT,
    via_class     TEXT,
    via_input     TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    model_id      INTEGER REFERENCES models(id)       ON DELETE SET NULL,
    node_class_id INTEGER REFERENCES node_classes(id) ON DELETE SET NULL,
    status        TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (status IN ('satisfied','missing','ambiguous','unknown')),
    match_method  TEXT,
    UNIQUE (workflow_id, dep_kind, ref_name, via_input)
);
CREATE INDEX ix_wf_deps_model     ON workflow_dependencies(model_id, workflow_id) WHERE model_id IS NOT NULL;
CREATE INDEX ix_wf_deps_nodeclass ON workflow_dependencies(node_class_id) WHERE node_class_id IS NOT NULL;
CREATE INDEX ix_wf_deps_wf_status ON workflow_dependencies(workflow_id, status);
CREATE INDEX ix_wf_deps_missing   ON workflow_dependencies(dep_kind, ref_name) WHERE status='missing';

CREATE TABLE albums (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('folder','smart','manual','system')),
    scope       TEXT NOT NULL CHECK (scope IN ('models','nodes','workflows','outputs','all')),
    icon        TEXT,
    color       TEXT,
    query_json  TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    item_count  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (parent_id, scope, name)
);
CREATE INDEX ix_albums_tree ON albums(scope, parent_id, sort_order);
-- NULL <> NULL in SQLite, so the table-level UNIQUE never dedupes root
-- albums.  This expression index is the constraint that actually holds.
CREATE UNIQUE INDEX ux_albums_identity
    ON albums(COALESCE(parent_id, 0), scope, name);

CREATE TABLE outputs (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    abs_path        TEXT NOT NULL,
    path_key        TEXT NOT NULL UNIQUE,
    rel_path        TEXT NOT NULL,
    folder          TEXT NOT NULL,
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
    created_at_file INTEGER NOT NULL,
    fingerprint     TEXT NOT NULL,
    parser_version  INTEGER NOT NULL DEFAULT 0,
    has_metadata    INTEGER NOT NULL DEFAULT 0 CHECK (has_metadata IN (0,1)),
    metadata_format TEXT,
    positive_prompt TEXT,
    negative_prompt TEXT,
    seed            TEXT,
    steps           INTEGER,
    cfg             REAL,
    denoise         REAL,
    sampler         TEXT,
    scheduler       TEXT,
    model_name      TEXT,
    model_id        INTEGER REFERENCES models(id) ON DELETE SET NULL,
    workflow_id     INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
    workflow_hash   TEXT,
    node_count      INTEGER,
    unresolved_inputs INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT,
    prompt_graph_json TEXT,
    album_id        INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    user_rating     INTEGER CHECK (user_rating BETWEEN 0 AND 5),
    favorite        INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0,1)),
    user_notes      TEXT,
    color_label     TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    missing_since   INTEGER
);
CREATE INDEX ix_outputs_created        ON outputs(created_at_file DESC, id DESC);
CREATE INDEX ix_outputs_folder_created ON outputs(folder, created_at_file DESC);
CREATE INDEX ix_outputs_kind_created   ON outputs(media_kind, created_at_file DESC);
CREATE INDEX ix_outputs_name           ON outputs(filename COLLATE NOCASE);
CREATE INDEX ix_outputs_size           ON outputs(size DESC);
CREATE INDEX ix_outputs_model          ON outputs(model_id, created_at_file DESC) WHERE model_id IS NOT NULL;
CREATE INDEX ix_outputs_workflow       ON outputs(workflow_id) WHERE workflow_id IS NOT NULL;
CREATE INDEX ix_outputs_wfhash         ON outputs(workflow_hash);
CREATE INDEX ix_outputs_fav            ON outputs(favorite, created_at_file DESC) WHERE favorite=1;
CREATE INDEX ix_outputs_album          ON outputs(album_id, created_at_file DESC) WHERE album_id IS NOT NULL;
CREATE INDEX ix_outputs_fp             ON outputs(fingerprint);

CREATE TABLE output_models (
    output_id INTEGER NOT NULL REFERENCES outputs(id) ON DELETE CASCADE,
    model_id  INTEGER REFERENCES models(id) ON DELETE CASCADE,
    ref_name  TEXT NOT NULL,
    role      TEXT,
    strength  REAL,
    PRIMARY KEY (output_id, ref_name)
) WITHOUT ROWID;
CREATE INDEX ix_output_models_model ON output_models(model_id, output_id);

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
    name_key   TEXT NOT NULL UNIQUE,
    color      TEXT,
    source     TEXT NOT NULL DEFAULT 'user'
               CHECK (source IN ('user','civitai','derived','ollama')),
    use_count  INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE asset_tags (
    uid      TEXT NOT NULL,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (uid, tag_id)
) WITHOUT ROWID;
CREATE INDEX ix_asset_tags_tag ON asset_tags(tag_id, uid);

CREATE VIRTUAL TABLE search_fts USING fts5(
    uid UNINDEXED, kind UNINDEXED,
    title, subtitle, body, tags,
    tokenize = "unicode61 remove_diacritics 2 tokenchars '-_.'",
    prefix = '2 3 4'
);

CREATE TABLE search_docs (
    uid        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    text_hash  TEXT NOT NULL,
    fts_rowid  INTEGER,
    updated_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX ix_search_docs_kind ON search_docs(kind);

CREATE TABLE embeddings (
    uid        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    model_id   TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    text_hash  TEXT NOT NULL,
    created_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX ix_embeddings_kind  ON embeddings(kind);
CREATE INDEX ix_embeddings_model ON embeddings(model_id);

CREATE TABLE embed_queue (
    uid         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 10,
    enqueued_at INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE scan_jobs (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('full','incremental','targeted')),
    scope_json    TEXT,
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','running','completed','cancelled','failed','interrupted')),
    phase         TEXT,
    phase_cursor_json TEXT,
    items_total   INTEGER NOT NULL DEFAULT 0,
    items_done    INTEGER NOT NULL DEFAULT 0,
    items_skipped INTEGER NOT NULL DEFAULT 0,
    error_count   INTEGER NOT NULL DEFAULT 0,
    stats_json    TEXT,
    trigger       TEXT,
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

CREATE TABLE thumb_cache (
    uid         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    cache_path  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    width       INTEGER,
    height      INTEGER,
    generated_at   INTEGER NOT NULL,
    last_access_at INTEGER NOT NULL,
    PRIMARY KEY (uid, size)
) WITHOUT ROWID;
CREATE INDEX ix_thumb_lru ON thumb_cache(last_access_at);

CREATE TABLE http_cache (
    cache_key  TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    status     INTEGER NOT NULL,
    body_json  TEXT,
    etag       TEXT,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    error      TEXT
) WITHOUT ROWID;
CREATE INDEX ix_http_cache_exp ON http_cache(provider, expires_at);

CREATE TABLE trash_items (
    id            INTEGER PRIMARY KEY,
    uid           TEXT NOT NULL,
    kind          TEXT NOT NULL,
    original_path TEXT NOT NULL,
    trash_path    TEXT NOT NULL,
    size          INTEGER NOT NULL,
    root_id       INTEGER REFERENCES roots(id) ON DELETE SET NULL,
    payload_json  TEXT,
    deleted_at    INTEGER NOT NULL,
    purge_after   INTEGER NOT NULL
);
CREATE INDEX ix_trash_purge ON trash_items(purge_after);

CREATE TABLE mcp_audit (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    session_id  TEXT,
    transport   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    arguments   TEXT NOT NULL,
    uids        TEXT,
    outcome     TEXT NOT NULL,
    affected    INTEGER DEFAULT 0,
    error_code  TEXT,
    elapsed_ms  INTEGER
);
CREATE INDEX ix_mcp_audit_ts   ON mcp_audit(ts DESC);
CREATE INDEX ix_mcp_audit_tool ON mcp_audit(tool, ts DESC);

CREATE VIEW v_model_list AS
SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, m.base_model_variant,
       m.modality, m.architecture_label, m.arch_source, m.precision, m.quantization,
       m.param_count_primary, m.param_count_total, m.total_size, m.favorite, m.user_rating,
       m.has_update, m.integrity, m.arch_confidence, m.workflow_count, m.output_count,
       m.is_bundled, m.is_adapter, m.civitai_state, m.civitai_model_id, m.civitai_url,
       m.updated_at, m.missing_since, m.color_label,
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
"""


def up(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
