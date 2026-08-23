"""Phase 6 - generated outputs: dimensions and embedded generation metadata.

Every prompt-graph read goes through ``graph_utils`` (B1); nothing here binds a
non-scalar to SQLite.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...config import PARSER_VERSION_OUTPUT
from ...core import config_service, progress
from ...core import db as dbmod
from ...core.fingerprint import file_fingerprint
from ...core.pathsafe import path_key, safe_relpath
from ...parsers import image_meta
from ..service import commit_batches, map_parallel
from ..walker import walk


@dataclass
class OutputWork:
    abs_path: str
    filename: str
    ext: str
    rel_path: str
    folder: str
    root_id: int
    size: int
    mtime_ns: int
    created_at_file: int
    fingerprint: str
    meta: image_meta.OutputMeta | None = None
    models: list[dict] = field(default_factory=list)


def _analyze(work: OutputWork) -> OutputWork:
    work.meta = image_meta.read_output(work.abs_path, work.ext)
    s = work.meta.summary
    if s is not None:
        refs = [{"ref_name": e["ref_name"], "role": e.get("category"), "strength": None}
                for e in s.models]
        refs += [{"ref_name": e["ref_name"], "role": "lora",
                  "strength": e.get("strength")} for e in s.loras]
        work.models = refs
    return work


def _upsert(conn: sqlite3.Connection, work: OutputWork) -> int | None:
    b = dbmod.bind
    now = dbmod.now_ms()
    m = work.meta or image_meta.OutputMeta()
    s = m.summary
    values = (
        b(work.root_id, kind="int"), b(work.abs_path), b(path_key(work.abs_path)),
        b(work.rel_path), b(work.folder), b(work.filename), b(work.ext),
        b(m.media_kind), b(m.mime), b(m.width, kind="int"), b(m.height, kind="int"),
        b(m.duration_ms, kind="int"), b(m.frame_count, kind="int"),
        b(m.has_alpha, kind="int"), b(m.color_mode),
        b(work.size, kind="int"), b(work.mtime_ns, kind="int"),
        b(work.created_at_file, kind="int"), b(work.fingerprint),
        b(PARSER_VERSION_OUTPUT, kind="int"),
        b(m.has_metadata), b(m.metadata_format),
        b(s.positive_prompt if s else None), b(s.negative_prompt if s else None),
        b(s.seed if s else None), b(s.steps if s else None, kind="int"),
        b(s.cfg if s else None, kind="real"), b(s.denoise if s else None, kind="real"),
        b(s.sampler if s else None), b(s.scheduler if s else None),
        b(s.primary_model if s else None),
        b(s.graph_hash if s else None),
        b(s.node_count if s else None, kind="int"),
        b(s.unresolved_count if s else 0, kind="int"),
        b(s.provenance if s else None, kind="json"),
        b(m.prompt_graph_json, kind="json"),
        b(now, kind="int"), b(now, kind="int"),
    )
    conn.execute(
        "INSERT INTO outputs(root_id,abs_path,path_key,rel_path,folder,filename,ext,"
        "media_kind,mime,width,height,duration_ms,frame_count,has_alpha,color_mode,size,"
        "mtime_ns,created_at_file,fingerprint,parser_version,has_metadata,metadata_format,"
        "positive_prompt,negative_prompt,seed,steps,cfg,denoise,sampler,scheduler,"
        "model_name,workflow_hash,node_count,unresolved_inputs,provenance_json,"
        "prompt_graph_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path_key) DO UPDATE SET root_id=excluded.root_id, "
        "abs_path=excluded.abs_path, rel_path=excluded.rel_path, folder=excluded.folder, "
        "filename=excluded.filename, ext=excluded.ext, media_kind=excluded.media_kind, "
        "mime=excluded.mime, width=excluded.width, height=excluded.height, "
        "duration_ms=excluded.duration_ms, frame_count=excluded.frame_count, "
        "has_alpha=excluded.has_alpha, color_mode=excluded.color_mode, size=excluded.size, "
        "mtime_ns=excluded.mtime_ns, created_at_file=excluded.created_at_file, "
        "fingerprint=excluded.fingerprint, parser_version=excluded.parser_version, "
        "has_metadata=excluded.has_metadata, metadata_format=excluded.metadata_format, "
        "positive_prompt=excluded.positive_prompt, negative_prompt=excluded.negative_prompt, "
        "seed=excluded.seed, steps=excluded.steps, cfg=excluded.cfg, "
        "denoise=excluded.denoise, sampler=excluded.sampler, scheduler=excluded.scheduler, "
        "model_name=excluded.model_name, workflow_hash=excluded.workflow_hash, "
        "node_count=excluded.node_count, unresolved_inputs=excluded.unresolved_inputs, "
        "provenance_json=excluded.provenance_json, "
        "prompt_graph_json=excluded.prompt_graph_json, updated_at=excluded.updated_at, "
        "missing_since=NULL",
        values,
    )
    row = conn.execute("SELECT id FROM outputs WHERE path_key = ?",
                       (path_key(work.abs_path),)).fetchone()
    if row is None:
        return None
    out_id = int(row["id"])
    if work.models:
        conn.execute("DELETE FROM output_models WHERE output_id = ?", (out_id,))
        written: set[str] = set()
        for ref in work.models:
            name = str(ref.get("ref_name") or "")
            if not name or name in written:
                continue
            written.add(name)
            conn.execute(
                "INSERT OR REPLACE INTO output_models(output_id,ref_name,role,strength) "
                "VALUES (?,?,?,?)",
                (out_id, b(name), b(ref.get("role")), b(ref.get("strength"), kind="real")),
            )
    return out_id


def run(ctx) -> dict:
    t0 = time.perf_counter()
    dirs = config_service.output_dirs(ctx.cfg)
    if not dirs:
        return {"found": 0, "parsed": 0}

    conn = dbmod.get_ro()
    existing: dict[str, tuple[str, int]] = {}
    for r in dbmod.rows(conn, "SELECT path_key, fingerprint, parser_version FROM outputs"):
        existing[r["path_key"]] = (r["fingerprint"], int(r["parser_version"] or 0))

    work: list[OutputWork] = []
    seen: set[str] = set()
    found = 0
    for directory, root in dirs:
        root_id = ctx.root_id(root)
        base = Path(directory)
        for entry in walk(directory):
            found += 1
            key = path_key(entry.path)
            if key in seen:
                continue
            seen.add(key)
            fp = file_fingerprint(entry.path, entry.size, entry.mtime_ns)
            prev = existing.get(key)
            if not ctx.force and prev and prev[0] == fp \
                    and prev[1] == PARSER_VERSION_OUTPUT:
                ctx.bump(skipped=1)
                continue
            rel = safe_relpath(entry.path, base)
            work.append(OutputWork(
                abs_path=entry.path, filename=entry.name, ext=entry.ext,
                rel_path=rel, folder=os.path.dirname(rel).replace("\\", "/"),
                root_id=root_id, size=entry.size, mtime_ns=entry.mtime_ns,
                created_at_file=int(entry.ctime_ns / 1_000_000),
                fingerprint=fp,
            ))

    ctx.items_total += len(work)
    if not work:
        return {"found": found, "parsed": 0, "skipped": found,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    analyzed = map_parallel(ctx, ctx.ex_img, _analyze, work, phase="outputs",
                            kind="output")
    ready = []
    for w in analyzed:
        if w is None:
            continue
        if w.meta is not None and w.meta.error_code:
            ctx.record_error("outputs", "output", w.abs_path, w.meta.error_code,
                             w.meta.error_message or "Could not read media header.")
        ready.append(w)

    def _progress(done: int, total: int) -> None:
        elapsed = time.perf_counter() - t0
        ctx.bus.publish("progress", {
            "job_id": ctx.job_id, "phase": "outputs", "done": done, "total": total,
            "rate": progress.rate_per_s(done, elapsed),
            "eta_ms": progress.eta_ms(done, total, elapsed),
        }, coalesce_key="outputs")

    commit_batches(ctx, "outputs", "output", ready, _upsert, on_progress=_progress)
    ctx.bump(len(ready))
    ctx.bus.flush("outputs")
    with_meta = sum(1 for w in ready if w.meta and w.meta.has_metadata)
    return {
        "found": found, "parsed": len(ready), "with_metadata": with_meta,
        "skipped": found - len(work),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
