"""Phase 7 - pure SQL: resolve dependencies, output models, and derived counts.

Implements the matching ladder from DATA_MODEL 6.1:
exact_relpath -> basename (same category) -> basename_ci -> ambiguous -> missing.
"""

from __future__ import annotations

import os
import sqlite3
import time

from ...core import db as dbmod

_RESET_COUNTS = (
    "UPDATE models SET workflow_count = 0, output_count = 0",
    "UPDATE node_classes SET workflow_count = 0",
    "UPDATE node_packages SET workflow_count = 0",
)

_DERIVED_COUNTS = (
    """UPDATE models SET workflow_count = COALESCE((
           SELECT COUNT(DISTINCT d.workflow_id) FROM workflow_dependencies d
           WHERE d.model_id = models.id), 0)""",
    """UPDATE models SET output_count = COALESCE((
           SELECT COUNT(DISTINCT om.output_id) FROM output_models om
           WHERE om.model_id = models.id), 0)""",
    """UPDATE node_classes SET workflow_count = COALESCE((
           SELECT COUNT(DISTINCT d.workflow_id) FROM workflow_dependencies d
           WHERE d.node_class_id = node_classes.id), 0)""",
    """UPDATE node_packages SET workflow_count = COALESCE((
           SELECT COUNT(DISTINCT nc.workflow_count > 0) FROM node_classes nc
           WHERE nc.package_id = node_packages.id AND nc.workflow_count > 0), 0)""",
    """UPDATE workflows SET missing_model_count = COALESCE((
           SELECT COUNT(*) FROM workflow_dependencies d
           WHERE d.workflow_id = workflows.id AND d.dep_kind='model'
             AND d.status='missing'), 0)""",
    """UPDATE workflows SET missing_node_count = COALESCE((
           SELECT COUNT(*) FROM workflow_dependencies d
           WHERE d.workflow_id = workflows.id AND d.dep_kind='node'
             AND d.status='missing'), 0)""",
    "UPDATE workflows SET is_runnable = CASE WHEN missing_node_count = 0 THEN 1 ELSE 0 END",
)


def _norm(s: str | None) -> str:
    """Case-folded, forward-slash form.

    The trailing ``replace`` is load-bearing: on Windows ``os.path.normcase``
    rewrites ``/`` back to a backslash, so without it ``rsplit("/", 1)`` below never
    splits.  A reference that carries a sub-folder - and the WanVideo workflows
    are full of them, ``WanVideo/Lightx2v/foo.safetensors`` - could then never
    match the file sitting in ``models/loras``, and every one of them was
    reported as a missing model that the user already had.
    """
    return os.path.normcase((s or "").replace("\\", "/").strip()).replace("\\", "/")


def _link_models(conn: sqlite3.Connection) -> dict:
    """Resolve workflow_dependencies.model_id via the matching ladder."""
    by_relpath: dict[tuple[str, str], int] = {}
    by_basename_cat: dict[tuple[str, str], list[int]] = {}
    by_basename: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT f.model_id, f.rel_path, f.filename, m.category FROM model_files f "
        "JOIN models m ON m.id = f.model_id WHERE f.missing_since IS NULL"
    ):
        mid = int(r["model_id"])
        cat = str(r["category"] or "")
        rel = _norm(r["rel_path"])
        base = _norm(r["filename"])
        by_relpath.setdefault((cat, rel), mid)
        by_relpath.setdefault((cat, base), mid)
        by_basename_cat.setdefault((cat, base), []).append(mid)
        by_basename.setdefault(base, []).append(mid)

    updates: list[tuple] = []
    stats = {"satisfied": 0, "missing": 0, "ambiguous": 0}
    for r in conn.execute(
        "SELECT id, ref_name, ref_category FROM workflow_dependencies WHERE dep_kind='model'"
    ):
        ref = _norm(r["ref_name"])
        cat = str(r["ref_category"] or "")
        base = ref.rsplit("/", 1)[-1]
        mid = by_relpath.get((cat, ref)) or by_relpath.get((cat, base))
        method = "exact_relpath" if mid else None
        if mid is None:
            cands = by_basename_cat.get((cat, base)) or []
            if len(cands) == 1:
                mid, method = cands[0], "basename"
            elif len(cands) > 1:
                mid, method = cands[0], "ambiguous"
        if mid is None:
            cands = by_basename.get(base) or []
            if len(cands) == 1:
                mid, method = cands[0], "basename_ci"
            elif len(cands) > 1:
                mid, method = cands[0], "ambiguous"
        if mid is None:
            updates.append((None, "missing", "none", int(r["id"])))
            stats["missing"] += 1
        elif method == "ambiguous":
            updates.append((mid, "ambiguous", "basename_ci", int(r["id"])))
            stats["ambiguous"] += 1
        else:
            updates.append((mid, "satisfied", method, int(r["id"])))
            stats["satisfied"] += 1
    conn.executemany(
        "UPDATE workflow_dependencies SET model_id=?, status=?, match_method=? WHERE id=?",
        updates,
    )
    return stats


