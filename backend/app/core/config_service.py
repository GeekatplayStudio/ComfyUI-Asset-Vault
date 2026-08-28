"""The ONLY module permitted to answer "where is ComfyUI?" (fixes B6).

The DB ``config`` table is the single persistent store.  ``get_config()`` reads it
once and caches in-process; ``set_config()`` writes and invalidates.  On restart
the cache is cold and reloads from the DB, so desync is structurally impossible.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR
from . import db as dbmod
from .pathsafe import Root, normalize, path_key

log = logging.getLogger(__name__)

_lock = threading.RLock()
_cache: AppConfig | None = None

# key -> (value_type, default)
DEFAULTS: dict[str, tuple[str, object]] = {
    "comfyui_path": ("str", None),
    "is_configured": ("bool", False),
    "auto_reindex": ("bool", True),
    "online_enabled": ("bool", False),
    "civitai_enabled": ("bool", False),
    "civitai_api_key": ("str", None),
    "ollama_enabled": ("bool", False),
    "ollama_url": ("str", "http://localhost:11434"),
    "ollama_model": ("str", "llama3.2"),
    "smart_search_enabled": ("bool", False),
    "smart_search_min_score": ("float", 0.30),
    # App self-update.  Checking is on by default but still cannot reach the
    # network unless ``online_enabled`` is also on.  Downloading is opt-in and
    # defaults OFF: fetching a new copy of the app is the owner's decision,
    # never a background one.
    "app_update_check_enabled": ("bool", True),
    "app_update_auto_download": ("bool", False),
    "app_update_last_check": ("int", 0),
    "app_update_skipped_version": ("str", None),
    "embedding_model_id": ("str", "all-MiniLM-L6-v2-int8"),
    "embedding_state": ("str", "not_installed"),
    "embedding_model_url": ("str", "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"),
    "hash_concurrency": ("int", 2),
    "hash_throttle_mbps": ("int", 0),
    "thumb_cache_max_mb": ("int", 2048),
    "thumb_video_ffmpeg": ("bool", False),
    "page_size_default": ("int", 100),
    "watch_enabled": ("bool", False),
    "trash_mode": ("str", "trash"),
    "trash_retention_days": ("int", 30),
    "read_held_extra_paths": ("bool", False),
    "extra_workflow_dirs": ("json", []),
    "mcp_read_only": ("bool", False),
    "needs_relink": ("bool", False),
    "ui_prefs_json": ("json", {}),
}

MODEL_CATEGORY_DIRS = (
    "checkpoints", "diffusion_models", "unet", "loras", "vae", "vae_approx", "clip",
    "clip_vision", "text_encoders", "controlnet", "embeddings", "upscale_models",
    "latent_upscale_models", "style_models", "gligen", "hypernetworks", "photomaker",
    "audio_encoders", "diffusers", "model_patches", "frame_interpolation",
    "geometry_estimation", "detection", "optical_flow", "background_removal", "LLM",
    "configs", "onnx", "sams", "ultralytics", "insightface", "ipadapter",
)


@dataclass(frozen=True)
class AppConfig:
    comfyui_path: Path | None
    is_configured: bool
    roots: tuple[Root, ...]
    auto_reindex: bool
    online_enabled: bool
    civitai_enabled: bool
    civitai_api_key: str | None
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    smart_search_enabled: bool
    hash_concurrency: int
    hash_throttle_mbps: int
    thumb_cache_max_mb: int
    page_size_default: int
    watch_enabled: bool
    trash_mode: str
    extra_workflow_dirs: tuple[Path, ...]
    embedding_model_id: str = "all-MiniLM-L6-v2-int8"
    embedding_state: str = "not_installed"
    embedding_model_url: str = ""
    read_held_extra_paths: bool = False
    trash_retention_days: int = 30
    thumb_video_ffmpeg: bool = False
    mcp_read_only: bool = False
    smart_search_min_score: float = 0.30
    app_update_check_enabled: bool = True
    app_update_auto_download: bool = False
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(self.raw)
        d["comfyui_path"] = str(self.comfyui_path) if self.comfyui_path else None
        d["is_configured"] = self.is_configured
        d.pop("civitai_api_key", None)
        d["civitai_api_key_set"] = bool(self.civitai_api_key)
        return d


# ---------------------------------------------------------------------------
# Raw key/value access
# ---------------------------------------------------------------------------

def _coerce(value: str | None, vtype: str, default: object) -> object:
    if value is None:
        return default
    try:
        if vtype == "bool":
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if vtype == "int":
            return int(float(value))
        if vtype == "float":
            return float(value)
        if vtype == "json":
            return json.loads(value)
        return value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _encode(value: object, vtype: str) -> str | None:
    if value is None:
        return None
    if vtype == "bool":
        return "1" if value else "0"
    if vtype == "json":
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def read_raw() -> dict:
    values = {k: v for k, (_t, v) in DEFAULTS.items()}
    try:
        conn = dbmod.get_ro()
        for row in dbmod.rows(conn, "SELECT key, value, value_type FROM config"):
            key = row["key"]
            vtype = DEFAULTS.get(key, (row["value_type"] or "str", None))[0]
            values[key] = _coerce(row["value"], vtype, DEFAULTS.get(key, ("str", None))[1])
    except sqlite3.DatabaseError:
        pass
    if not values.get("comfyui_path"):
        env = os.environ.get("COMFYUI_PATH")
        if env:
            values["comfyui_path"] = env
    return values


def write_raw(patch: dict) -> None:
    items = []
    now = dbmod.now_ms()
    for key, value in patch.items():
        vtype = DEFAULTS.get(key, ("str", None))[0]
        if isinstance(value, (dict, list)) and vtype != "json":
            vtype = "json"
        items.append((str(key), _encode(value, vtype), vtype, now))

    def _op(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO config(key,value,value_type,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "value_type=excluded.value_type, updated_at=excluded.updated_at",
            items,
        )
        conn.commit()

    dbmod.writer().run(_op)


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

def _extra_model_roots(comfy: Path, read_held: bool) -> list[tuple[str, Path]]:
    """Parse ``extra_model_paths.yaml`` if present.  Absence is normal and silent."""
    from ..parsers.extra_paths_yaml import parse_extra_model_paths

    names = ["extra_model_paths.yaml"]
    if read_held:
        names.append("extra_model_paths.yaml.hold")
    out: list[tuple[str, Path]] = []
    for name in names:
        p = comfy / name
        if p.exists():
            out.extend(parse_extra_model_paths(p))
    return out


def compute_roots(values: dict) -> tuple[Root, ...]:
    roots: list[Root] = []
    seen: set[str] = set()
    next_id = 1

    def add(kind: str, path: Path, label: str, *, category: str | None = None,
            source: str = "config", is_default: bool = False) -> None:
        nonlocal next_id
        key = path_key(path)
        if key in seen:
            return
        seen.add(key)
        roots.append(Root(id=next_id, kind=kind, path=str(normalize(path)), label=label,
                          category=category, is_default=is_default, source=source))
        next_id += 1

    raw_path = values.get("comfyui_path")
    comfy = normalize(raw_path) if raw_path else None
    if comfy is not None and str(comfy):
        add("comfyui", comfy, "ComfyUI", is_default=True)
        try:
            for cat, target in _extra_model_roots(comfy, bool(values.get("read_held_extra_paths"))):
                add("extra_models", Path(target), f"{cat} ({target})", category=cat, source="yaml")
        except Exception as exc:  # noqa: BLE001 - never let YAML break config load
            log.warning("extra_model_paths parse failed: %s", exc)

    for d in values.get("extra_workflow_dirs") or []:
        try:
            add("extra_workflows", Path(str(d)), f"Workflows ({d})", source="manual")
        except (OSError, ValueError):
            continue

    add("data", Path(DATA_DIR), "App data", source="config")
    return tuple(roots)


# ---------------------------------------------------------------------------
# Public API (frozen interface)
# ---------------------------------------------------------------------------

def _build(values: dict) -> AppConfig:
    raw_path = values.get("comfyui_path")
    comfy = normalize(raw_path) if raw_path else None
    if comfy is not None and not str(comfy):
        comfy = None
    return AppConfig(
        comfyui_path=comfy,
        is_configured=bool(values.get("is_configured")) and comfy is not None,
        roots=compute_roots(values),
        auto_reindex=bool(values.get("auto_reindex")),
        online_enabled=bool(values.get("online_enabled")),
        civitai_enabled=bool(values.get("civitai_enabled")),
        civitai_api_key=values.get("civitai_api_key") or None,
        ollama_enabled=bool(values.get("ollama_enabled")),
        ollama_url=str(values.get("ollama_url") or ""),
        ollama_model=str(values.get("ollama_model") or ""),
        smart_search_enabled=bool(values.get("smart_search_enabled")),
        hash_concurrency=max(1, min(8, int(values.get("hash_concurrency") or 2))),
        hash_throttle_mbps=max(0, int(values.get("hash_throttle_mbps") or 0)),
        thumb_cache_max_mb=max(64, int(values.get("thumb_cache_max_mb") or 2048)),
        page_size_default=max(1, min(500, int(values.get("page_size_default") or 100))),
        watch_enabled=bool(values.get("watch_enabled")),
        trash_mode=str(values.get("trash_mode") or "trash"),
        extra_workflow_dirs=tuple(Path(str(d)) for d in (values.get("extra_workflow_dirs") or [])),
        embedding_model_id=str(values.get("embedding_model_id") or "all-MiniLM-L6-v2-int8"),
        embedding_state=str(values.get("embedding_state") or "not_installed"),
        embedding_model_url=str(values.get("embedding_model_url") or ""),
        read_held_extra_paths=bool(values.get("read_held_extra_paths")),
        trash_retention_days=max(0, int(values.get("trash_retention_days") or 30)),
        thumb_video_ffmpeg=bool(values.get("thumb_video_ffmpeg")),
        mcp_read_only=bool(values.get("mcp_read_only")),
        # Clamped to a band where the floor still means something: 0 would
        # return the whole neighbour list, 0.9 would reject near-paraphrases.
        smart_search_min_score=max(0.05, min(0.9, float(
            values.get("smart_search_min_score") if values.get("smart_search_min_score")
            is not None else 0.30))),
        app_update_check_enabled=bool(values.get("app_update_check_enabled")),
        app_update_auto_download=bool(values.get("app_update_auto_download")),
        raw=values,
    )


def get_config() -> AppConfig:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _build(read_raw())
        return _cache


def set_config(patch: dict) -> AppConfig:
    global _cache
    clean = {k: v for k, v in patch.items() if k in DEFAULTS}
    unknown = set(patch) - set(clean)
    if unknown:
        log.warning("ignoring unknown config keys: %s", sorted(unknown))
    # Store the install itself, not the portable parent it may have been typed
    # as: every root, scan and custom_nodes lookup is derived from this value.
    if clean.get("comfyui_path"):
        clean["comfyui_path"] = str(resolve_comfyui_dir(str(clean["comfyui_path"])))
    if clean:
        write_raw(clean)
    with _lock:
        _cache = None
    return get_config()


def reload_config() -> AppConfig:
    global _cache
    with _lock:
        _cache = None
    return get_config()


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


# ---------------------------------------------------------------------------
# Derived helpers used by the indexer
# ---------------------------------------------------------------------------

def model_dirs(cfg: AppConfig | None = None) -> list[tuple[str, Path, Root]]:
    """(category, directory, owning root) for every model category directory."""
    cfg = cfg or get_config()
    out: list[tuple[str, Path, Root]] = []
    seen: set[str] = set()
    for root in cfg.roots:
        if root.kind == "comfyui":
            base = Path(root.path) / "models"
            if not base.is_dir():
                continue
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                key = path_key(e.path)
                if key in seen:
                    continue
                seen.add(key)
                out.append((e.name, Path(e.path), root))
        elif root.kind == "extra_models" and root.category:
            p = Path(root.path)
            key = path_key(p)
            if p.is_dir() and key not in seen:
                seen.add(key)
                out.append((root.category, p, root))
    return out


def workflow_dirs(cfg: AppConfig | None = None) -> list[tuple[Path, Root]]:
    """Both ``<root>\\workflows`` and ``<root>\\user\\default\\workflows`` (D5)."""
    cfg = cfg or get_config()
    out: list[tuple[Path, Root]] = []
    seen: set[str] = set()
    for root in cfg.roots:
        if root.kind == "comfyui":
            for rel in ("workflows", os.path.join("user", "default", "workflows")):
                p = Path(root.path) / rel
                key = path_key(p)
                if p.is_dir() and key not in seen:
                    seen.add(key)
                    out.append((p, root))
        elif root.kind == "extra_workflows":
            p = Path(root.path)
            key = path_key(p)
            if p.is_dir() and key not in seen:
                seen.add(key)
                out.append((p, root))
    return out


def output_dirs(cfg: AppConfig | None = None) -> list[tuple[Path, Root]]:
    cfg = cfg or get_config()
    out = []
    for root in cfg.roots:
        if root.kind == "comfyui":
            p = Path(root.path) / "output"
            if p.is_dir():
                out.append((p, root))
    return out


def custom_nodes_dirs(cfg: AppConfig | None = None) -> list[tuple[Path, Root]]:
    cfg = cfg or get_config()
    out = []
    for root in cfg.roots:
        if root.kind == "comfyui":
            p = Path(root.path) / "custom_nodes"
            if p.is_dir():
                out.append((p, root))
    return out


def comfyui_root(cfg: AppConfig | None = None) -> Path | None:
    cfg = cfg or get_config()
    return cfg.comfyui_path


#: Portable builds nest the real install one level down, next to
#: ``python_embeded`` and ``update``.  These names are tried before a scan.
COMFY_CHILD_HINTS = ("ComfyUI", "ComfyUI_windows_portable")
#: Bound on the "any child might be it" fallback, so pointing at a huge folder
#: (or a slow network drive) can never turn into an unbounded walk.
_MAX_CHILD_PROBE = 64


def looks_like_comfyui(p: Path) -> bool:
    """The marker test: a models/ directory plus the ComfyUI entry point."""
    try:
        return (p / "models").is_dir() and (
            (p / "main.py").is_file() or (p / "nodes.py").is_file())
    except OSError:
        return False


def resolve_comfyui_dir(path: str | Path) -> Path:
    """Map a portable parent onto the install inside it.

    A portable build is usually unpacked so that ``ComfyUI`` sits beside
    ``python_embeded`` and ``update``.  Typing that parent (often a whole
    drive, ``O:\\``) is the natural thing to do, so accept it and descend to
    the directory that actually holds ``main.py`` and ``models``.  Anything
    already pointing at an install is returned untouched.
    """
    p = normalize(path)
    if looks_like_comfyui(p):
        return p
    for name in COMFY_CHILD_HINTS:
        child = p / name
        if looks_like_comfyui(child):
            return child
    try:
        with os.scandir(p) as it:
            for n, entry in enumerate(it):
                if n >= _MAX_CHILD_PROBE:
                    break
                if entry.is_dir() and looks_like_comfyui(Path(entry.path)):
                    return Path(entry.path)
    except OSError:
        pass
    return p


def validate_comfyui_path(path: str | Path) -> dict:
    """Inspect a candidate install directory and report what was found."""
    typed = normalize(path)
    p = resolve_comfyui_dir(typed)
    result: dict = {"path": str(p), "valid": False, "found": {}, "issues": []}
    if str(p) != str(typed):
        # Tell the caller we looked inside, so the saved path is never a
        # surprise: the UI echoes this back before anything is written.
        result["resolved_from"] = str(typed)
    if not p.exists():
        result["issues"].append("Directory does not exist.")
        return result
    if not p.is_dir():
        result["issues"].append("Path is not a directory.")
        return result
    markers = {
        "main.py": (p / "main.py").is_file(),
        "nodes.py": (p / "nodes.py").is_file(),
        "models": (p / "models").is_dir(),
        "custom_nodes": (p / "custom_nodes").is_dir(),
        "output": (p / "output").is_dir(),
    }
    result["found"] = markers
    counts: dict[str, int] = {}
    if markers["models"]:
        for name in MODEL_CATEGORY_DIRS:
            d = p / "models" / name
            if d.is_dir():
                try:
                    counts[name] = sum(
                        1 for e in os.scandir(d)
                        if e.is_file() and not e.name.startswith("put_")
                    )
                except OSError:
                    counts[name] = 0
    result["model_counts"] = counts
    result["workflow_dirs"] = [
        str(d) for d in (p / "workflows", p / "user" / "default" / "workflows") if d.is_dir()
    ]
    result["valid"] = markers["models"] and (markers["main.py"] or markers["nodes.py"])
    if not result["valid"]:
        result["issues"].append(
            "This does not look like a ComfyUI installation "
            "(expected main.py/nodes.py plus a models/ directory)."
        )
    return result
