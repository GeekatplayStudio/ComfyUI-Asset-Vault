"""Phase 4 - node packages and node classes (B4).

Static analysis only.  Nothing under ``custom_nodes/`` is ever imported,
executed, or unpickled - the package is read as text and parsed with ``ast``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...config import PARSER_VERSION_NODE
from ...core import config_service
from ...core import db as dbmod
from ...core.fingerprint import folder_fingerprint
from ...core.pathsafe import path_key, safe_relpath
from ...parsers import node_ast, node_registry
from ..service import commit_batches, map_cpu
from ..walker import is_reparse_point

OFFICIAL_FOLDER = "__comfyui_core__"
FINGERPRINT_EXTS = (".py", ".toml", ".txt", ".md", ".json")
MAX_FP_FILES = 6000


@dataclass
class PackageWork:
    folder_name: str
    abs_path: str
    is_official: bool = False
    is_single_file: bool = False
    enabled: bool = True
    disabled_reason: str | None = None
    fingerprint: str = ""
    file_count: int = 0
    total_size: int = 0
    folder_mtime_ns: int = 0
    files: list[Path] = field(default_factory=list)
    meta: node_registry.PackageMeta | None = None
    result: node_ast.ExtractResult | None = None
    registry_ids: list[str] = field(default_factory=list)
    registry_meta: dict = field(default_factory=dict)
    extraction_status: str = "ok"
    repo_suspect: bool = False


def folder_size(root: Path) -> tuple[int, int]:
    """True on-disk (bytes, file count) for a package - nothing pruned.

    The fingerprint walk deliberately skips .git/web/node_modules, so using its
    total under-reports a git checkout by roughly half.  The Storage view needs
    the real number.
    """
    total = 0
    count = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if is_reparse_point(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                total += int(e.stat(follow_symlinks=False).st_size)
                count += 1
            except OSError:
                continue
    return total, count


def _fingerprint_folder(root: Path) -> tuple[str, int, int, int]:
    entries: list[tuple[str, int, int]] = []
    count = 0
    newest = 0
    stack = [root]
    while stack and count < MAX_FP_FILES:
        cur = stack.pop()
        try:
            it = list(os.scandir(cur))
        except OSError:
            continue
        for e in it:
            try:
                if is_reparse_point(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name in node_ast.PRUNE_DIRS or e.name.startswith("."):
                        continue
                    stack.append(Path(e.path))
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            count += 1
            mtime = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            newest = max(newest, mtime)
            if e.name.endswith(FINGERPRINT_EXTS):
                entries.append((safe_relpath(e.path, root), int(st.st_size), mtime))
    total, real_count = folder_size(root)
    return folder_fingerprint(entries), real_count, total, newest


def _discover(ctx) -> list[PackageWork]:
    cfg = ctx.cfg
    out: list[PackageWork] = []
    for cn_dir, _root in config_service.custom_nodes_dirs(cfg):
        try:
            entries = sorted(os.scandir(cn_dir), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            name = e.name
            if name in ("__pycache__", ".gitkeep") or name.endswith(".example"):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse_point(e):
                continue
            if is_dir:
                p = Path(e.path)
                enabled = not name.endswith(".disabled")
                reason = ".disabled suffix" if not enabled else None
                if enabled:
                    try:
                        if (p / ".disabled").exists():
                            enabled = False
                            reason = "marker file"
                    except OSError:
                        pass
                fp, count, total, newest = _fingerprint_folder(p)
                out.append(PackageWork(
                    folder_name=name, abs_path=str(p), enabled=enabled,
                    disabled_reason=reason, fingerprint=fp, file_count=count,
                    total_size=total, folder_mtime_ns=newest,
                ))
            elif name.endswith(".py"):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mtime = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                out.append(PackageWork(
                    folder_name=name, abs_path=e.path, is_single_file=True,
                    fingerprint=folder_fingerprint([(name, int(st.st_size), mtime)]),
                    file_count=1, total_size=int(st.st_size), folder_mtime_ns=mtime,
                    files=[Path(e.path)],
                ))

    comfy = cfg.comfyui_path
    if comfy is not None:
        core_files = []
        nodes_py = comfy / "nodes.py"
        if nodes_py.is_file():
            core_files.append(nodes_py)
        entries: list[tuple[str, int, int]] = []
        total = 0
        newest = 0
        for sub in ("comfy_extras", "comfy_api_nodes"):
            d = comfy / sub
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*.py")):
                if any(part in node_ast.PRUNE_DIRS for part in p.parts):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                core_files.append(p)
                mtime = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                entries.append((safe_relpath(p, comfy), int(st.st_size), mtime))
                total += int(st.st_size)
                newest = max(newest, mtime)
        if core_files:
            try:
                st = nodes_py.stat()
                entries.append(("nodes.py", int(st.st_size),
                                int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))))
            except OSError:
                pass
            out.append(PackageWork(
                folder_name=OFFICIAL_FOLDER, abs_path=str(comfy), is_official=True,
                fingerprint=folder_fingerprint(entries), file_count=len(core_files),
                total_size=total, folder_mtime_ns=newest, files=core_files,
            ))
    return out


def _analyze(work: PackageWork) -> PackageWork:
    p = Path(work.abs_path)
    if work.is_official:
        work.result = node_ast.extract_official(p)
        work.meta = node_registry.PackageMeta(
            display_name="ComfyUI Core", author="Comfy Org",
            description="Built-in ComfyUI node classes.",
            homepage_url="https://github.com/comfyanonymous/ComfyUI",
        )
        node_registry.read_git(p, work.meta)
        version = None
        try:
            vp = p / "comfyui_version.py"
            if vp.is_file():
                text = vp.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if "__version__" in line and "=" in line:
                        version = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except OSError:
            version = None
        work.meta.version = version
    elif work.is_single_file:
        work.result = node_ast.extract_package(p.parent, pkg_root=p.parent, files=[p])
        work.meta = node_registry.PackageMeta(display_name=p.stem)
    else:
        work.result = node_ast.extract_package(p)
        work.meta = node_registry.collect_package_meta(p, work.folder_name)
    return work


def _apply_registry(ctx, work: PackageWork) -> None:
    reg = node_registry.get_registry(ctx.cfg.comfyui_path)
    meta = work.meta or node_registry.PackageMeta()
    hit, _how = reg.lookup(meta.repo_url, work.folder_name)
    if hit:
        ids, rmeta = hit
        work.registry_ids = ids
        work.registry_meta = rmeta or {}
        if not meta.description:
            meta.description = (rmeta or {}).get("description")
        if not meta.author:
            meta.author = (rmeta or {}).get("author")
        result = work.result
        if result is not None:
            for node_id in ids:
                if node_id not in result.classes:
                    result.add(node_ast.NodeClass(node_id=node_id,
                                                  display_name=node_id), "S6")
    if meta.repo_url and not work.is_official:
        work.repo_suspect = node_registry.repo_url_is_suspect(
            meta.repo_url, work.folder_name, reg)
    # Fall back to node_list.json when nothing else produced classes.
    result = work.result
    if result is not None and not result.classes and not work.is_single_file:
        for node_id in node_registry.read_node_list(Path(work.abs_path)):
            result.add(node_ast.NodeClass(node_id=node_id, display_name=node_id), "S6")
    if result is None or not result.classes:
        work.extraction_status = "empty_package" if work.file_count == 0 else "no_classes_found"
    elif result.strategies == {"S6"}:
        work.extraction_status = "registry_only"
    elif result.errors:
        work.extraction_status = "partial"
    else:
        work.extraction_status = "ok"


def _upsert(conn: sqlite3.Connection, work: PackageWork) -> int | None:
    b = dbmod.bind
    now = dbmod.now_ms()
    meta = work.meta or node_registry.PackageMeta()
    result = work.result or node_ast.ExtractResult()
    repo_norm = node_registry.normalize_repo_url(meta.repo_url)

    values = (
        b(work.folder_name), b(path_key(work.abs_path)), b(work.abs_path),
        b(meta.display_name or work.folder_name), b(meta.author), b(meta.publisher_id),
        b(meta.registry_id), b(meta.description), b(meta.long_description),
        b(meta.icon_url), b(meta.homepage_url), b(meta.license),
        b(work.is_official), b(work.enabled), b(work.disabled_reason),
        b(work.is_single_file),
        b(meta.repo_url), b(repo_norm), b(work.repo_suspect),
        b(meta.git_branch), b(meta.git_commit), b(meta.git_commit_at, kind="int"),
        b(meta.last_fetch_at, kind="int"),
        b(meta.version),
        b(meta.python_deps, kind="json"), b(meta.has_web_directory),
        b(len(result.classes), kind="int"), b(work.extraction_status),
        b(sorted(result.strategies), kind="json"),
        b(result.source_breakdown or None, kind="json"),
        b(work.file_count, kind="int"), b(work.total_size, kind="int"),
        b(work.folder_mtime_ns, kind="int"), b(work.fingerprint),
        b(PARSER_VERSION_NODE, kind="int"), b(now, kind="int"), b(now, kind="int"),
    )

    conn.execute(
        "INSERT INTO node_packages(folder_name,path_key,abs_path,display_name,author,"
        "publisher_id,registry_id,description,long_description,icon_url,homepage_url,"
        "license,is_official,enabled,disabled_reason,is_single_file,repo_url,"
        "repo_url_normalized,repo_url_suspect,git_branch,git_commit,git_commit_at,"
        "last_fetch_at,installed_version,python_deps_json,has_web_directory,class_count,"
        "extraction_status,extraction_strategies_json,source_breakdown_json,file_count,"
        "total_size,folder_mtime_ns,fingerprint,parser_version,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path_key) DO UPDATE SET folder_name=excluded.folder_name, "
        "abs_path=excluded.abs_path, display_name=excluded.display_name, "
        "author=excluded.author, publisher_id=excluded.publisher_id, "
        "registry_id=excluded.registry_id, description=excluded.description, "
        "long_description=excluded.long_description, icon_url=excluded.icon_url, "
        "homepage_url=excluded.homepage_url, license=excluded.license, "
        "is_official=excluded.is_official, enabled=excluded.enabled, "
        "disabled_reason=excluded.disabled_reason, is_single_file=excluded.is_single_file, "
        "repo_url=excluded.repo_url, repo_url_normalized=excluded.repo_url_normalized, "
        "repo_url_suspect=excluded.repo_url_suspect, git_branch=excluded.git_branch, "
        "git_commit=excluded.git_commit, git_commit_at=excluded.git_commit_at, "
        "last_fetch_at=excluded.last_fetch_at, installed_version=excluded.installed_version, "
        "python_deps_json=excluded.python_deps_json, "
        "has_web_directory=excluded.has_web_directory, class_count=excluded.class_count, "
        "extraction_status=excluded.extraction_status, "
        "extraction_strategies_json=excluded.extraction_strategies_json, "
        "source_breakdown_json=excluded.source_breakdown_json, "
        "file_count=excluded.file_count, total_size=excluded.total_size, "
        "folder_mtime_ns=excluded.folder_mtime_ns, fingerprint=excluded.fingerprint, "
        "parser_version=excluded.parser_version, updated_at=excluded.updated_at, "
        "missing_since=NULL",
        values,
    )
    row = conn.execute("SELECT id FROM node_packages WHERE path_key = ?",
                       (path_key(work.abs_path),)).fetchone()
    if row is None:
        return None
    pkg_id = int(row["id"])

    keep = set()
    for nc in result.classes.values():
        keep.add(nc.node_id)
        conn.execute(
            "INSERT INTO node_classes(package_id,node_id,class_name,display_name,category,"
            "description,input_types_json,return_types_json,return_names_json,output_node,"
            "function_name,is_deprecated,is_experimental,is_api_node,source_file,"
            "source_lineno,source_strategy,sources_json,confidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(package_id,node_id) DO UPDATE SET class_name=excluded.class_name, "
            "display_name=excluded.display_name, category=excluded.category, "
            "description=excluded.description, input_types_json=excluded.input_types_json, "
            "return_types_json=excluded.return_types_json, "
            "return_names_json=excluded.return_names_json, output_node=excluded.output_node, "
            "function_name=excluded.function_name, is_deprecated=excluded.is_deprecated, "
            "is_experimental=excluded.is_experimental, is_api_node=excluded.is_api_node, "
            "source_file=excluded.source_file, source_lineno=excluded.source_lineno, "
            "source_strategy=excluded.source_strategy, sources_json=excluded.sources_json, "
            "confidence=excluded.confidence, updated_at=excluded.updated_at",
            (
                pkg_id, b(nc.node_id), b(nc.class_name), b(nc.display_name or nc.node_id),
                b(nc.category), b(nc.description), b(nc.input_types, kind="json"),
                b(nc.return_types, kind="json"), b(nc.return_names, kind="json"),
                b(nc.output_node), b(nc.function_name), b(nc.is_deprecated),
                b(nc.is_experimental), b(nc.is_api_node), b(nc.source_file),
                b(nc.source_lineno, kind="int"), b(nc.primary_strategy),
                b(sorted(nc.strategies), kind="json"), b(nc.confidence),
                b(now, kind="int"), b(now, kind="int"),
            ),
        )
    if keep:
        placeholders = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM node_classes WHERE package_id = ? AND node_id NOT IN ({placeholders})",  # noqa: S608
            (pkg_id, *sorted(keep)),
        )
    else:
        conn.execute("DELETE FROM node_classes WHERE package_id = ?", (pkg_id,))
    return pkg_id


def run(ctx) -> dict:
    t0 = time.perf_counter()
    packages = _discover(ctx)
    if not packages:
        return {"found": 0, "analyzed": 0}

    conn = dbmod.get_ro()
    existing: dict[str, tuple[str, int]] = {}
    for r in dbmod.rows(conn, "SELECT path_key, fingerprint, parser_version FROM node_packages"):
        existing[r["path_key"]] = (r["fingerprint"], int(r["parser_version"] or 0))

    todo: list[PackageWork] = []
    for work in packages:
        prev = existing.get(path_key(work.abs_path))
        if not ctx.force and prev and prev[0] == work.fingerprint \
                and prev[1] == PARSER_VERSION_NODE:
            ctx.bump(skipped=1)
            continue
        todo.append(work)

    ctx.items_total += len(todo)
    if not todo:
        return {"found": len(packages), "analyzed": 0, "skipped": len(packages),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    node_registry.get_registry(ctx.cfg.comfyui_path)
    # `ast.parse` holds the GIL for the whole of a file - a measured 152 ms on
    # one 563 KB node suite - so the extraction runs in worker processes
    # (QA-PERF-1).  `ex_ast` stays as the fallback if the pool cannot start.
    analyzed = map_cpu(ctx, _analyze, todo, phase="nodes", kind="node_package",
                       fallback=ctx.ex_ast)
    ready = [w for w in analyzed if w is not None]
    for work in ready:
        try:
            _apply_registry(ctx, work)
        except BaseException as exc:  # noqa: BLE001
            ctx.record_exception("nodes", "node_package", work.abs_path, exc)
        for path, code, message in (work.result.errors if work.result else []):
            ctx.record_error("nodes", "node_class", path, code, message)

    def _progress(done: int, total: int) -> None:
        ctx.bus.publish("progress", {
            "job_id": ctx.job_id, "phase": "nodes", "done": done, "total": total,
        }, coalesce_key="nodes")

    commit_batches(ctx, "nodes", "node_package", ready, _upsert,
                   on_progress=_progress, batch=8)
    ctx.bump(len(ready))
    ctx.bus.flush("nodes")

    total_classes = sum(len(w.result.classes if w.result else {}) for w in ready)
    official = sum(len(w.result.classes) for w in ready if w.is_official and w.result)
    zero = [w.folder_name for w in ready
            if not (w.result and w.result.classes)]
    return {
        "found": len(packages), "analyzed": len(ready),
        "skipped": len(packages) - len(todo), "classes": total_classes,
        "official_classes": official, "packages_without_classes": zero,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def registry_hint(node_id: str, comfy_root: Path | None) -> dict | None:
    return node_registry.get_registry(comfy_root).package_for_node(node_id)


def _unused(_: json) -> None:  # pragma: no cover
    return None
