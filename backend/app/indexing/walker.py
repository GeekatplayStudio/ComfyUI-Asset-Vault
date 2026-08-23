"""Iterative ``os.scandir`` walker.

Yields ``(path, size, mtime_ns, ctime_ns)`` straight from the ``DirEntry`` stat
cache - no extra syscall per file - and never recurses, so a pathological tree
cannot blow the stack.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import QUARANTINE_DIRNAME, TRASH_DIRNAME

SKIP_DIRS = {
    "__pycache__", ".git", ".svn", ".hg", "node_modules", "$RECYCLE.BIN",
    "System Volume Information", TRASH_DIRNAME, QUARANTINE_DIRNAME,
    ".vault-thumbs",
}
SKIP_FILES = {
    "desktop.ini", "thumbs.db", ".ds_store", ".gitkeep", ".gitignore",
    "put_models_here", "putmodelshere",
}
SKIP_EXTS = {
    ".crdownload", ".part", ".tmp", ".download", ".partial", ".lnk", ".url", ".!qb",
}
MODEL_EXTS = {
    ".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx",
    ".pkl", ".npz", ".engine", ".trt",
}
MIN_MODEL_BYTES = 4096

# SECURITY (SECURITY_REVIEW S-01).  On Windows `is_dir(follow_symlinks=False)` is
# True for an NTFS *junction* - `is_symlink()` only covers IO_REPARSE_TAG_SYMLINK,
# not IO_REPARSE_TAG_MOUNT_POINT - so a `mklink /J` (no elevation required)
# walked the indexer straight out of every configured root.
#
# Policy: NO reparse point is ever descended, not even one that resolves back
# inside a root.  Following an in-root junction would index the same file under
# two path_keys and can loop, and the supported way to index a second location is
# to add a root or an `extra_model_paths.yaml` entry - which is validated.  Every
# skip is recorded so it is visible rather than silent.
_REPARSE = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_point(entry_or_stat) -> bool:
    """True for a junction, symlink or mount point.  Never raises."""
    try:
        st = (entry_or_stat if hasattr(entry_or_stat, "st_mode")
              else entry_or_stat.stat(follow_symlinks=False))
    except (OSError, ValueError, AttributeError):
        return False
    if getattr(st, "st_reparse_tag", 0):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


@dataclass(frozen=True)
class FileEntry:
    path: str
    name: str
    size: int
    mtime_ns: int
    ctime_ns: int

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]


def walk(root: str | Path, *, max_depth: int = 24,
         skip_dirs: set[str] | None = None,
         on_skip: Callable[[str, str], None] | None = None) -> Iterator[FileEntry]:
    """Recursively yield every file under ``root``.  Never raises.

    ``on_skip(path, reason)`` is called for anything deliberately not walked, so
    a refused junction shows up in ``scan_errors`` instead of vanishing.
    """
    skip = SKIP_DIRS if skip_dirs is None else (SKIP_DIRS | skip_dirs)
    stack: list[tuple[str, int]] = [(str(root), 0)]
    while stack:
        cur, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            it = os.scandir(cur)
        except (OSError, ValueError):
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except (OSError, ValueError):
                    continue
                try:
                    if is_reparse_point(entry):
                        if on_skip is not None:
                            on_skip(entry.path, "reparse_point")
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in skip or entry.name.startswith("$"):
                            continue
                        stack.append((entry.path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except (OSError, ValueError):
                    continue
                lower = entry.name.lower()
                if lower in SKIP_FILES or lower.startswith("put_"):
                    continue
                yield FileEntry(
                    path=entry.path,
                    name=entry.name,
                    size=int(st.st_size),
                    mtime_ns=int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
                    ctime_ns=int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9))),
                )


def walk_models(root: str | Path, on_skip=None) -> Iterator[FileEntry]:
    """Model files only, with the skip list from ARCHITECTURE 4.3.2 applied."""
    for e in walk(root, on_skip=on_skip):
        ext = e.ext
        if ext in SKIP_EXTS or ext not in MODEL_EXTS:
            continue
        if e.size < MIN_MODEL_BYTES:
            continue
        yield e


def walk_partials(root: str | Path) -> Iterator[FileEntry]:
    """Interrupted downloads - reported as a health item, never indexed."""
    for e in walk(root):
        if e.ext in SKIP_EXTS:
            yield e


def walk_json(root: str | Path, on_skip=None) -> Iterator[FileEntry]:
    for e in walk(root, on_skip=on_skip):
        if e.ext == ".json":
            yield e


def top_level_dirs(root: str | Path) -> list[tuple[str, str]]:
    """(name, path) of the immediate subdirectories of ``root``."""
    out: list[tuple[str, str]] = []
    try:
        for e in os.scandir(str(root)):
            try:
                if is_reparse_point(e):
                    continue
                if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS:
                    out.append((e.name, e.path))
            except OSError:
                continue
    except OSError:
        return out
    out.sort(key=lambda t: t[0].lower())
    return out


def dir_mtime_ns(path: str | Path) -> int:
    try:
        st = os.stat(str(path))
        return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return 0