def _link_nodes(conn: sqlite3.Connection) -> dict:
    by_node_id: dict[str, int] = {}
    for r in conn.execute(
        "SELECT nc.id, nc.node_id, p.is_official FROM node_classes nc "
        "JOIN node_packages p ON p.id = nc.package_id ORDER BY p.is_official DESC, nc.id"
    ):
        by_node_id.setdefault(str(r["node_id"]), int(r["id"]))

    updates: list[tuple] = []
    node_updates: list[tuple] = []
    stats = {"satisfied": 0, "missing": 0}
    for r in conn.execute(
        "SELECT id, workflow_id, ref_name FROM workflow_dependencies WHERE dep_kind='node'"
    ):
        cid = by_node_id.get(str(r["ref_name"]))
        if cid:
            updates.append((cid, "satisfied", "exact_relpath", int(r["id"])))
            node_updates.append((cid, 1, int(r["workflow_id"]), str(r["ref_name"])))
            stats["satisfied"] += 1
        else:
            updates.append((None, "missing", "none", int(r["id"])))
            node_updates.append((None, 0, int(r["workflow_id"]), str(r["ref_name"])))
            stats["missing"] += 1
    conn.executemany(
        "UPDATE workflow_dependencies SET node_class_id=?, status=?, match_method=? "
        "WHERE id=?", updates,
    )
    conn.executemany(
        "UPDATE workflow_nodes SET node_class_id=?, resolved=? "
        "WHERE workflow_id=? AND class_type=?", node_updates,
    )
    return stats


def _link_outputs(conn: sqlite3.Connection) -> dict:
    by_basename: dict[str, list[int]] = {}
    for r in conn.execute(
        "SELECT model_id, filename FROM model_files WHERE missing_since IS NULL"
    ):
        by_basename.setdefault(_norm(r["filename"]), []).append(int(r["model_id"]))

    om_updates: list[tuple] = []
    matched = 0
    for r in conn.execute("SELECT output_id, ref_name FROM output_models"):
        base = _norm(r["ref_name"]).rsplit("/", 1)[-1]
        cands = by_basename.get(base) or []
        mid = cands[0] if cands else None
        if mid:
            matched += 1
        om_updates.append((mid, int(r["output_id"]), str(r["ref_name"])))
    conn.executemany(
        "UPDATE output_models SET model_id=? WHERE output_id=? AND ref_name=?", om_updates
    )
    conn.execute(
        "UPDATE outputs SET model_id = (SELECT om.model_id FROM output_models om "
        "WHERE om.output_id = outputs.id AND om.model_id IS NOT NULL "
        "ORDER BY CASE WHEN om.role='lora' THEN 1 ELSE 0 END, om.ref_name LIMIT 1)"
    )
    # Outputs -> workflows via the structural graph hash.
    conn.execute(
        "UPDATE outputs SET workflow_id = NULL WHERE workflow_id IS NOT NULL"
    )
    return {"output_model_links": matched}


def run(ctx) -> dict:
    t0 = time.perf_counter()

    def _op(conn: sqlite3.Connection) -> dict:
        conn.execute("BEGIN IMMEDIATE")
        stats: dict = {}
        try:
            stats["models"] = _link_models(conn)
            stats["nodes"] = _link_nodes(conn)
            stats["outputs"] = _link_outputs(conn)
            for sql in _RESET_COUNTS:
                conn.execute(sql)
            for sql in _DERIVED_COUNTS:
                conn.execute(sql)
            conn.execute(
                "UPDATE node_packages SET workflow_count = COALESCE((SELECT COUNT(*) "
                "FROM (SELECT DISTINCT d.workflow_id FROM workflow_dependencies d "
                "JOIN node_classes nc ON nc.id = d.node_class_id "
                "WHERE nc.package_id = node_packages.id)), 0)"
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return stats

    try:
        stats = dbmod.writer().run(_op)
    except BaseException as exc:  # noqa: BLE001 - recorded, never fatal
        ctx.record_exception("links", "link", None, exc)
        stats = {"failed": True}
    stats["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return stats
