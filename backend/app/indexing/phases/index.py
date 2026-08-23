"""Phase 8 - FTS5 upserts plus embedding enqueue.

``search_docs.text_hash`` means an unchanged item is never re-tokenized or
re-embedded.
"""

from __future__ import annotations

import sqlite3
import time

from ...core import db as dbmod
from ...search import doc_builder, fts

BATCH = 2000

_QUERIES = {
    "model": (
        "SELECT m.id, m.name, m.category, m.model_role, m.base_model_family, "
        "m.base_model_variant, m.precision, m.quantization, m.modality, "
        "m.architecture_label, m.param_count_primary, m.description, "
        "m.trigger_words_json, f.filename, f.folder "
        "FROM models m LEFT JOIN model_files f ON f.id = m.primary_file_id"
    ),
    "node_package": (
        "SELECT id, display_name, folder_name, author, publisher_id, registry_id, "
        "description, long_description FROM node_packages"
    ),
    "node_class": (
        "SELECT nc.id, nc.node_id, nc.class_name, nc.display_name, nc.category, "
        "nc.description, nc.input_types_json, nc.return_types_json, "
        "p.display_name AS package_name "
        "FROM node_classes nc JOIN node_packages p ON p.id = nc.package_id"
    ),
    "workflow": (
        "SELECT id, name, title, folder, base_model_family, modality, description, "
        "prompt_summary, positive_prompt, capability_tags_json FROM workflows"
    ),
    "output": (
        "SELECT id, filename, folder, media_kind, model_name, positive_prompt, "
        "negative_prompt, sampler, scheduler FROM outputs"
    ),
}


def _build_docs(conn: sqlite3.Connection) -> list[doc_builder.SearchDoc]:
    docs: list[doc_builder.SearchDoc] = []

    class_names: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT package_id, display_name FROM node_classes ORDER BY package_id, id"
    ):
        lst = class_names.setdefault(int(r["package_id"]), [])
        if len(lst) < 40:
            lst.append(str(r["display_name"] or ""))

    wf_nodes: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT workflow_id, class_type FROM workflow_nodes ORDER BY workflow_id"
    ):
        lst = wf_nodes.setdefault(int(r["workflow_id"]), [])
        if len(lst) < 60:
            lst.append(str(r["class_type"]))

    tags: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT at.uid, t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
    ):
        tags.setdefault(str(r["uid"]), []).append(str(r["name"]))

    for r in conn.execute(_QUERIES["model"]):
        uid = f"model:{r['id']}"
        docs.append(doc_builder.model_doc(r, " ".join(tags.get(uid, []))))
    for r in conn.execute(_QUERIES["node_package"]):
        uid = f"node_package:{r['id']}"
        docs.append(doc_builder.node_package_doc(
            r, " ".join(class_names.get(int(r["id"]), [])), " ".join(tags.get(uid, []))))
    for r in conn.execute(_QUERIES["node_class"]):
        uid = f"node_class:{r['id']}"
        docs.append(doc_builder.node_class_doc(
            r, str(r["package_name"] or ""), " ".join(tags.get(uid, []))))
    for r in conn.execute(_QUERIES["workflow"]):
        uid = f"workflow:{r['id']}"
        docs.append(doc_builder.workflow_doc(
            r, " ".join(wf_nodes.get(int(r["id"]), [])), " ".join(tags.get(uid, []))))
    for r in conn.execute(_QUERIES["output"]):
        uid = f"output:{r['id']}"
        docs.append(doc_builder.output_doc(r, " ".join(tags.get(uid, []))))
    return docs


def run(ctx) -> dict:
    t0 = time.perf_counter()

    def _collect(conn: sqlite3.Connection) -> tuple[list, dict]:
        docs = _build_docs(conn)
        known = {}
        for r in conn.execute("SELECT uid, text_hash FROM search_docs"):
            known[str(r["uid"])] = str(r["text_hash"])
        return docs, known

    conn_ro = dbmod.get_ro()
    docs, known = _collect(conn_ro)
    stale = [d for d in docs if known.get(d.uid) != d.text_hash]
    live_uids = {d.uid for d in docs}

    written = 0
    for start in range(0, len(stale), BATCH):
        if ctx.cancelled():
            break
        chunk = stale[start:start + BATCH]

        def _op(conn: sqlite3.Connection, chunk=chunk) -> int:
            conn.execute("BEGIN IMMEDIATE")
            n = 0
            for doc in chunk:
                sp = f"sp_fts_{n}"
                conn.execute(f"SAVEPOINT {sp}")
                try:
                    fts.upsert(conn, doc)
                    conn.execute(
                        "INSERT OR REPLACE INTO embed_queue(uid,kind,priority,enqueued_at) "
                        "VALUES (?,?,?,?)", (doc.uid, doc.kind, 10, dbmod.now_ms()),
                    )
                    conn.execute(f"RELEASE {sp}")
                    n += 1
                except sqlite3.Error:
                    conn.execute(f"ROLLBACK TO {sp}")
                    conn.execute(f"RELEASE {sp}")
            conn.commit()
            return n

        try:
            written += int(dbmod.writer().run(_op))
        except BaseException as exc:  # noqa: BLE001
            ctx.record_exception("index", "search", None, exc)
        ctx.bus.publish("progress", {
            "job_id": ctx.job_id, "phase": "index",
            "done": min(start + BATCH, len(stale)), "total": len(stale),
        }, coalesce_key="index")
    ctx.bus.flush("index")

    def _prune(conn: sqlite3.Connection) -> int:
        conn.execute("BEGIN IMMEDIATE")
        removed = 0
        rows = conn.execute("SELECT uid FROM search_docs").fetchall()
        for r in rows:
            uid = str(r["uid"])
            if uid not in live_uids:
                fts.delete(conn, uid)
                conn.execute("DELETE FROM embed_queue WHERE uid = ?", (uid,))
                removed += 1
        conn.commit()
        return removed

    try:
        removed = int(dbmod.writer().run(_prune))
    except BaseException as exc:  # noqa: BLE001
        ctx.record_exception("index", "search", None, exc)
        removed = 0

    return {
        "docs": len(docs), "updated": written, "removed": removed,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
