"""Where ComfyUI's own workflow templates live, and what they say about themselves.

ComfyUI ships its official templates as Python distributions inside the
interpreter that runs it::

    <site-packages>/comfyui_workflow_templates_json/templates/<name>.json
    <site-packages>/comfyui_workflow_templates_json/templates/index.json   (+ index.<lang>.json)
    <site-packages>/comfyui_workflow_templates_core/manifest.json
    <site-packages>/comfyui_workflow_templates_media_*/templates/<name>-1.webp

Older installs carry everything in a single ``comfyui_workflow_templates``
distribution with the same ``templates/`` layout, which is also accepted.

This module only *reads*.  It never imports those packages (they are a
different interpreter's site-packages), never executes anything, and every
reader is bounded and never raises.  The indexer, the read-only file
resolution and the catalogue all ask it the same questions, so they can never
disagree about what is - and is not - a template file.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.pathsafe import long_path, path_key

#: Distributions whose ``templates/`` directory holds the workflow JSON, newest
#: layout first.
JSON_PACKAGES = ("comfyui_workflow_templates_json", "comfyui_workflow_templates")
#: Distributions that may carry ``manifest.json``.
MANIFEST_PACKAGES = ("comfyui_workflow_templates_core", "comfyui_workflow_templates")
MEDIA_PREFIX = "comfyui_workflow_templates_media"

#: ``index.json`` and its translations (``index.fr.json``, ``index.mcp.json``…)
#: sit beside the templates but are not templates.
INDEX_FILE_RE = re.compile(r"^index(\.[A-Za-z0-9_-]+)*\.json$", re.IGNORECASE)
IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif")

MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_MEDIA_FILES = 20_000
CACHE_TTL_S = 60.0


def is_index_file(name: str) -> bool:
    return bool(INDEX_FILE_RE.match(os.path.basename(str(name or ""))))


@dataclass
class TemplateMeta:
    """One template as ``index.json`` and the manifest describe it."""

    name: str
    title: str | None = None
    description: str | None = None
    category: str | None = None
    category_group: str | None = None
    media_type: str | None = None
    media_subtype: str | None = None
    tags: list[str] = field(default_factory=list)
    model_families: list[str] = field(default_factory=list)
    required_size: int | None = None
    open_source: bool | None = None
    tutorial_url: str | None = None
    min_comfyui: str | None = None
    released_on: str | None = None
    usage_rank: int | None = None
    bundle: str | None = None


@dataclass
class TemplateSource:
    site_packages: Path
    json_dir: Path
    package: str
    package_version: str | None
    media_dirs: list[Path] = field(default_factory=list)
    meta: dict[str, TemplateMeta] = field(default_factory=dict)
    _media: dict[str, str] | None = None

    def template_files(self) -> list[Path]:
        try:
            entries = sorted(os.scandir(long_path(str(self.json_dir))), key=lambda e: e.name)
        except OSError:
            return []
        out = []
        for e in entries:
            if not e.name.lower().endswith(".json") or is_index_file(e.name):
                continue
            try:
                if e.is_file(follow_symlinks=False):
                    out.append(Path(self.json_dir) / e.name)
            except OSError:
                continue
        return out

    def media_index(self) -> dict[str, str]:
        """filename -> absolute path across every media distribution."""
        if self._media is not None:
            return self._media
        out: dict[str, str] = {}
        for d in self.media_dirs:
            try:
                for e in os.scandir(long_path(str(d))):
                    if len(out) >= MAX_MEDIA_FILES:
                        break
                    if e.name.lower().endswith(IMAGE_EXTS):
                        out.setdefault(e.name, str(Path(d) / e.name))
            except OSError:
                continue
        self._media = out
        return out

    def thumbnails(self, name: str) -> list[str]:
        """The template's own preview images, ``-1`` first (all 689 at 0.11.x)."""
        media = self.media_index()
        meta = self.meta.get(name)
        subtypes = [meta.media_subtype] if meta and meta.media_subtype else []
        out: list[str] = []
        for idx in (1, 2):
            for ext in [*subtypes, "webp", "png", "jpg"]:
                cand = media.get(f"{name}-{idx}.{ext}")
                if cand and cand not in out:
                    out.append(cand)
                    break
        return out

    def contains(self, path: str | Path) -> bool:
        """Is this exactly a template JSON file of this source?"""
        p = Path(str(path))
        if p.suffix.lower() != ".json" or is_index_file(p.name):
            return False
        return path_key(p.parent) == path_key(self.json_dir)

    def contains_media(self, path: str | Path) -> bool:
        p = Path(str(path))
        if p.suffix.lower() not in IMAGE_EXTS:
            return False
        parent = path_key(p.parent)
        return any(parent == path_key(d) for d in self.media_dirs)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def _python_home(root: Path) -> Path | None:
    parent = root.parent if root.parent != root else None
    for candidate in filter(None, (parent, root)):
        for name in ("python_embeded", "python_embedded", "venv", ".venv"):
            home = candidate / name
            try:
                if (home / "python.exe").is_file() or (home / "bin" / "python").is_file():
                    return home
            except OSError:
                continue
    return None


