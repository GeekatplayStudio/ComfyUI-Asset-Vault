"""Windows-first path normalization and containment checks."""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import PathNotAllowed, ValidationError

LONG_PATH_THRESHOLD = 240
SEP = "\\"
LONG_PREFIX = "\\\\?\\"
UNC_PREFIX = "\\\\"
#: NTFS caps a single path component at 255 characters.  Enforcing it here
#: turns an over-long name into a 422 the user can act on, instead of a raw
#: OSError surfacing from whichever syscall runs first - which is also what
#: stands between a long name and a partially created folder tree.
MAX_COMPONENT_CHARS = 255

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL_NAME_CHARS = frozenset('<>:"/\\|?*')


@dataclass(frozen=True)
class Root:
    id: int
    kind: str
    path: str
    label: str
    category: str | None = None
    is_default: bool = False
    source: str = "config"


def normalize(p: str | Path) -> Path:
    """realpath-based normalization.  Never raises."""
    try:
        s = os.fspath(p)
    except TypeError:
        s = str(p)
    if not s:
        return Path("")
    if s.startswith(LONG_PREFIX):
        s = s[len(LONG_PREFIX):]
        if s.upper().startswith("UNC" + SEP):
            s = UNC_PREFIX + s[4:]
    try:
        s = os.path.realpath(s)
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            s = os.path.abspath(s)
    return Path(s)


#: Fold case on the platforms whose default filesystems ignore it.  Windows
#: (NTFS) and macOS (APFS/HFS+ as shipped) are case-insensitive; Linux is not.
#: ``os.path.normcase`` alone gets macOS wrong - it is the identity there, so
#: one file spelled two ways would index as two rows.  A deliberately
#: case-sensitive APFS volume is the rarer setup and trades the other way.
FOLD_CASE = os.name == "nt" or sys.platform == "darwin"


def fold_case(s: str) -> str:
    """The canonical spelling of a path for keys and containment checks."""
    return os.path.normcase(s).lower() if FOLD_CASE else s


def path_key(p: str | Path) -> str:
    """Case-folded canonical key used for UNIQUE constraints."""
    return fold_case(str(normalize(p)))


def long_path(p: str | Path) -> str:
    """Apply the Windows long-path prefix when a path needs it."""
    s = str(p)
    if os.name != "nt" or s.startswith(LONG_PREFIX):
        return s
    if len(s) < LONG_PATH_THRESHOLD:
        return s
    try:
        s = os.path.abspath(s)
    except (OSError, ValueError):
        return s
    if s.startswith(UNC_PREFIX):
        return LONG_PREFIX + "UNC" + SEP + s[len(UNC_PREFIX):]
    return LONG_PREFIX + s


def is_contained(child: str | Path, root: str | Path) -> bool:
    """True when ``child`` lies inside ``root`` (inclusive).  Never raises."""
    c, r = normalize(child), normalize(root)
    cs, rs = fold_case(str(c)), fold_case(str(r))
    if not rs or rs == ".":
        return False
    if os.name == "nt" and c.drive.lower() != r.drive.lower():
        return False
    if cs == rs:
        return True
    try:
        return os.path.commonpath([cs, rs]) == rs
    except (ValueError, OSError):
        return False


def resolve_within_roots(p: str | Path, roots) -> tuple[Path, Root]:
    """Return the normalized path plus its owning root, or raise PathNotAllowed."""
    target = normalize(p)
    best: tuple[int, Root] | None = None
    for root in roots:
        if is_contained(target, root.path):
            depth = len(str(normalize(root.path)))
            if best is None or depth > best[0]:
                best = (depth, root)
    if best is None:
        raise PathNotAllowed(
            "Path is outside every configured root.",
            details={"path": str(target)},
        )
    return target, best[1]


def validate_filename(name: str) -> None:
    """Raise ValidationError if ``name`` is not a safe single path component."""
    if not name or not name.strip():
        raise ValidationError("Name may not be empty.")
    if name in (".", ".."):
        raise ValidationError("Name may not be '.' or '..'.")
    if os.path.basename(name) != name or os.path.isabs(name) or "/" in name or SEP in name:
        raise ValidationError("Name must be a single path component.")
    bad = _ILLEGAL_NAME_CHARS & set(name)
    if bad:
        raise ValidationError("Name may not contain " + " ".join(sorted(bad)))
    if any(ord(ch) < 0x20 for ch in name):
        raise ValidationError("Name may not contain control characters.")
    if name.endswith((".", " ")):
        raise ValidationError("Name may not end with a dot or a space.")
    if len(name) > MAX_COMPONENT_CHARS:
        raise ValidationError(
            f"Name may not be longer than {MAX_COMPONENT_CHARS} characters "
            f"(this one is {len(name)}).")
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED:
        raise ValidationError(f"'{stem}' is a reserved Windows device name.")


def safe_relpath(child: str | Path, root: str | Path) -> str:
    """Relative path of ``child`` under ``root``; falls back to the basename."""
    try:
        rel = os.path.relpath(str(child), str(root))
    except (ValueError, OSError):
        return os.path.basename(str(child))
    return rel if not rel.startswith("..") else os.path.basename(str(child))
