"""Where did this workflow come from?  (REQUIREMENTS_R2 C8.4)

A pure path classifier, shared by the indexing phase and by migration ``m004``
so a backfilled row and a freshly scanned row can never disagree.

Three origins, and only three:

``user``
    Anything the owner put in ``<root>\\workflows`` or
    ``<root>\\user\\default\\workflows``, or in a manually added workflow root.
    A bundle the owner *copied* into their own workflow folder is still theirs.

``bundled``
    Physically inside an installed node package - ``custom_nodes\\<pkg>\\...``.
    ``origin_package`` names the package folder.  This is a measured fact: the
    file lives inside that package's directory.

``official_template``
    Shipped by ComfyUI itself in the ``comfyui_workflow_templates*`` distributions.
    These are catalogued read-only (see ``services/comfyui_service.py``); they are
    deliberately not indexed as vault rows, so this value does not normally appear
    on a ``workflows`` row.
"""

from __future__ import annotations

import os

ORIGINS = ("user", "bundled", "official_template")

CUSTOM_NODES = "custom_nodes"
#: Directory names a node package uses to ship its own example graphs.
EXAMPLE_DIR_NAMES = frozenset(
    {"workflows", "example_workflows", "examples_workflows", "example_wf"}
)


def _segments(rel_path: str) -> list[str]:
    text = str(rel_path or "").replace("\\", "/").strip("/")
    return [s for s in text.split("/") if s and s != "."]


def classify(rel_path: str, abs_path: str | None = None) -> tuple[str, str | None]:
    """Return ``(origin, origin_package)`` for one workflow file.

    ``rel_path`` is relative to its owning root, which is what the indexer already
    stores.  ``abs_path`` is consulted only when ``rel_path`` is unusable (a file
    outside its root falls back to a bare basename).
    """
    parts = _segments(rel_path)
    if len(parts) < 2 and abs_path:
        parts = _segments(abs_path)

    for i, part in enumerate(parts[:-1]):
        if part.lower() == CUSTOM_NODES and i + 1 < len(parts) - 1:
            package = parts[i + 1]
            if package:
                return "bundled", package
    return "user", None


def label(origin: str, package: str | None) -> str:
    """The exact wording C8.4 asks for, ready for the UI."""
    if origin == "bundled":
        return f"bundled with {package}" if package else "bundled with a node package"
    if origin == "official_template":
        return "official template"
    return "user"


def example_dir_hint(rel_path: str) -> str | None:
    """Package name suggested by a copied example folder, e.g.
    ``workflows/ComfyUI-LTXVideo/example_workflows/2.0`` -> ``ComfyUI-LTXVideo``.

    This is an *inferred* hint about a file the owner copied into their own
    workflow folder.  It never changes ``origin``; it only lets the UI say where
    a graph probably came from.  Violet, not amber (C11).
    """
    parts = _segments(rel_path)
    for i, part in enumerate(parts):
        if i > 0 and part.lower() in EXAMPLE_DIR_NAMES:
            candidate = parts[i - 1]
            if candidate and candidate.lower() not in EXAMPLE_DIR_NAMES:
                return candidate
    return None


def is_under(rel_path: str, *names: str) -> bool:
    lowered = {n.lower() for n in names}
    return any(p.lower() in lowered for p in _segments(os.path.dirname(rel_path or "")))