def _site_packages(home: Path) -> Path | None:
    for rel in (Path("Lib") / "site-packages", Path("lib") / "site-packages"):
        cand = home / rel
        if cand.is_dir():
            return cand
    try:
        for entry in (home / "lib").iterdir():
            cand = entry / "site-packages"
            if cand.is_dir():
                return cand
    except OSError:
        pass
    return None


def _dist_version(site: Path, package: str) -> str | None:
    prefix = package.lower() + "-"
    try:
        for e in os.scandir(long_path(str(site))):
            n = e.name.lower()
            if n.startswith(prefix) and n.endswith(".dist-info"):
                return e.name[len(prefix):-len(".dist-info")]
    except OSError:
        pass
    return None


def _read_json(path: Path) -> object | None:
    try:
        if path.stat().st_size > MAX_INDEX_BYTES:
            return None
        return json.loads(Path(long_path(path)).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _str(v: object, cap: int = 2000) -> str | None:
    return str(v)[:cap] if isinstance(v, str) and v.strip() else None


def _strs(v: object, cap: int = 40) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x)[:120] for x in v if isinstance(x, str) and x.strip()][:cap]


def parse_index(data: object) -> dict[str, TemplateMeta]:
    """``index.json`` is a list of categories, each with a ``templates`` list."""
    out: dict[str, TemplateMeta] = {}
    if not isinstance(data, list):
        return out
    for cat in data:
        if not isinstance(cat, dict):
            continue
        cat_title = _str(cat.get("title"), 120)
        cat_group = _str(cat.get("category"), 120)
        for t in cat.get("templates") or []:
            if not isinstance(t, dict):
                continue
            name = _str(t.get("name"), 300)
            if not name or name in out:
                continue
            size = t.get("size")
            usage = t.get("usage")
            open_source = t.get("openSource")
            out[name] = TemplateMeta(
                name=name, title=_str(t.get("title"), 300),
                description=_str(t.get("description"), 4000),
                category=cat_title, category_group=cat_group,
                media_type=_str(t.get("mediaType"), 40),
                media_subtype=_str(t.get("mediaSubtype"), 20),
                tags=_strs(t.get("tags")), model_families=_strs(t.get("models")),
                required_size=int(size) if isinstance(size, (int, float)) and size >= 0 else None,
                open_source=open_source if isinstance(open_source, bool) else None,
                tutorial_url=_str(t.get("tutorialUrl"), 1000),
                min_comfyui=_str(t.get("minComfyUIVersion"), 40),
                released_on=_str(t.get("date"), 40),
                usage_rank=int(usage) if isinstance(usage, (int, float)) else None,
            )
    return out


