"""Content-independent fingerprints used to make scans incremental."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .pathsafe import fold_case


def file_fingerprint(abs_path: str | Path, size: int, mtime_ns: int) -> str:
    """blake2b over (case-folded path, size, mtime_ns).  See ARCHITECTURE 3.2."""
    h = hashlib.blake2b(digest_size=16)
    h.update(fold_case(str(abs_path)).encode("utf-16-le", errors="replace"))
    h.update(b"\x1f")
    h.update(str(int(size)).encode())
    h.update(b"\x1f")
    h.update(str(int(mtime_ns)).encode())
    return h.hexdigest()


def folder_fingerprint(entries) -> str:
    """blake2b over the sorted (rel_path, size, mtime_ns) triples of a folder."""
    h = hashlib.blake2b(digest_size=16)
    for rel, size, mtime_ns in sorted(entries):
        h.update(fold_case(str(rel)).encode("utf-16-le", errors="replace"))
        h.update(b"\x1f")
        h.update(str(int(size)).encode())
        h.update(b"\x1f")
        h.update(str(int(mtime_ns)).encode())
        h.update(b"\x1e")
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", errors="replace"), digest_size=16).hexdigest()


def path_hash(abs_path: str | Path) -> str:
    return hashlib.blake2b(
        fold_case(str(abs_path)).encode("utf-16-le", errors="replace"),
        digest_size=16,
    ).hexdigest()
