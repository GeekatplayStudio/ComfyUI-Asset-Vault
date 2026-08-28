"""Output-file metadata: dimensions plus the embedded generation graph.

``Image.open`` reads the header only - no full decode - which is what keeps
3,569 outputs inside the 5 s budget.  Everything graph-shaped is handed to
``graph_utils``; this module never interprets a prompt itself (B1).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors, imaging
from ..core.pathsafe import long_path
from . import graph_utils, mp4_boxes

#: SECURITY_REVIEW S-05: pin Pillow's pixel budget before the first header read.
imaging.apply_budget()

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".exr", ".avif"}
# .gif is deliberately not here: the MP4 box parser cannot read it, while
# Pillow extracts its dimensions, frames and metadata natively (and <img>
# animates it in the interface).
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac"}
MODEL3D_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".ply", ".stl", ".usdz"}
TEXT_EXTS = {".txt", ".json", ".md", ".csv", ".yaml", ".yml"}

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".exr": "image/x-exr",
    ".avif": "image/avif",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".m4a": "audio/mp4",
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".obj": "text/plain",
    ".fbx": "application/octet-stream", ".ply": "application/octet-stream",
    ".stl": "model/stl",
    ".txt": "text/plain", ".json": "application/json", ".md": "text/markdown",
}

GRAPH_CAP = 2 * 1024 * 1024

_A1111_RE = re.compile(
    r"^(?P<positive>.*?)(?:\nNegative prompt:\s*(?P<negative>.*?))?"
    r"\n(?P<params>Steps:.*)$", re.DOTALL)
_A1111_KV_RE = re.compile(r"([A-Za-z ]+):\s*([^,]+)")


@dataclass
class OutputMeta:
    media_kind: str = "other"
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    frame_count: int | None = None
    has_alpha: int | None = None
    color_mode: str | None = None
    has_metadata: bool = False
    metadata_format: str | None = None
    summary: graph_utils.GraphSummary | None = None
    prompt_graph_json: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_info: dict = field(default_factory=dict)


def media_kind_for(ext: str) -> str:
    e = ext.lower()
    if e in IMAGE_EXTS and e not in VIDEO_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    if e in MODEL3D_EXTS:
        return "model3d"
    if e in TEXT_EXTS:
        return "text"
    return "other"


def _parse_a1111(text: str) -> graph_utils.GraphSummary | None:
    m = _A1111_RE.match(text.strip())
    if not m:
        return None
    s = graph_utils.GraphSummary()
    s.positive_prompt = (m.group("positive") or "").strip()[:8000] or None
    neg = m.group("negative")
    s.negative_prompt = neg.strip()[:8000] if neg else None
    params = m.group("params") or ""
    kv = {k.strip().lower(): v.strip() for k, v in _A1111_KV_RE.findall(params)}
    try:
        if "steps" in kv:
            s.steps = int(float(kv["steps"]))
    except (TypeError, ValueError):
        pass
    try:
        if "cfg scale" in kv:
            s.cfg = float(kv["cfg scale"])
    except (TypeError, ValueError):
        pass
    if "seed" in kv and re.fullmatch(r"-?\d{1,40}", kv["seed"]):
        s.seed = kv["seed"]
    s.sampler = kv.get("sampler")
    s.scheduler = kv.get("schedule type") or kv.get("scheduler")
    model = kv.get("model")
    if model:
        s.primary_model = model
        s.models = [{"ref_name": model, "category": "checkpoints",
                     "via_class": "a1111", "via_input": "model", "node_id": "0"}]
    size = kv.get("size")
    if size and "x" in size:
        try:
            w, h = size.lower().split("x", 1)
            s.width, s.height = int(w), int(h)
        except (TypeError, ValueError):
            pass
    s.provenance = {"positive_prompt": {"origin": "literal", "resolved": True}}
    return s


def _graph_json(value) -> str | None:
    if value is None:
        return None
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                            default=str)
    except (TypeError, ValueError):
        return None
    return s if len(s) <= GRAPH_CAP else None


def read_image(path: str | Path, meta: OutputMeta) -> None:
    try:
        from PIL import Image, ImageFile
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        meta.error_code = errors.IMAGE_UNREADABLE
        return
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    p = long_path(path)
    try:
        with Image.open(p, formats=imaging.open_formats()) as im:
            meta.width, meta.height = im.size
            if imaging.exceeds_budget(im.size):
                # Recorded as a scan error rather than decoded: the header is
                # cheap, the decode is not (SECURITY_REVIEW S-05).
                meta.error_code = errors.IMAGE_UNREADABLE
                meta.error_message = (
                    f"{im.size[0]}x{im.size[1]} exceeds the "
                    f"{imaging.MAX_IMAGE_PIXELS}-pixel decode budget.")
                return
            meta.color_mode = im.mode
            meta.has_alpha = 1 if (im.mode in ("RGBA", "LA", "PA")
                                   or "transparency" in im.info) else 0
            try:
                meta.frame_count = getattr(im, "n_frames", 1)
            except (OSError, AttributeError, ValueError):
                meta.frame_count = 1
            info = dict(im.info)
    except (OSError, ValueError, SyntaxError, MemoryError) as exc:
        meta.error_code = errors.classify_os_error(exc)
        if meta.error_code == errors.UNKNOWN:
            meta.error_code = errors.IMAGE_UNREADABLE
        meta.error_message = str(exc)[:500]
        return
    except Exception as exc:  # noqa: BLE001 - Pillow raises plugin-specific errors
        meta.error_code = errors.IMAGE_UNREADABLE
        meta.error_message = str(exc)[:500]
        return

    prompt = info.get("prompt")
    workflow = info.get("workflow")
    if prompt or workflow:
        meta.has_metadata = True
        meta.metadata_format = "comfy_prompt" if prompt else "comfy_workflow"
        meta.summary = graph_utils.summarize_graph(prompt, workflow)
        meta.prompt_graph_json = _graph_json(prompt or workflow)
        return
    params = info.get("parameters") or info.get("Parameters")
    if isinstance(params, str) and "Steps:" in params:
        meta.has_metadata = True
        meta.metadata_format = "a1111"
        meta.summary = _parse_a1111(params)
        return
    for key in ("Description", "comment", "Comment", "usercomment", "UserComment"):
        val = info.get(key)
        if isinstance(val, (str, bytes)):
            text = val.decode("utf-8", "replace") if isinstance(val, bytes) else val
            if text.strip().startswith("{"):
                s = graph_utils.summarize_graph(text)
                if s.node_count:
                    meta.has_metadata = True
                    meta.metadata_format = "comfy_prompt"
                    meta.summary = s
                    meta.prompt_graph_json = _graph_json(text)
                    return
            if "Steps:" in text:
                meta.has_metadata = True
                meta.metadata_format = "a1111"
                meta.summary = _parse_a1111(text)
                return
    meta.metadata_format = "none"


def read_video(path: str | Path, meta: OutputMeta) -> None:
    info = mp4_boxes.read_info(path)
    if info.ok:
        meta.width = info.width
        meta.height = info.height
        meta.duration_ms = info.duration_ms
    _read_sidecar_graph(path, meta)


def _read_sidecar_graph(path: str | Path, meta: OutputMeta) -> None:
    """Video/audio renders often carry a sibling ``<stem>.json`` workflow."""
    p = Path(path)
    for cand in (p.with_suffix(".json"), p.with_name(p.stem + "_workflow.json")):
        try:
            if not cand.is_file() or cand.stat().st_size > GRAPH_CAP:
                continue
            with open(long_path(cand), "rb") as fh:
                text = fh.read(GRAPH_CAP).decode("utf-8", "replace")
        except OSError:
            continue
        s = graph_utils.summarize_graph(text, text)
        if s.node_count:
            meta.has_metadata = True
            meta.metadata_format = "comfy_prompt"
            meta.summary = s
            meta.prompt_graph_json = text
        return


def read_audio(path: str | Path, meta: OutputMeta) -> None:
    _read_sidecar_graph(path, meta)


def read_output(path: str | Path, ext: str) -> OutputMeta:
    """Entry point for the outputs phase.  Never raises."""
    meta = OutputMeta()
    meta.media_kind = media_kind_for(ext)
    meta.mime = MIME_BY_EXT.get(ext.lower())
    try:
        if meta.media_kind == "image":
            read_image(path, meta)
        elif meta.media_kind == "video":
            read_video(path, meta)
        elif meta.media_kind == "audio":
            read_audio(path, meta)
        elif meta.media_kind in ("model3d", "text", "other"):
            meta.metadata_format = "none"
    except Exception as exc:  # noqa: BLE001 - one bad file must not abort a scan
        meta.error_code = errors.classify_os_error(exc)
        meta.error_message = str(exc)[:500]
    if meta.metadata_format is None:
        meta.metadata_format = "none"
    return meta


def file_times(st: os.stat_result) -> tuple[int, int]:
    """(mtime_ns, created_at_ms) from a stat result."""
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    born = getattr(st, "st_birthtime", None)
    ctime = born if born else st.st_ctime
    return mtime_ns, int(ctime * 1000)