def _apply_manifest(site: Path, meta: dict[str, TemplateMeta]) -> None:
    for pkg in MANIFEST_PACKAGES:
        data = _read_json(site / pkg / "manifest.json")
        if not isinstance(data, dict):
            continue
        for t in data.get("templates") or []:
            if not isinstance(t, dict):
                continue
            name = _str(t.get("id"), 300)
            if not name:
                continue
            entry = meta.setdefault(name, TemplateMeta(name=name))
            entry.bundle = entry.bundle or _str(t.get("bundle"), 80)
        return


def _locate(root: Path) -> TemplateSource | None:
    home = _python_home(root)
    site = _site_packages(home) if home else None
    if site is None:
        return None
    for pkg in JSON_PACKAGES:
        d = site / pkg / "templates"
        if d.is_dir():
            json_dir, package = d, pkg
            break
    else:
        return None
    media = []
    try:
        for e in sorted(os.scandir(long_path(str(site))), key=lambda e: e.name):
            if e.name.startswith(MEDIA_PREFIX) and not e.name.endswith(".dist-info"):
                cand = Path(site) / e.name / "templates"
                if cand.is_dir():
                    media.append(cand)
    except OSError:
        pass
    # The single-distribution layout keeps its media beside the JSON.
    if package == "comfyui_workflow_templates":
        media.append(json_dir)
    meta = parse_index(_read_json(json_dir / "index.json"))
    _apply_manifest(site, meta)
    return TemplateSource(site_packages=site, json_dir=json_dir, package=package,
                          package_version=_dist_version(site, package),
                          media_dirs=media, meta=meta)


_cache: dict[str, tuple[float, TemplateSource | None]] = {}
_lock = threading.Lock()


def locate(comfyui_root: str | Path | None, *, fresh: bool = False) -> TemplateSource | None:
    """The template source for this ComfyUI install, or ``None``.

    Cached briefly per install: file resolution asks on every template
    download, and the indexer asks with ``fresh=True``.
    """
    if not comfyui_root:
        return None
    root = Path(str(comfyui_root))
    key = path_key(root)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    try:
        source = _locate(root)
    except OSError:
        source = None
    with _lock:
        _cache[key] = (now, source)
    return source


def invalidate() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

MAX_DECLARED_MODELS = 200


def declared_models(graph: object) -> list[dict]:
    """``properties.models`` entries anywhere in a UI graph, subgraphs included.

    ComfyUI's own templates declare each model a loader needs, with the folder
    it belongs in and the URL the frontend offers when it is missing::

        {"name": "flux1-dev.safetensors", "url": "https://...", "directory": "diffusion_models"}

    Only ``https`` URLs are kept; anything else is dropped rather than shown.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def visit(obj: object, depth: int, via: str | None) -> None:
        if depth > 12 or len(out) >= MAX_DECLARED_MODELS:
            return
        if isinstance(obj, dict):
            node_type = obj.get("type") if isinstance(obj.get("type"), str) else via
            props = obj.get("properties")
            if isinstance(props, dict) and isinstance(props.get("models"), list):
                for m in props["models"]:
                    if not isinstance(m, dict):
                        continue
                    name = _str(m.get("name"), 400)
                    if not name:
                        continue
                    directory = _str(m.get("directory"), 120) or ""
                    key = (name.lower(), directory.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    url = _str(m.get("url"), 2000)
                    if url and not url.lower().startswith("https://"):
                        url = None
                    out.append({"name": name, "directory": directory or None,
                                "url": url, "via_class": node_type})
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    visit(v, depth + 1, node_type)
        elif isinstance(obj, list):
            for v in obj:
                if isinstance(v, (dict, list)):
                    visit(v, depth + 1, via)

    visit(graph, 0, None)
    return out


def graph_uuid(graph: object) -> str | None:
    """The frontend's top-level graph ``id`` - a UUID - when it has one."""
    if not isinstance(graph, dict):
        return None
    value = graph.get("id")
    if isinstance(value, str) and 8 <= len(value) <= 64 and re.fullmatch(r"[0-9A-Fa-f-]+", value):
        return value.lower()
    return None
