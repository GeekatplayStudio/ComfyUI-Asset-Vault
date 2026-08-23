"""Phase 3 - model files: header parse, architecture detection, upsert.

Hashing NEVER runs here (DECISIONS C1).  A cheap first+last 1 MiB probe hash is
computed so a later rename/move can reuse a full hash without re-reading 12 GB.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ...config import PARSER_VERSION_MODEL
from ...core import config_service, errors, progress
from ...core import db as dbmod
from ...core.fingerprint import file_fingerprint
from ...core.pathsafe import long_path, path_key, safe_relpath
from ...parsers import arch_detect, gguf_header, safetensors_header, torch_zip
from ..service import commit_batches, map_cpu
from ..walker import MODEL_EXTS, SKIP_EXTS, walk_models

PROBE_BYTES = 1024 * 1024
SIDECAR_SUFFIXES = (".preview.png", ".preview.jpg", ".png", ".jpg", ".jpeg", ".webp")
SIDECAR_JSON = (".civitai.info", ".json", ".txt")


@dataclass
class ModelWork:
    abs_path: str
    filename: str
    stem: str
    ext: str
    size: int
    mtime_ns: int
    ctime_ns: int
    fingerprint: str
    category: str
    rel_path: str
    folder: str
    root_id: int
    existing_id: int | None = None
    model_id: int | None = None
    parsed: dict | None = None


def _format_for(ext: str, path: str) -> str:
    e = ext.lower()
    if e in (".safetensors", ".sft"):
        return "safetensors"
    if e == ".gguf":
        return "gguf"
    if e == ".onnx":
        return "onnx"
    if e in (".pt", ".pth", ".ckpt", ".bin", ".pkl"):
        return "torch_zip" if torch_zip.is_zip(path) else "torch_legacy"
    return "other"


def _probe_hash(path: str, size: int) -> str | None:
    """First + last 1 MiB - cheap, and enough to recognise a moved file."""
    try:
        with open(long_path(path), "rb") as fh:
            h = hashlib.sha256()
            h.update(fh.read(min(PROBE_BYTES, size)))
            if size > PROBE_BYTES * 2:
                fh.seek(-PROBE_BYTES, os.SEEK_END)
                h.update(fh.read(PROBE_BYTES))
            h.update(str(size).encode())
            return h.hexdigest()
    except (OSError, ValueError):
        return None


def _sidecars(path: str, stem: str) -> tuple[str | None, str | None]:
    directory = os.path.dirname(path)
    preview = None
    sidecar = None
    for suffix in SIDECAR_SUFFIXES:
        cand = os.path.join(directory, stem + suffix)
        try:
            if os.path.isfile(cand):
                preview = cand
                break
        except OSError:
            continue
    for suffix in SIDECAR_JSON:
        cand = os.path.join(directory, stem + suffix)
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) < 512_000:
                sidecar = cand
                break
        except OSError:
            continue
    return preview, sidecar


def _parse_one(work: ModelWork) -> ModelWork:
    fmt = _format_for(work.ext, work.abs_path)
    parsed: dict = {
        "format": fmt, "header_parsed": 0, "integrity": "ok", "integrity_note": None,
        "keys": [], "shapes": {}, "dtypes": {}, "metadata": {}, "tensor_count": None,
        "error_code": None,
    }
    if fmt == "safetensors":
        h = safetensors_header.read_header(work.abs_path, file_size=work.size)
        parsed.update({
            "header_parsed": 1 if h.ok else 0, "integrity": h.integrity,
            "integrity_note": h.integrity_note, "keys": h.keys, "shapes": h.shapes,
            "dtypes": h.dtypes, "metadata": h.metadata, "tensor_count": h.tensor_count,
            "error_code": h.error_code,
            "header_metadata_json": safetensors_header.metadata_json(h.metadata),
        })
    elif fmt == "gguf":
        g = gguf_header.read_header(work.abs_path, file_size=work.size)
        md = {k: v for k, v in g.metadata.items() if isinstance(v, (str, int, float, bool))}
        parsed.update({
            "header_parsed": 1 if g.ok else 0, "integrity": g.integrity,
            "integrity_note": g.integrity_note, "keys": g.keys, "shapes": g.shapes,
            "dtypes": g.dtypes, "metadata": md, "tensor_count": g.tensor_count,
            "error_code": g.error_code,
            "header_metadata_json": safetensors_header.metadata_json(md),
        })
    elif fmt in ("torch_zip", "torch_legacy"):
        t = torch_zip.read_keys(work.abs_path, file_size=work.size)
        parsed.update({
            "header_parsed": 1 if t.ok else 0, "integrity": t.integrity,
            "integrity_note": t.integrity_note, "keys": t.keys,
            "tensor_count": t.tensor_count, "error_code": t.error_code,
            "format": t.fmt,
        })
    elif fmt == "onnx":
        info = torch_zip.read_onnx_producer(work.abs_path)
        parsed["metadata"] = info
        parsed["integrity"] = "ok" if info else "unsupported_format"
        parsed["header_metadata_json"] = safetensors_header.metadata_json(info)
    else:
        parsed["integrity"] = "unsupported_format"

    arch = arch_detect.detect(
        keys=parsed["keys"], shapes=parsed["shapes"], dtypes=parsed["dtypes"],
        metadata=parsed["metadata"], category=work.category, stem=work.stem,
        file_size=work.size, fmt=parsed["format"],
    )
    parsed["arch"] = arch
    parsed["probe_sha256"] = _probe_hash(work.abs_path, work.size)
    parsed["preview_path"], parsed["sidecar_json"] = _sidecars(work.abs_path, work.stem)
    work.parsed = parsed
    return work


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert(conn: sqlite3.Connection, work: ModelWork) -> int | None:
    b = dbmod.bind
    now = dbmod.now_ms()
    p = work.parsed or {}
    arch: arch_detect.ArchResult = p.get("arch") or arch_detect.ArchResult()

    row = conn.execute(
        "SELECT id, model_id, first_seen_at, sha256, autov2, hash_state, hashed_at, "
        "fingerprint FROM model_files WHERE path_key = ?", (path_key(work.abs_path),)
    ).fetchone()

    integrity = p.get("integrity") or "ok"
    if integrity not in ("ok", "invalid_header", "not_a_model", "truncated",
                         "unreadable", "unsupported_format"):
        integrity = "ok"

    model_values = (
        b(work.stem), b(path_key(work.abs_path)), b(work.category),
        b(arch.model_role), b(arch.base_model_family), b(arch.base_model_variant),
        b(arch.modality), b(arch.architecture_label), b(arch.arch_source),
        b(arch.arch_confidence, kind="real"),
        b(arch.is_adapter), b(arch.adapter_format), b(arch.adapter_rank, kind="int"),
        b(arch.adapter_alpha, kind="real"),
        b(arch.is_bundled), b(arch.components_json(), kind="json"),
        b(arch.param_count_primary, kind="int"), b(arch.param_count_total, kind="int"),
        b(p.get("tensor_count"), kind="int"), b(arch.precision), b(arch.quantization),
        b(arch.resolution_hint), b(arch.prediction_type),
        b(p.get("header_metadata_json"), kind="json"),
        b(arch.signals, kind="json"),
        b(integrity), b(p.get("integrity_note")),
        b(work.size, kind="int"), b(now, kind="int"), b(now, kind="int"),
    )

    if row is not None and row["model_id"]:
        model_id = int(row["model_id"])
        conn.execute(
            "UPDATE models SET name=?, canonical_key=?, category=?, model_role=?, "
            "base_model_family=?, base_model_variant=?, modality=?, architecture_label=?, "
            "arch_source=?, arch_confidence=?, is_adapter=?, adapter_format=?, "
            "adapter_rank=?, adapter_alpha=?, is_bundled=?, components_json=?, "
            "param_count_primary=?, param_count_total=?, tensor_count=?, precision=?, "
            "quantization=?, resolution_hint=?, prediction_type=?, header_metadata_json=?, "
            "detection_signals_json=?, integrity=?, integrity_note=?, total_size=?, "
            "updated_at=?, missing_since=NULL WHERE id=?",
            (*model_values[:-2], model_values[-1], model_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO models(name,canonical_key,category,model_role,base_model_family,"
            "base_model_variant,modality,architecture_label,arch_source,arch_confidence,"
            "is_adapter,adapter_format,adapter_rank,adapter_alpha,is_bundled,"
            "components_json,param_count_primary,param_count_total,tensor_count,precision,"
            "quantization,resolution_hint,prediction_type,header_metadata_json,"
            "detection_signals_json,integrity,integrity_note,total_size,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            model_values,
        )
        model_id = int(cur.lastrowid)

    file_values = (
        b(model_id, kind="int"), b(work.root_id, kind="int"), b(work.abs_path),
        b(path_key(work.abs_path)), b(work.rel_path), b(work.folder), b(work.filename),
        b(work.stem), b(work.ext), b(work.size, kind="int"),
        b(work.mtime_ns, kind="int"), b(work.ctime_ns, kind="int"), b(work.fingerprint),
        b(p.get("format") or "other"), b(p.get("header_parsed") or 0),
        b(PARSER_VERSION_MODEL, kind="int"), b(p.get("probe_sha256")),
        b(p.get("preview_path")), b(p.get("sidecar_json")),
        b(now, kind="int"), b(now, kind="int"),
    )
    if row is None:
        cur = conn.execute(
            "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,folder,"
            "filename,stem,ext,size,mtime_ns,ctime_ns,fingerprint,format,header_parsed,"
            "parser_version,probe_sha256,preview_path,sidecar_json,first_seen_at,"
            "last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            file_values,
        )
        file_id = int(cur.lastrowid)
    else:
        file_id = int(row["id"])
        # A changed fingerprint invalidates any cached hash (ARCHITECTURE 4.2).
        hash_reset = ""
        if (row["hash_state"] == "done" and row["sha256"]
                and work.fingerprint != row["fingerprint"]):
            hash_reset = ", hash_state='stale'"
        conn.execute(
            "UPDATE model_files SET model_id=?, root_id=?, abs_path=?, rel_path=?, "  # noqa: S608
            "folder=?, filename=?, stem=?, ext=?, size=?, mtime_ns=?, ctime_ns=?, "
            "fingerprint=?, format=?, header_parsed=?, parser_version=?, probe_sha256=?, "
            "preview_path=?, sidecar_json=?, last_seen_at=?, missing_since=NULL "
            f"{hash_reset} WHERE id=?",
            (file_values[0], file_values[1], file_values[2], file_values[4],
             file_values[5], file_values[6], file_values[7], file_values[8],
             file_values[9], file_values[10], file_values[11], file_values[12],
             file_values[13], file_values[14], file_values[15], file_values[16],
             file_values[17], file_values[18], file_values[20], file_id),
        )

    conn.execute("UPDATE models SET primary_file_id=?, file_count=("
                 "SELECT COUNT(*) FROM model_files WHERE model_id=?), total_size=("
                 "SELECT COALESCE(SUM(size),0) FROM model_files WHERE model_id=?) "
                 "WHERE id=?", (file_id, model_id, model_id, model_id))
    work.model_id = model_id
    return model_id


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------

def run(ctx) -> dict:
    t0 = time.perf_counter()
    cfg = ctx.cfg
    dirs = config_service.model_dirs(cfg)
    if not dirs:
        return {"found": 0, "parsed": 0, "skipped": 0}

    conn = dbmod.get_ro()
    existing: dict[str, tuple[str, int]] = {}
    for r in dbmod.rows(conn, "SELECT path_key, fingerprint, parser_version FROM model_files"):
        existing[r["path_key"]] = (r["fingerprint"], int(r["parser_version"] or 0))

    work: list[ModelWork] = []
    seen_keys: set[str] = set()
    partials = 0
    found = 0
    for category, directory, root in dirs:
        root_id = ctx.root_id(root)
        # rel_path is relative to the models store, folder to the category dir,
        # so the left rail groups as 'loras / ltx2' rather than 'models/loras'.
        store_path = Path(directory).parent if root.kind == "comfyui" else Path(root.path)
        for entry in walk_models(directory):
            found += 1
            key = path_key(entry.path)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            fp = file_fingerprint(entry.path, entry.size, entry.mtime_ns)
            prev = existing.get(key)
            if not ctx.force and prev and prev[0] == fp and prev[1] == PARSER_VERSION_MODEL:
                ctx.bump(skipped=1)
                continue
            rel = safe_relpath(entry.path, store_path)
            folder = os.path.dirname(
                safe_relpath(entry.path, Path(directory))).replace("\\", "/")
            work.append(ModelWork(
                abs_path=entry.path, filename=entry.name, stem=entry.stem,
                ext=entry.ext, size=entry.size, mtime_ns=entry.mtime_ns,
                ctime_ns=entry.ctime_ns, fingerprint=fp, category=category,
                rel_path=rel, folder=folder, root_id=root_id,
            ))
        for entry in _partial_downloads(directory):
            partials += 1
            ctx.record_error("models", "model", entry.path, errors.NOT_A_MODEL,
                             "Partial download - not indexed.")
        for entry in _undersized(directory):
            ctx.record_error("models", "model", entry.path, errors.NOT_A_MODEL,
                             f"File has a model extension but is only {entry.size} "
                             "bytes - not indexed.")

    ctx.items_total += len(work)
    if not work:
        return {"found": found, "parsed": 0, "skipped": found, "partials": partials,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    # Header reading is `json.loads` over a safetensors header - up to 390 KB and
    # a measured 94 ms of unbroken GIL for one file - so it runs in worker
    # processes (QA-PERF-1).  `ex_io` stays as the fallback.
    parsed = map_cpu(ctx, _parse_one, work, phase="models", kind="model",
                     fallback=ctx.ex_io)
    ready = [w for w in parsed if w is not None]

    def _progress(done: int, total: int) -> None:
        ctx.bump(0)
        ctx.bus.publish("progress", {
            "job_id": ctx.job_id, "phase": "models", "done": done, "total": total,
            "rate": progress.rate_per_s(done, time.perf_counter() - t0),
            "eta_ms": progress.eta_ms(done, total, time.perf_counter() - t0),
        }, coalesce_key="models")

    ids = commit_batches(ctx, "models", "model", ready, _upsert, on_progress=_progress)
    ctx.bump(len(ready))
    ctx.bus.flush("models")
    written = sum(1 for i in ids if i)
    return {
        "found": found, "parsed": len(ready), "written": written,
        "skipped": found - len(work), "partials": partials,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def _partial_downloads(directory: Path):
    from ..walker import walk

    for e in walk(directory):
        if e.ext in SKIP_EXTS and e.size > 0:
            yield e


def _undersized(directory: Path):
    """A .safetensors of 0 bytes is a failed download, not an absent file."""
    from ..walker import MIN_MODEL_BYTES, walk

    for e in walk(directory):
        if e.ext in MODEL_EXTS and e.size < MIN_MODEL_BYTES:
            yield e


def known_model_exts() -> set[str]:
    return set(MODEL_EXTS)
