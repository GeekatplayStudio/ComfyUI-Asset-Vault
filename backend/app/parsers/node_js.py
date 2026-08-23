"""Node types the ComfyUI **frontend** registers, found by reading text (B4b).

A ComfyUI node class does not have to come from Python.  Two families exist that
``node_ast`` can never see, and both were being reported as missing packages:

*Core virtual nodes* - ``Note``, ``MarkdownNote``, ``Reroute``,
``PrimitiveNode``.  They are drawn and evaluated entirely by the web client,
never appear in ``/object_info``, and no ``.py`` in the install declares them.

*Package JavaScript* - a package may ship ``web/**/*.js`` that calls
``LiteGraph.registerNodeType("GetNode", ...)``.  ``ComfyUI-KJNodes`` registers
``GetNode`` and ``SetNode`` exactly that way and has no Python class for either,
which is why both were reported missing while the package sat installed with
241 classes indexed.

**Absolute rule, same as ``node_ast``: nothing read here is ever executed.**
There is no JavaScript engine, no ``eval``, no ``subprocess`` - the scan is
``re`` over decoded text, and a name is only accepted when it is written as a
string literal (or as a ``const`` bound to one) at the call site.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.pathsafe import long_path

#: Directories a package keeps its client code in.
WEB_DIRS = ("web", "js", "dist")
JS_EXTS = (".js", ".mjs")
#: Bounds.  A vendored bundle must never turn the scan into a full-text search.
MAX_JS_BYTES = 4 * 1024 * 1024
MAX_JS_FILES = 600
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".github", "locales", "types"}

#: Node types provided by the ComfyUI web client itself.  They carry no Python
#: class in any install, so "is the package installed?" is not a meaningful
#: question for them - they are always present, and always were.
CORE_VIRTUAL_NODES: tuple[tuple[str, str, str], ...] = (
    ("Note", "Note", "Sticky note drawn on the canvas by the web client."),
    ("MarkdownNote", "Markdown Note", "Sticky note rendered as Markdown."),
    ("Reroute", "Reroute", "Routes one link through a pass-through point."),
    ("PrimitiveNode", "Primitive", "Feeds a literal value into a widget input."),
)
CORE_VIRTUAL_NODE_IDS = frozenset(entry[0] for entry in CORE_VIRTUAL_NODES)

_QUOTED = r"\"([^\"\r\n]{1,120})\"|'([^'\r\n]{1,120})'|`([^`\r\n$]{1,120})`"
_IDENT = r"[A-Za-z_$][\w$]{0,80}"

#: ``LiteGraph.registerNodeType("SetNode", SetNode)`` and the bare form.
_REGISTER_RE = re.compile(
    r"registerNodeType\s*\(\s*(?:" + _QUOTED + r"|(" + _IDENT + r"))")
#: ``const NODE_NAME = "Foo"`` so an identifier argument can still be resolved.
_CONST_RE = re.compile(
    r"(?:const|let|var)\s+(" + _IDENT + r")\s*=\s*(?:" + _QUOTED + r")\s*[;\r\n]")
#: ``comfyClass: "Foo"`` / ``comfyClass = "Foo"`` - a declaration, never ``===``.
_COMFYCLASS_RE = re.compile(r"comfyClass\s*(?::|=(?!=))\s*(?:" + _QUOTED + r")")

#: A registered type is a class id, not a sentence, a URL or a file name.
_NAME_OK = re.compile(r"^[^\s/\\][^\r\n]{0,99}$")
_REJECT = re.compile(r"^(?:https?:|\.{1,2}[/\\])|\.(?:js|mjs|css|json|png|svg)$",
                     re.IGNORECASE)


@dataclass(frozen=True)
class JsNode:
    node_id: str
    source_file: str
    source_lineno: int


def _first_group(match: re.Match, start: int, count: int) -> str | None:
    for i in range(start, start + count):
        if match.group(i) is not None:
            return match.group(i)
    return None


def _plausible(name: str) -> bool:
    text = name.strip()
    if not text or not _NAME_OK.match(text) or _REJECT.search(text):
        return False
    return any(ch.isalpha() for ch in text)


def read_text(path: Path) -> str | None:
    p = long_path(path)
    try:
        if os.path.getsize(p) > MAX_JS_BYTES:
            return None
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def walk_web_files(pkg_dir: Path, limit: int = MAX_JS_FILES) -> list[Path]:
    """Every ``.js``/``.mjs`` under the package's client directories.

    Never descends a reparse point: a package is third-party content, and a
    junction inside one would otherwise let the scan read JavaScript from
    anywhere on the disk (S-01, the rule ``walk_python_files`` already follows).
    """
    from ..indexing.walker import is_reparse_point

    out: list[Path] = []
    stack: list[Path] = []
    for name in WEB_DIRS:
        cand = pkg_dir / name
        try:
            if cand.is_dir():
                stack.append(cand)
        except OSError:
            continue
    while stack and len(out) < limit:
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
                    if e.name in SKIP_DIRS or e.name.startswith("."):
                        continue
                    stack.append(Path(e.path))
                elif e.name.endswith(JS_EXTS):
                    out.append(Path(e.path))
                    if len(out) >= limit:
                        break
            except OSError:
                continue
    return out


def scan_text(text: str) -> list[tuple[str, int]]:
    """Return ``(node_id, lineno)`` for every type this source registers."""
    consts: dict[str, str] = {}
    for m in _CONST_RE.finditer(text):
        value = _first_group(m, 2, 3)
        if value is not None:
            consts[m.group(1)] = value

    found: dict[str, int] = {}
    for m in _REGISTER_RE.finditer(text):
        name = _first_group(m, 1, 3)
        if name is None:
            name = consts.get(m.group(4) or "")
        if name and _plausible(name):
            found.setdefault(name.strip(), text.count("\n", 0, m.start()) + 1)
    for m in _COMFYCLASS_RE.finditer(text):
        name = _first_group(m, 1, 3)
        if name and _plausible(name):
            found.setdefault(name.strip(), text.count("\n", 0, m.start()) + 1)
    return sorted(found.items())


def scan_package(pkg_dir: Path, limit: int = MAX_JS_FILES) -> list[JsNode]:
    """Node types registered by a package's shipped JavaScript."""
    out: list[JsNode] = []
    seen: set[str] = set()
    for path in walk_web_files(pkg_dir, limit):
        text = read_text(path)
        if not text:
            continue
        if "registerNodeType" not in text and "comfyClass" not in text:
            continue
        try:
            rel = str(path.relative_to(pkg_dir)).replace("\\", "/")
        except ValueError:
            rel = path.name
        for node_id, lineno in scan_text(text):
            if node_id in seen:
                continue
            seen.add(node_id)
            out.append(JsNode(node_id=node_id, source_file=rel, source_lineno=lineno))
    return out
