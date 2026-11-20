"""Schema v10 - official ComfyUI templates as read-only workflow rows.

Until v9 the templates shipped in the ``comfyui_workflow_templates*``
distributions were only *catalogued*: listed by filename, never linked to a
model or to an image.  From v10 they are indexed into ``workflows`` with
``origin = 'official_template'`` so the same dependency ladder that resolves a
user workflow's models resolves theirs, and outputs generated from a template
trace back to it.

They stay strictly read-only.  Rename, move and delete refuse them in
``services/file_ops.py``; the vault never writes into ComfyUI's own install.

Forward-only and non-destructive, like v4: columns are appended with
``ALTER TABLE`` and one new table is created.  No row is deleted and no table
holding the owner's ratings, tags or notes is rebuilt.

``workflows.graph_uuid``
    The top-level ``id`` the ComfyUI frontend stamps on every saved graph.  A
    template keeps it when opened, so a user's saved copy of a template and
    every image rendered from it carry the same value.
``outputs.workflow_uuid``
    The same ``id``, read from the UI workflow embedded in a PNG (or a sidecar).
``models.template_count``
    Templates referencing the model, counted apart from ``workflow_count`` so
    the Storage "unused models" list still means unused by *your* workflows.
``workflow_templates``
    What ``index.json`` and the manifest say about a template: title,
    description, tags, category, local or API, model size, thumbnails, and
    the download URL ComfyUI itself would offer for each model.

Outputs: ``PARSER_VERSION_OUTPUT`` becomes 5 so files carrying a ComfyUI graph
are re-read once for their workflow id.  Re-reading the other rows would learn
nothing, so they are stamped as already current here - on a library with two
million outputs and a few thousand ComfyUI renders that is the difference
between minutes and hours.
"""

from __future__ import annotations

import sqlite3

VERSION = 10
NAME = "official_templates"

#: The output parser version this migration brings untouched rows up to.  A
#: literal on purpose: if PARSER_VERSION_OUTPUT moves on later, rows stamped
#: here must still read as "parsed by 5", never as the newer version.
OUTPUT_PARSER_V5 = 5

#: Formats whose rows hold an embedded ComfyUI graph and so must be re-read.
GRAPH_FORMATS = ("comfy_prompt", "comfy_workflow")

TEMPLATES_TABLE = """
CREATE TABLE IF NOT EXISTS workflow_templates (
    workflow_id      INTEGER PRIMARY KEY REFERENCES workflows(id) ON DELETE CASCADE,
    template_name    TEXT NOT NULL,
    bundle           TEXT,
    package_version  TEXT,
    category         TEXT,
    category_group   TEXT,
    media_type       TEXT,
    description      TEXT,
    tags_json        TEXT,
    model_families_json TEXT,
    model_downloads_json TEXT,
    required_size    INTEGER,
    open_source      INTEGER CHECK (open_source IN (0,1)),
    tutorial_url     TEXT,
    min_comfyui      TEXT,
    released_on      TEXT,
    usage_rank       INTEGER,
    thumbnails_json  TEXT,
    updated_at       INTEGER NOT NULL
)
"""


#: Templates are counted apart.  ``workflows`` / ``workflows_broken`` drive the
#: status bar and the "Needs attention" button, and a template that names a
#: model the owner never downloaded is not something that needs their
#: attention.  ``workflows_broken`` also gains the ``missing_since`` filter it
#: always lacked: a deleted file was still counted as a broken workflow.
STATS_VIEW = """
CREATE VIEW v_vault_stats AS SELECT
  (SELECT COUNT(*) FROM models      WHERE missing_since IS NULL) AS models,
  (SELECT COUNT(*) FROM model_files WHERE missing_since IS NULL) AS model_files,
  (SELECT COALESCE(SUM(size),0) FROM model_files WHERE missing_since IS NULL) AS models_bytes,
  (SELECT COUNT(*) FROM model_files WHERE hash_state='done')     AS models_hashed,
  (SELECT COUNT(*) FROM node_packages WHERE missing_since IS NULL) AS node_packages,
  (SELECT COUNT(*) FROM node_classes) AS node_classes,
  (SELECT COUNT(*) FROM workflows WHERE missing_since IS NULL
     AND origin <> 'official_template')                          AS workflows,
  (SELECT COUNT(*) FROM workflows WHERE is_runnable=0 AND missing_since IS NULL
     AND origin <> 'official_template')                          AS workflows_broken,
  (SELECT COUNT(*) FROM workflows WHERE missing_since IS NULL
     AND origin = 'official_template')                           AS templates,
  (SELECT COUNT(*) FROM outputs   WHERE missing_since IS NULL)   AS outputs,
  (SELECT COALESCE(SUM(size),0) FROM outputs WHERE missing_since IS NULL) AS outputs_bytes,
  (SELECT COUNT(*) FROM embeddings) AS embedded,
  (SELECT COUNT(*) FROM models WHERE integrity<>'ok') AS integrity_issues
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}


def up(conn: sqlite3.Connection) -> None:
    wf = _columns(conn, "workflows")
    if "graph_uuid" not in wf:
        conn.execute("ALTER TABLE workflows ADD COLUMN graph_uuid TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workflows_graph_uuid "
                 "ON workflows(graph_uuid) WHERE graph_uuid IS NOT NULL")

    out = _columns(conn, "outputs")
    if "workflow_uuid" not in out:
        conn.execute("ALTER TABLE outputs ADD COLUMN workflow_uuid TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outputs_workflow_uuid "
                 "ON outputs(workflow_uuid) WHERE workflow_uuid IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outputs_workflow_id "
                 "ON outputs(workflow_id) WHERE workflow_id IS NOT NULL")

    models = _columns(conn, "models")
    if "template_count" not in models:
        conn.execute(
            "ALTER TABLE models ADD COLUMN template_count INTEGER NOT NULL DEFAULT 0")

    conn.execute("DROP VIEW IF EXISTS v_vault_stats")
    conn.execute(STATS_VIEW)

    conn.execute(TEMPLATES_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workflow_templates_name "
                 "ON workflow_templates(template_name)")

    # Only rows that carry a ComfyUI graph can yield a workflow id; everything
    # else is already as current as a re-read would make it.
    ph = ",".join("?" * len(GRAPH_FORMATS))
    conn.execute(
        f"UPDATE outputs SET parser_version = ? WHERE parser_version < ? "  # noqa: S608
        f"AND (metadata_format IS NULL OR metadata_format NOT IN ({ph}))",
        (OUTPUT_PARSER_V5, OUTPUT_PARSER_V5, *GRAPH_FORMATS),
    )
