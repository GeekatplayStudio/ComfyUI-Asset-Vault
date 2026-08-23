"""Workflow ``.json`` analysis.

Delegates every graph read to ``graph_utils`` (B1).  Handles both the UI format
(``{"nodes": [...], "links": [...]}``) and the API format
(``{"1": {"class_type": ...}}``), plus files that carry both.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core import errors
from ..core.pathsafe import long_path
from . import graph_utils

GRAPH_CAP = 8 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024

CAPABILITY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("video", ("WanVideo", "VHS_", "VideoCombine", "SVD_", "AnimateDiff", "LTX",
               "Hunyuan", "CogVideo", "Mochi", "SaveWEBM", "SaveVideo")),
    ("audio", ("AudioEncoder", "SaveAudio", "AceStep", "StableAudio", "VAEEncodeAudio",
               "MusicMapper")),
    ("3d", ("Hunyuan3D", "Mesh", "GLB", "Load3D", "Preview3D")),
    ("upscale", ("UpscaleModel", "ImageUpscale", "UltimateSDUpscale", "LatentUpscale",
                 "GenUpscale")),
    ("controlnet", ("ControlNet", "Preprocessor", "DepthAnything", "Canny", "OpenPose")),
    ("lora", ("LoraLoader", "LoraSelect", "Power Lora")),
    ("inpaint", ("InpaintModel", "VAEEncodeForInpaint", "SetLatentNoiseMask", "Inpaint")),
    ("img2img", ("LoadImage", "ImageScale", "VAEEncode")),
    ("txt2img", ("EmptyLatentImage", "EmptySD3LatentImage")),
    ("ipadapter", ("IPAdapter",)),
    ("face", ("FaceDetailer", "ReActor", "InstantID", "PuLID", "FaceRestore")),
    ("api", ("Openai", "Gemini", "Anthropic", "Kling", "Luma", "Runway", "Veo", "Pika")),
)

MODALITY_BY_CAPABILITY = {
    "video": "video", "audio": "audio", "3d": "3d",
}


@dataclass
class WorkflowResult:
    ok: bool = False
    error_code: str | None = None
    error_message: str | None = None
    fmt: str = "unknown"
    schema_version: str | None = None
    node_count: int = 0
    link_count: int = 0
    group_count: int = 0
    has_subgraphs: bool = False
    subgraph_count: int = 0
    #: subgraph id (a UUID) -> its declared name, for every definition the file
    #: carries.  A node whose ``type`` is one of these keys instantiates that
    #: subgraph; it is an internal reference, never an installable dependency.
    subgraph_defs: dict[str, str] = field(default_factory=dict)
    #: subgraph id -> how many nodes instantiate it.
    subgraph_types: dict[str, int] = field(default_factory=dict)
    title: str | None = None
    author: str | None = None
    node_types: dict[str, int] = field(default_factory=dict)
    summary: graph_utils.GraphSummary | None = None
    capability_tags: list[str] = field(default_factory=list)
    base_model_family: str | None = None
    modality: str | None = None
    graph_json: str | None = None
    graph_truncated: bool = False
    dependencies: list[dict] = field(default_factory=list)
    unresolved_inputs: int = 0


def _read_json(path: str | Path) -> tuple[object | None, str | None, str | None]:
    p = long_path(path)
    try:
        size = os.path.getsize(p)
        if size > MAX_FILE:
            return None, errors.HEADER_TOO_LARGE, f"Workflow is {size} bytes."
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, errors.classify_os_error(exc), str(exc)[:400]
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text), None, None
    except ValueError as exc:
        return None, errors.JSON_INVALID, str(exc)[:400]


MAX_SUBGRAPH_DEPTH = 8
MAX_SUBGRAPH_DEFS = 2000


def collect_subgraph_defs(data: object) -> dict[str, str]:
    """Every subgraph id a workflow declares, mapped to its name.

    The frontend stores them under ``definitions.subgraphs`` (and, in older
    files, a bare ``subgraphs`` list); a node that instantiates one carries the
    definition's UUID as its ``type``.  Reading only the top level is not
    enough by construction - a subgraph may declare its own ``definitions`` -
    so the walk recurses, bounded on both depth and count.
    """
    out: dict[str, str] = {}

    def visit(obj: object, depth: int) -> None:
        if depth > MAX_SUBGRAPH_DEPTH or not isinstance(obj, dict):
            return
        lists: list[object] = []
        defs = obj.get("definitions")
        if isinstance(defs, dict):
            lists.append(defs.get("subgraphs"))
        lists.append(obj.get("subgraphs"))
        for entries in lists:
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("id")
                if isinstance(sid, str) and sid and len(out) < MAX_SUBGRAPH_DEFS:
                    name = entry.get("name")
                    out[sid] = str(name)[:200] if isinstance(name, str) else sid
                visit(entry, depth + 1)

    visit(data, 0)
    return out


def _detect_format(data: object) -> tuple[str, dict | None, dict | None]:
    """Return (format, api_graph, ui_graph)."""
    if not isinstance(data, dict):
        return "unknown", None, None
    has_ui = isinstance(data.get("nodes"), list)
    embedded_prompt = data.get("prompt")
    api = graph_utils.normalize_prompt_graph(embedded_prompt)
    if api is None and not has_ui:
        api = graph_utils.normalize_prompt_graph(data)
    if has_ui and api is not None:
        return "both", api, data
    if has_ui:
        return "ui", graph_utils.ui_graph_to_api(data), data
    if api is not None:
        return "api", api, None
    return "unknown", None, None


def _capabilities(node_types: dict[str, int]) -> list[str]:
    names = " ".join(node_types)
    tags: list[str] = []
    for tag, needles in CAPABILITY_RULES:
        if any(n in names for n in needles):
            tags.append(tag)
    if "img2img" in tags and "txt2img" in tags:
        tags.remove("img2img")
    return tags[:8]


def _family_from_refs(deps: list[dict]) -> str | None:
    from .arch_rules import FILENAME_FAMILY

    scores: dict[str, int] = {}
    for dep in deps:
        name = str(dep.get("ref_name") or "").lower()
        for token, family in FILENAME_FAMILY:
            if token in name:
                scores[family] = scores.get(family, 0) + (
                    3 if dep.get("category") in ("checkpoints", "diffusion_models", "unet")
                    else 1)
                break
    return max(scores, key=scores.get) if scores else None


def analyze(path: str | Path, *, name: str | None = None) -> WorkflowResult:
    """Parse and summarize one workflow file.  Never raises."""
    res = WorkflowResult()
    data, code, message = _read_json(path)
    if data is None:
        res.error_code = code or errors.JSON_INVALID
        res.error_message = message
        return res

    fmt, api, ui = _detect_format(data)
    res.fmt = fmt
    res.subgraph_defs = collect_subgraph_defs(data)
    res.subgraph_count = len(res.subgraph_defs)
    if api is None and ui is None:
        res.error_code = errors.JSON_INVALID
        res.error_message = "File is JSON but not a ComfyUI workflow."
        return res

    if isinstance(ui, dict):
        nodes = ui.get("nodes") or []
        res.node_count = len(nodes) if isinstance(nodes, list) else 0
        links = ui.get("links")
        res.link_count = len(links) if isinstance(links, list) else 0
        groups = ui.get("groups")
        res.group_count = len(groups) if isinstance(groups, list) else 0
        res.has_subgraphs = bool(res.subgraph_defs) or bool(
            ui.get("definitions") or ui.get("subgraphs"))
        ver = ui.get("version")
        res.schema_version = str(ver) if ver is not None else None
        extra = ui.get("extra")
        if isinstance(extra, dict):
            title = extra.get("title") or extra.get("workflow_name")
            res.title = str(title)[:300] if isinstance(title, str) else None
            author = extra.get("author")
            res.author = str(author)[:200] if isinstance(author, str) else None
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict) and isinstance(n.get("type"), str):
                    t = n["type"]
                    if t in res.subgraph_defs:
                        # An instance of a subgraph this very file declares.  It
                        # is not a node class, so it is not a dependency: the
                        # UUID used to be recorded as a missing node package.
                        res.subgraph_types[t] = res.subgraph_types.get(t, 0) + 1
                        res.has_subgraphs = True
                        continue
                    res.node_types[t] = res.node_types.get(t, 0) + 1
                    if t in ("Subgraph", "SubgraphNode"):
                        res.has_subgraphs = True

    if api:
        if not res.node_count:
            res.node_count = len(api)
        if not res.node_types:
            for node in api.values():
                t = node.get("class_type") if isinstance(node, dict) else None
                if not isinstance(t, str):
                    continue
                if t in res.subgraph_defs:
                    res.subgraph_types[t] = res.subgraph_types.get(t, 0) + 1
                    res.has_subgraphs = True
                    continue
                res.node_types[t] = res.node_types.get(t, 0) + 1
        if any(":" in str(k) for k in api):
            res.has_subgraphs = True

    res.summary = graph_utils.summarize_graph(api, ui)
    res.unresolved_inputs = res.summary.unresolved_count
    deps = [{**e, "dep_kind": "model"} for e in res.summary.models]
    deps += [{**e, "dep_kind": "model"} for e in res.summary.loras]
    res.dependencies = deps
    res.capability_tags = _capabilities(res.node_types)
    res.base_model_family = _family_from_refs(deps)
    for tag in res.capability_tags:
        if tag in MODALITY_BY_CAPABILITY:
            res.modality = MODALITY_BY_CAPABILITY[tag]
            break
    if res.modality is None:
        res.modality = "image"

    try:
        blob = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = None
    if blob is not None and len(blob) <= GRAPH_CAP:
        res.graph_json = blob
    else:
        res.graph_truncated = True

    if not res.title and name:
        res.title = None  # the filename already serves as the display name
    res.ok = True
    return res


def prompt_summary(s: graph_utils.GraphSummary | None, limit: int = 240) -> str | None:
    if s is None:
        return None
    text = s.positive_prompt
    if not text:
        return None
    text = " ".join(text.split())
    return text[:limit]
