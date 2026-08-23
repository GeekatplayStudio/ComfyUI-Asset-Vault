"""Phase 5 - workflow ``.json`` files.

Scans BOTH ``<root>\\workflows`` and ``<root>\\user\\default\\workflows``
(DECISIONS D5), plus every ``custom_nodes/*/[example_]workflows`` tree and any
user-added directory.  All graph reads go through ``graph_utils`` (B1).
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...config import PARSER_VERSION_WORKFLOW
from ...core import config_service
from ...core import db as dbmod
from ...core.fingerprint import file_fingerprint
from ...core.pathsafe import Root, path_key, safe_relpath
from ...parsers import workflow_graph, workflow_origin
from ..service import commit_batches, map_parallel
from ..walker import walk_json

EXAMPLE_DIR_NAMES = ("workflows", "example_workflows", "examples_workflows", "example_wf")

# A ComfyUI workflow always carries one of these markers.  Sniffing the first
# 64 KB keeps unrelated JSON (tokenizer configs, presets) out of the work list
# entirely, so it neither costs a parse nor produces a recurring error row.
WORKFLOW_MARKERS = (b'"nodes"', b'"class_type"', b'"last_node_id"', b'"prompt"')
SNIFF_BYTES = 64 * 1024


def _looks_like_workflow(path: str) -> bool:
    from ...core.pathsafe import long_path

    try:
        with open(long_path(path), "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return True  # unreadable: let the parser produce the error row
    return any(m in head for m in WORKFLOW_MARKERS)


@dataclass
class WorkflowWork:
    abs_path: str
    name: str
    rel_path: str
    folder: str
    root_id: int
    size: int
    mtime_ns: int
    fingerprint: str
    origin: str = "user"
    origin_package: str | None = None
    result: workflow_graph.WorkflowResult | None = None
    deps: list[dict] = field(default_factory=list)


def _sources(ctx) -> list[tuple[Path, Root]]:
    cfg = ctx.cfg
    out = list(config_service.workflow_dirs(cfg))
    seen = {path_key(p) for p, _r in out}
    for cn_dir, root in config_service.custom_nodes_dirs(cfg):
        try:
            entries = list(os.scandir(cn_dir))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            for name in EXAMPLE_DIR_NAMES:
                cand = Path(e.path) / name
                key = path_key(cand)
                if key in seen:
                    continue
                try:
                    if cand.is_dir():
                        seen.add(key)
                        out.append((cand, root))
                except OSError:
                    continue
    return out


def _analyze(work: WorkflowWork) -> WorkflowWork:
    work.result = workflow_graph.analyze(work.abs_path, name=work.name)
    if work.result.ok:
        work.deps = work.result.dependencies
    return work


def _upsert(conn: sqlite3.Connection, work: WorkflowWork) -> int | None:
    b = dbmod.bind
    now = dbmod.now_ms()
    r = work.result
    if r is None or not r.ok:
        return None
    s = r.summary
    values = (
        b(work.root_id, kind="int"), b(work.abs_path), b(path_key(work.abs_path)),
        b(work.rel_path), b(work.folder), b(work.name), "file",
        b(work.origin), b(work.origin_package),
        b(r.fmt if r.fmt in ("ui", "api", "both", "unknown") else "unknown"),
        b(r.schema_version), b(r.node_count, kind="int"), b(r.link_count, kind="int"),
        b(r.group_count, kind="int"), b(r.has_subgraphs),
        b(r.title), b(r.author), b(r.capability_tags, kind="json"),
        b(s.positive_prompt if s else None), b(s.negative_prompt if s else None),
        b(workflow_graph.prompt_summary(s)),
        b(r.base_model_family), b(r.modality),
        b(r.graph_json, kind="json"), b(r.graph_truncated),
        b(work.size, kind="int"), b(work.mtime_ns, kind="int"), b(work.fingerprint),
        b(PARSER_VERSION_WORKFLOW, kind="int"), b(r.unresolved_inputs, kind="int"),
        b(now, kind="int"), b(now, kind="int"),
    )
    conn.execute(
        "INSERT INTO workflows(root_id,abs_path,path_key,rel_path,folder,name,source,"
        "origin,origin_package,"
        "format,schema_version,node_count,link_count,group_count,has_subgraphs,title,"
        "author,capability_tags_json,positive_prompt,negative_prompt,prompt_summary,"
        "base_model_family,modality,graph_json,graph_truncated,size,mtime_ns,fingerprint,"
        "parser_version,unresolved_inputs,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path_key) DO UPDATE SET root_id=excluded.root_id, "
        "abs_path=excluded.abs_path, rel_path=excluded.rel_path, folder=excluded.folder, "
        "name=excluded.name, origin=excluded.origin, "
        "origin_package=excluded.origin_package, format=excluded.format, "
        "schema_version=excluded.schema_version, node_count=excluded.node_count, "
        "link_count=excluded.link_count, group_count=excluded.group_count, "
        "has_subgraphs=excluded.has_subgraphs, title=excluded.title, "
        "author=excluded.author, capability_tags_json=excluded.capability_tags_json, "
        "positive_prompt=excluded.positive_prompt, "
        "negative_prompt=excluded.negative_prompt, prompt_summary=excluded.prompt_summary, "
        "base_model_family=excluded.base_model_family, modality=excluded.modality, "
        "graph_json=excluded.graph_json, graph_truncated=excluded.graph_truncated, "
        "size=excluded.size, mtime_ns=excluded.mtime_ns, fingerprint=excluded.fingerprint, "
        "parser_version=excluded.parser_version, "
        "unresolved_inputs=excluded.unresolved_inputs, updated_at=excluded.updated_at, "
        "missing_since=NULL",
        values,
    )
    row = conn.execute("SELECT id FROM workflows WHERE path_key = ?",
                       (path_key(work.abs_path),)).fetchone()
    if row is None:
        return None
    wf_id = int(row["id"])

    conn.execute("DELETE FROM workflow_nodes WHERE workflow_id = ?", (wf_id,))
    if r.node_types:
        conn.executemany(
            "INSERT INTO workflow_nodes(workflow_id,class_type,count) VALUES (?,?,?)",
            [(wf_id, b(k), b(v, kind="int")) for k, v in r.node_types.items()],
        )
    conn.execute("DELETE FROM workflow_dependencies WHERE workflow_id = ?", (wf_id,))
    seen: dict[tuple, int] = {}
    for dep in work.deps:
        key = (dep.get("dep_kind") or "model", str(dep.get("ref_name") or ""),
               str(dep.get("via_input") or ""))
        seen[key] = seen.get(key, 0) + 1
    written: set[tuple] = set()
    for dep in work.deps:
        key = (dep.get("dep_kind") or "model", str(dep.get("ref_name") or ""),
               str(dep.get("via_input") or ""))
        if key in written or not key[1]:
            continue
        written.add(key)
        conn.execute(
            "INSERT OR IGNORE INTO workflow_dependencies(workflow_id,dep_kind,ref_name,"
            "ref_category,via_class,via_input,occurrences) VALUES (?,?,?,?,?,?,?)",
            (wf_id, b(key[0]), b(key[1]), b(dep.get("category")),
             b(dep.get("via_class")), b(key[2]), b(seen[key], kind="int")),
        )
    for class_type in r.node_types:
        conn.execute(
            "INSERT OR IGNORE INTO workflow_dependencies(workflow_id,dep_kind,ref_name,"
            "via_class,via_input,occurrences) VALUES (?,'node',?,?,'',?)",
            (wf_id, b(class_type), b(class_type),
             b(r.node_types[class_type], kind="int")),
        )
    return wf_id


def run(ctx) -> dict:
    t0 = time.perf_counter()
    sources = _sources(ctx)
    if not sources:
        return {"found": 0, "parsed": 0}

    conn = dbmod.get_ro()
    existing: dict[str, tuple[str, int]] = {}
    for r in dbmod.rows(conn, "SELECT path_key, fingerprint, parser_version FROM workflows"):
        existing[r["path_key"]] = (r["fingerprint"], int(r["parser_version"] or 0))

    work: list[WorkflowWork] = []
    seen: set[str] = set()
    found = 0
    not_workflow_files = 0
    for directory, root in sources:
        root_id = ctx.root_id(root)
        base = Path(root.path)
        for entry in walk_json(directory):
            key = path_key(entry.path)
            if key in seen:
                continue
            seen.add(key)
            if not _looks_like_workflow(entry.path):
                not_workflow_files += 1
                continue
            found += 1
            fp = file_fingerprint(entry.path, entry.size, entry.mtime_ns)
            prev = existing.get(key)
            if not ctx.force and prev and prev[0] == fp \
                    and prev[1] == PARSER_VERSION_WORKFLOW:
                ctx.bump(skipped=1)
                continue
            rel = safe_relpath(entry.path, base)
            origin, origin_package = workflow_origin.classify(rel, entry.path)
            work.append(WorkflowWork(
                abs_path=entry.path, name=entry.stem, rel_path=rel,
                folder=os.path.dirname(rel).replace("\\", "/"), root_id=root_id,
                size=entry.size, mtime_ns=entry.mtime_ns, fingerprint=fp,
                origin=origin, origin_package=origin_package,
            ))

    ctx.items_total += len(work)
    if not work:
        return {"found": found, "parsed": 0, "skipped": found,
                "ignored_json": not_workflow_files,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    analyzed = map_parallel(ctx, ctx.ex_io, _analyze, work, phase="workflows",
                            kind="workflow")
    ready = []
    not_workflows = 0
    for w in analyzed:
        if w is None:
            continue
        if w.result is None or not w.result.ok:
            not_workflows += 1
            code = (w.result.error_code if w.result else None) or "JSON_INVALID"
            ctx.record_error("workflows", "workflow", w.abs_path, code,
                             (w.result.error_message if w.result else "Unreadable JSON.")
                             or "Not a ComfyUI workflow.")
            continue
        ready.append(w)

    def _progress(done: int, total: int) -> None:
        ctx.bus.publish("progress", {
            "job_id": ctx.job_id, "phase": "workflows", "done": done, "total": total,
        }, coalesce_key="workflows")

    commit_batches(ctx, "workflows", "workflow", ready, _upsert, on_progress=_progress)
    ctx.bump(len(ready))
    ctx.bus.flush("workflows")
    return {
        "found": found, "parsed": len(ready), "not_workflows": not_workflows,
        "ignored_json": not_workflow_files, "skipped": found - len(work),
        "sources": len(sources),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
