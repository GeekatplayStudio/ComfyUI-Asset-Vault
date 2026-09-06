"""Schema v9 - expose models.created_at in the model list.

``models.created_at`` has existed since v1 but was never selected by
``v_model_list`` - not in the original definition and not when v7 rebuilt the
view.  ``models_query.SORTS`` maps ``created`` onto it and the Models tab offers
it as "Recently added", so picking that sort raised
``no such column: created_at`` and the request ended as a 500.  Node packages
and workflows query their base tables directly and were never affected.

A view is cheap to rebuild, so this recreates it with the column appended
after ``updated_at``.  Reported by a user who traced the missing column.
"""

from __future__ import annotations

import sqlite3

VERSION = 9
NAME = "model_created"

VIEW = """
CREATE VIEW v_model_list AS
SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, m.base_model_variant,
       m.modality, m.architecture_label, m.arch_source, m.precision, m.quantization,
       m.param_count_primary, m.param_count_total, m.total_size, m.favorite, m.user_rating,
       m.has_update, m.integrity, m.arch_confidence, m.workflow_count, m.output_count,
       m.is_bundled, m.is_adapter, m.civitai_state, m.civitai_model_id, m.civitai_url,
       m.created_at, m.updated_at, m.missing_since, m.color_label,
       m.rating, m.download_count,
       f.abs_path, f.rel_path, f.folder, f.filename, f.ext, f.size AS file_size,
       f.hash_state, f.autov2, f.sha256, f.mtime_ns, f.preview_path, f.root_id, f.id AS file_id
FROM models m
LEFT JOIN model_files f ON f.id = m.primary_file_id
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS v_model_list")
    conn.execute(VIEW)
