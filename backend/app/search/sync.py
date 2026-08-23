"""Keep the FTS document in lockstep with the row it describes.

Search sync used to be something each mutation had to remember, so rename, move
and restore silently diverged from the database.  Every write that touches an
indexed row now goes through :func:`write_synced`, which reindexes the affected
uids *inside the same transaction as the write itself* - so a committed row and
its search document can never disagree.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from ..core import db as dbmod
from . import doc_builder, fts

# One single-row query per kind, mirroring the columns the scan phase feeds to
# doc_builder so a mutation-time document is byte-identical to a scan-time one.
_ROW_SQL = {
    "model": (
        "SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, "
        "m.base_model_variant, m.precision, m.quantization, m.modality, "
        "m.architecture_label, m.param_count_primary, m.description, "
        "m.trigger_words_json, f.filename, f.folder "
        "FROM models m LEFT JOIN model_files f ON f.id = m.primary_file_id "
        "WHERE m.id = ?"
    ),
    "node_package": (
        "SELECT id, display_name, folder_name, author, publisher_id, registry_id, "
        "description, long_description FROM node_packages WHERE id = ?"
    ),
    "node_class": (
        "SELECT nc.id, nc.node_id, nc.class_name, nc.display_name, nc.category, "
        "nc.description, nc.input_types_json, nc.return_types_json, "
        "p.display_name AS package_name FROM node_classes nc "
        "JOIN node_packages p ON p.id = nc.package_id WHERE nc.id = ?"
    ),
    "workflow": (
        "SELECT id, name, title, folder, base_model_family, modality, description, "
        "prompt_summary, positive_prompt, capability_tags_json FROM workflows "
        "WHERE id = ?"
    ),
    "output": (
        "SELECT id, filename, folder, media_kind, model_name, positive_prompt, "
        "negative_prompt, sampler, scheduler FROM outputs WHERE id = ?"
    ),
}


def split_uid(uid: str) -> tuple[str, int] | None:
    kind, _sep, num = str(uid).partition(":")
    if kind not in _ROW_SQL:
        return None
    try:
        return kind, int(num)
    except (TypeError, ValueError):
        return None


def _tags_for(conn: sqlite3.Connection, uid: str) -> str:
    rows = conn.execute(
        "SELECT t.name FROM asset_tags a JOIN tags t ON t.id = a.tag_id "
        "WHERE a.uid = ? ORDER BY t.name", (uid,),
    ).fetchall()
    return " ".join(str(r["name"]) for r in rows)


def doc_for(conn: sqlite3.Connection, uid: str) -> doc_builder.SearchDoc | None:
    """Build the search document for ``uid`` from its live row, or None if gone."""
    parsed = split_uid(uid)
    if parsed is None:
        return None
    kind, row_id = parsed
    row = conn.execute(_ROW_SQL[kind], (row_id,)).fetchone()
    if row is None:
        return None
    tags = _tags_for(conn, uid)
    if kind == "model":
        return doc_builder.model_doc(row, tags)
    if kind == "node_package":
        names = [str(r["display_name"] or "") for r in conn.execute(
            "SELECT display_name FROM node_classes WHERE package_id = ? "
            "ORDER BY id LIMIT 40", (row_id,))]
        return doc_builder.node_package_doc(row, " ".join(names), tags)
    if kind == "node_class":
        return doc_builder.node_class_doc(row, str(row["package_name"] or ""), tags)
    if kind == "workflow":
        classes = [str(r["class_type"]) for r in conn.execute(
            "SELECT class_type FROM workflow_nodes WHERE workflow_id = ? LIMIT 60",
            (row_id,))]
        return doc_builder.workflow_doc(row, " ".join(classes), tags)
    return doc_builder.output_doc(row, tags)


def sync_uid(conn: sqlite3.Connection, uid: str) -> str:
    """Reindex one uid.  Must run inside the caller's transaction."""
    doc = doc_for(conn, uid)
    if doc is None:
        fts.delete(conn, uid)
        conn.execute("DELETE FROM embed_queue WHERE uid = ?", (uid,))
        return "deleted"
    fts.upsert(conn, doc)
    conn.execute(
        "INSERT OR REPLACE INTO embed_queue(uid,kind,priority,enqueued_at) "
        "VALUES (?,?,?,?)", (doc.uid, doc.kind, 5, dbmod.now_ms()),
    )
    return "upserted"


def write_synced(fn: Callable[[sqlite3.Connection, Callable[[str], None]], Any],
                 uids: Iterable[str] = (), *, timeout: float = 300.0) -> Any:
    """Run a mutation and reindex every uid it touched, atomically.

    ``fn(conn, touch)`` performs the write; call ``touch(uid)`` for any uid whose
    identity is only known once the write has run (a restored row's new id, for
    instance).  BEGIN/COMMIT are owned here: ``fn`` must not commit.
    """
    tracked: list[str] = [str(u) for u in uids if u]

    def _op(conn: sqlite3.Connection) -> Any:
        def touch(uid: str) -> None:
            if uid and str(uid) not in tracked:
                tracked.append(str(uid))

        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, touch)
            seen: set[str] = set()
            for uid in tracked:
                if uid in seen or split_uid(uid) is None:
                    continue
                seen.add(uid)
                sync_uid(conn, uid)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return result

    return dbmod.writer().run(_op, timeout)


def resync(uids: Iterable[str]) -> int:
    """Reindex uids outside any other write (used by enrichment jobs)."""
    targets = [str(u) for u in uids if u]
    if not targets:
        return 0

    def _fn(_conn: sqlite3.Connection, _touch) -> int:
        return len(targets)

    return int(write_synced(_fn, targets))
