"""Schema v7 - community metadata reaches the model list.

``models.rating`` and ``models.download_count`` (Civitai enrichment) have been
stored since v1 but never selected by ``v_model_list``, so the UI could not
show them.  A view is cheap to rebuild, so this recreates it with the two
columns appended; nothing in any table changes.
"""

from __future__ import annotations

import sqlite3

VERSION = 7
NAME = "community_stats"

VIEW = """
CREATE VIEW v_model_list AS
SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, m.base_model_variant,
       m.modality, m.architecture_label, m.arch_source, m.precision, m.quantization,
       m.param_count_primary, m.param_count_total, m.total_size, m.favorite, m.user_rating,
       m.has_update, m.integrity, m.arch_confidence, m.workflow_count, m.output_count,
       m.is_bundled, m.is_adapter, m.civitai_state, m.civitai_model_id, m.civitai_url,
       m.updated_at, m.missing_since, m.color_label,
       m.rating, m.download_count,
       f.abs_path, f.rel_path, f.folder, f.filename, f.ext, f.size AS file_size,
       f.hash_state, f.autov2, f.sha256, f.mtime_ns, f.preview_path, f.root_id, f.id AS file_id
FROM models m
LEFT JOIN model_files f ON f.id = m.primary_file_id
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute("DROP VIEW IF EXISTS v_model_list")
    conn.execute(VIEW)
