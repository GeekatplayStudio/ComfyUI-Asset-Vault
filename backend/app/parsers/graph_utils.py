"""The ONLY prompt-graph reader in the codebase (fixes B1).

ComfyUI's API-format ``prompt`` is ``{ "<node_id>": {"class_type": str,
"inputs": {name: value}} }`` where ``value`` is either a scalar **or a link**
``[<source_node_id>, <output_slot>]``.  Node ids are strings and may be
subgraph-qualified (``'88:97'``), so every lookup is by ``str(key)``.

Nothing outside this module may reach into a prompt graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

SCALAR_KINDS = (str, int, float, bool, type(None))
MAX_DEPTH = 12
MAX_TEXT = 8000

MODEL_EXTS = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft", ".pkl",
)

VALUE_PRODUCER_MAP: dict[str, tuple[str, ...]] = {
    "PrimitiveNode": ("value",),
    "PrimitiveString": ("value",),
    "PrimitiveStringMultiline": ("value",),
    "PrimitiveInt": ("value",),
    "PrimitiveFloat": ("value",),
    "PrimitiveBoolean": ("value",),
    "String": ("string", "value"),
    "StringConstant": ("string", "value"),
    "StringConstantMultiline": ("string", "value"),
    "Text": ("text",),
    "CR Text": ("text",),
    "ttN text": ("text",),
    "easy string": ("value",),
    "JoinStrings": ("string1", "string2"),
    "ImpactWildcardProcessor": ("populated_text", "wildcard_text"),
    "CLIPTextEncode": ("text",),
    "CLIPTextEncodeSDXL": ("text_g", "text_l"),
    "CLIPTextEncodeFlux": ("clip_l", "t5xxl"),
    "Reroute": ("*",),
    "Reroute (rgthree)": ("*",),
    "GetNode": ("value",),
    "SetNode": ("value",),
    # Additional producers observed on the target install.
    "ShowText|pysssss": ("text",),
    "StringConcatenate": ("string_a", "string_b"),
    "Textbox": ("text",),
    "CustomCombo": ("value", "text"),
    "ImpactWildcardEncode": ("populated_text", "wildcard_text"),
    "DPRandomGenerator": ("text",),
    "TextMultiline": ("text",),
    "Prompts Everywhere": ("+", "-"),
}

# class_type -> (input name -> category) for model references
MODEL_LOADER_INPUTS: dict[str, dict[str, str]] = {
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoints"},
    "CheckpointLoader": {"ckpt_name": "checkpoints"},
    "unCLIPCheckpointLoader": {"ckpt_name": "checkpoints"},
    "ImageOnlyCheckpointLoader": {"ckpt_name": "checkpoints"},
    "UNETLoader": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUF": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUFAdvanced": {"unet_name": "diffusion_models"},
    "DiffusersLoader": {"model_path": "diffusers"},
    "VAELoader": {"vae_name": "vae"},
    "CLIPLoader": {"clip_name": "text_encoders"},
    "DualCLIPLoader": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoader": {
        "clip_name1": "text_encoders", "clip_name2": "text_encoders",
        "clip_name3": "text_encoders",
    },
    "QuadrupleCLIPLoader": {
        "clip_name1": "text_encoders", "clip_name2": "text_encoders",
        "clip_name3": "text_encoders", "clip_name4": "text_encoders",
    },
    "CLIPLoaderGGUF": {"clip_name": "text_encoders"},
    "DualCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "CLIPVisionLoader": {"clip_name": "clip_vision"},
    "ControlNetLoader": {"control_net_name": "controlnet"},
    "DiffControlNetLoader": {"control_net_name": "controlnet"},
    "StyleModelLoader": {"style_model_name": "style_models"},
    "GLIGENLoader": {"gligen_name": "gligen"},
    "HypernetworkLoader": {"hypernetwork_name": "hypernetworks"},
    "UpscaleModelLoader": {"model_name": "upscale_models"},
    "PhotoMakerLoader": {"photomaker_model_name": "photomaker"},
    "IPAdapterModelLoader": {"ipadapter_file": "ipadapter"},
    "InstantIDModelLoader": {"instantid_file": "instantid"},
    "AudioEncoderLoader": {"audio_encoder_name": "audio_encoders"},
    "ModelPatchLoader": {"name": "model_patches"},
    "WanVideoModelLoader": {"model": "diffusion_models"},
    "WanVideoVAELoader": {"model_name": "vae"},
    "LoadWanVideoT5TextEncoder": {"model_name": "text_encoders"},
    "LoadWanVideoClipTextEncoder": {"model_name": "clip_vision"},
    "DownloadAndLoadFlorence2Model": {"model": "LLM"},
    "LatentUpscaleModelLoader": {"model_name": "latent_upscale_models"},
    "FrameInterpolationModelLoader": {"model_name": "frame_interpolation"},
}

LORA_LOADER_INPUTS: dict[str, dict[str, str]] = {
    "LoraLoader": {"lora_name": "loras"},
    "LoraLoaderModelOnly": {"lora_name": "loras"},
    "LoraLoaderBlockWeight": {"lora_name": "loras"},
    "LoraModelLoader": {"lora_name": "loras"},
    "WanVideoLoraSelect": {"lora": "loras"},
    "WanVideoLoraSelectMulti": {
        "lora_0": "loras", "lora_1": "loras", "lora_2": "loras", "lora_3": "loras",
    },
    "Power Lora Loader (rgthree)": {},
}

# UI-format workflows store widget values positionally in ``widgets_values`` and
# do NOT name them, so a positional table for the core classes is what makes a
# .json workflow yield real model references and prompts.
WIDGET_NAMES: dict[str, tuple[str, ...]] = {
    "CheckpointLoaderSimple": ("ckpt_name",),
    "CheckpointLoader": ("config_name", "ckpt_name"),
    "unCLIPCheckpointLoader": ("ckpt_name",),
    "ImageOnlyCheckpointLoader": ("ckpt_name",),
    "UNETLoader": ("unet_name", "weight_dtype"),
    "UnetLoaderGGUF": ("unet_name",),
    "VAELoader": ("vae_name",),
    "CLIPLoader": ("clip_name", "type", "device"),
    "DualCLIPLoader": ("clip_name1", "clip_name2", "type", "device"),
    "TripleCLIPLoader": ("clip_name1", "clip_name2", "clip_name3"),
    "QuadrupleCLIPLoader": ("clip_name1", "clip_name2", "clip_name3", "clip_name4"),
    "CLIPVisionLoader": ("clip_name",),
    "ControlNetLoader": ("control_net_name",),
    "DiffControlNetLoader": ("control_net_name",),
    "StyleModelLoader": ("style_model_name",),
    "GLIGENLoader": ("gligen_name",),
    "HypernetworkLoader": ("hypernetwork_name", "strength"),
    "UpscaleModelLoader": ("model_name",),
    "PhotoMakerLoader": ("photomaker_model_name",),
    "IPAdapterModelLoader": ("ipadapter_file",),
    "AudioEncoderLoader": ("audio_encoder_name",),
    "ModelPatchLoader": ("name",),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
    "CLIPTextEncode": ("text",),
    "CLIPTextEncodeSDXL": ("width", "height", "crop_w", "crop_h", "target_width",
                           "target_height", "text_g", "text_l"),
    "CLIPTextEncodeFlux": ("clip_l", "t5xxl", "guidance"),
    "KSampler": ("seed", "control_after_generate", "steps", "cfg", "sampler_name",
                 "scheduler", "denoise"),
    "KSamplerAdvanced": ("add_noise", "noise_seed", "control_after_generate", "steps",
                         "cfg", "sampler_name", "scheduler", "start_at_step",
                         "end_at_step", "return_with_leftover_noise"),
    "SamplerCustom": ("add_noise", "noise_seed", "control_after_generate", "cfg"),
    "KSamplerSelect": ("sampler_name",),
    "BasicScheduler": ("scheduler", "steps", "denoise"),
    "RandomNoise": ("noise_seed", "control_after_generate"),
    "BasicGuider": (),
    "FluxGuidance": ("guidance",),
    "EmptyLatentImage": ("width", "height", "batch_size"),
    "EmptySD3LatentImage": ("width", "height", "batch_size"),
    "EmptyLatentImagePresets": ("resolution", "batch_size"),
    "LatentUpscale": ("upscale_method", "width", "height", "crop"),
    "LoadImage": ("image", "upload"),
    "SaveImage": ("filename_prefix",),
    "PreviewImage": (),
    "VAEDecode": (),
    "VAEEncode": (),
    "ControlNetApplyAdvanced": ("strength", "start_percent", "end_percent"),
    "ControlNetApply": ("strength",),
    "PrimitiveNode": ("value", "control_after_generate"),
    "PrimitiveString": ("value",),
    "PrimitiveStringMultiline": ("value",),
    "PrimitiveInt": ("value", "control_after_generate"),
    "PrimitiveFloat": ("value",),
    "PrimitiveBoolean": ("value",),
    "Note": ("text",),
    "MarkdownNote": ("text",),
}

SAMPLER_CLASS_RE = re.compile(r"sampler", re.IGNORECASE)
SAMPLER_EXACT = {
    "KSampler", "KSamplerAdvanced", "KSampler (Efficient)", "SamplerCustom",
    "SamplerCustomAdvanced", "KSamplerSelect", "WanVideoSampler",
}

_TEXT_INPUT_NAMES = ("text", "prompt", "string", "text_g", "t5xxl", "clip_l", "positive", "value")

# A conditioning that has been zeroed carries no text by construction: walking
# through it lands on the positive encoder and reports the positive prompt as
# the negative one.  These classes terminate the walk.
ZERO_OUT_CLASSES = frozenset({
    "ConditioningZeroOut", "ConditioningZero", "ZeroOut", "ConditioningSetTimestepRange",
})

# Edit-style encoders (TextEncodeBooguEdit, TextEncodeMageFlowEdit, ...) emit the
# positive conditioning on output 0 and the negative on output 1 from two
# different inputs, so the output slot selects which input to read.
POSITIVE_INPUT_NAMES = ("text", "text_g", "t5xxl", "clip_l", "prompt", "positive_prompt",
                        "positive", "string", "value")
NEGATIVE_INPUT_NAMES = ("negative_prompt", "negative", "negative_text", "text_negative",
                        "neg_text", "neg")

# Origins that mean "we determined there is no negative prompt", as opposed to
# "we could not tell".  They are not failures and must not inflate telemetry.
DETERMINED_EMPTY_ORIGINS = frozenset({"zeroed", "empty", "same_as_positive"})


def _is_zero_out(class_name: str) -> bool:
    return class_name in ZERO_OUT_CLASSES or "ZeroOut" in class_name


def _encoder_input_names(inputs: dict, slot: int) -> tuple[tuple[str, ...], bool]:
    """(input names to read, exclusive) for a text encoder at output ``slot``."""
    negatives = tuple(n for n in NEGATIVE_INPUT_NAMES if n in inputs)
    if slot >= 1 and negatives:
        # A dual-output encoder: slot 1 is the negative, and it is the ONLY
        # candidate - falling back to the positive input is the bug being fixed.
        return negatives, True
    positives = tuple(n for n in POSITIVE_INPUT_NAMES
                      if n in inputs and n not in NEGATIVE_INPUT_NAMES)
    return positives, False


@dataclass(frozen=True)
class Resolved:
    value: Any | None
    origin: str  # literal | link | widget | unresolved
    source_node_id: str | None = None
    source_class_type: str | None = None
    depth: int = 0

    @property
    def resolved(self) -> bool:
        return self.origin in ("literal", "widget")

    def as_provenance(self) -> dict:
        d: dict = {"origin": self.origin, "resolved": self.resolved}
        if self.source_node_id is not None:
            d["source_node_id"] = self.source_node_id
        if self.source_class_type is not None:
            d["source_class_type"] = self.source_class_type
        return d


UNRESOLVED = Resolved(None, "unresolved")


def is_link(v: Any) -> bool:
    return (
        isinstance(v, list)
        and len(v) == 2
        and isinstance(v[0], (str, int))
        and not isinstance(v[0], bool)
        and isinstance(v[1], int)
        and not isinstance(v[1], bool)
    )


def node_inputs(graph: dict, node_id: Any) -> dict:
    node = graph.get(str(node_id))
    if not isinstance(node, dict):
        return {}
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def class_type(graph: dict, node_id: Any) -> str | None:
    node = graph.get(str(node_id))
    if not isinstance(node, dict):
        return None
    ct = node.get("class_type")
    return ct if isinstance(ct, str) else None


def resolve_input(graph: dict, node_id: Any, input_name: str, *,
                  max_depth: int = MAX_DEPTH,
                  _seen: frozenset = frozenset(),
                  _depth: int = 0) -> Resolved:
    """Resolve one input to a scalar, following links to value-producing nodes."""
    if not isinstance(graph, dict):
        return UNRESOLVED
    nid = str(node_id)
    inputs = node_inputs(graph, nid)
    if input_name not in inputs:
        return Resolved(None, "unresolved", source_node_id=nid, depth=_depth)
    return resolve_value(graph, inputs[input_name], owner=nid,
                         max_depth=max_depth, _seen=_seen, _depth=_depth)


def resolve_value(graph: dict, v: Any, *, owner: str | None = None,
                  max_depth: int = MAX_DEPTH,
                  _seen: frozenset = frozenset(),
                  _depth: int = 0) -> Resolved:
    """Resolve an arbitrary input value (scalar or link)."""
    if isinstance(v, SCALAR_KINDS):
        return Resolved(v, "literal", source_node_id=owner, depth=_depth)
    if not is_link(v):
        return Resolved(None, "unresolved", source_node_id=owner, depth=_depth)

    src_id = str(v[0])
    if src_id in _seen or _depth >= max_depth:
        return Resolved(None, "unresolved", source_node_id=src_id, depth=_depth)
    src = graph.get(src_id)
    if not isinstance(src, dict):
        return Resolved(None, "unresolved", source_node_id=src_id, depth=_depth)
    src_class = src.get("class_type")
    src_class = src_class if isinstance(src_class, str) else None

    producers = VALUE_PRODUCER_MAP.get(src_class or "")
    if producers is not None:
        src_inputs = node_inputs(graph, src_id)
        seen2 = _seen | {src_id}
        names: tuple[str, ...]
        names = tuple(src_inputs.keys()) if producers == ("*",) else producers
        for name in names:
            if name not in src_inputs:
                continue
            r = resolve_value(graph, src_inputs[name], owner=src_id,
                              max_depth=max_depth, _seen=seen2, _depth=_depth + 1)
            if r.resolved and r.value not in (None, ""):
                return Resolved(r.value, r.origin if r.origin == "literal" else "link",
                                source_node_id=src_id, source_class_type=src_class,
                                depth=_depth + 1)
        # Producer known but nothing usable inside it.
        return Resolved(None, "link", source_node_id=src_id,
                        source_class_type=src_class, depth=_depth + 1)

    return Resolved(None, "link", source_node_id=src_id,
                    source_class_type=src_class, depth=_depth)


# ---------------------------------------------------------------------------
# Convenience coercions
# ---------------------------------------------------------------------------

def as_text(r: Resolved, max_len: int = MAX_TEXT) -> str | None:
    v = r.value
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:max_len] if v else None


def as_int(r: Resolved) -> int | None:
    v = r.value
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v.strip()))
        except (TypeError, ValueError):
            return None
    return None


def as_float(r: Resolved) -> float | None:
    v = r.value
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f or f in (float("inf"), float("-inf")) else f
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def as_seed(r: Resolved) -> str | None:
    """Seeds can exceed INT64, so they are stored as TEXT."""
    v = r.value
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float):
        v = int(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return s if s and re.fullmatch(r"-?\d{1,40}", s) else None
    return None


def as_model_name(r: Resolved) -> str | None:
    """Strip directories, validate the extension."""
    v = r.value
    if not isinstance(v, str):
        return None
    s = v.strip().replace("\\", "/")
    if not s:
        return None
    lower = s.lower()
    if not lower.endswith(MODEL_EXTS):
        # Some loaders reference bare directory names (diffusers).
        return s[:512] if "/" in s or len(s) < 200 else None
    return s[:512]


def basename(ref: str) -> str:
    return ref.replace("\\", "/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Graph summary
# ---------------------------------------------------------------------------

@dataclass
class GraphSummary:
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    seed: str | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    denoise: float | None = None
    width: int | None = None
    height: int | None = None
    models: list[dict] = field(default_factory=list)
    loras: list[dict] = field(default_factory=list)
    node_types: list[str] = field(default_factory=list)
    node_count: int = 0
    unresolved_count: int = 0
    provenance: dict = field(default_factory=dict)
    graph_hash: str | None = None
    primary_model: str | None = None


def normalize_prompt_graph(prompt: Any) -> dict | None:
    """Accept a dict, a JSON string, or None; return an API-format graph dict."""
    if prompt is None:
        return None
    if isinstance(prompt, (bytes, bytearray)):
        try:
            prompt = prompt.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(prompt, str):
        s = prompt.strip()
        if not s:
            return None
        try:
            prompt = json.loads(s)
        except (ValueError, TypeError):
            return None
    if not isinstance(prompt, dict):
        return None
    # An API-format graph maps node ids to dicts carrying class_type.
    hits = 0
    for v in prompt.values():
        if isinstance(v, dict) and "class_type" in v:
            hits += 1
            if hits >= 1:
                break
    if hits:
        return {str(k): v for k, v in prompt.items() if isinstance(v, dict)}
    return None


def ui_graph_to_api(workflow: Any) -> dict | None:
    """Best-effort conversion of a UI-format workflow into an API-shaped graph.

    Only ``class_type`` and widget values are recovered - enough for node-type
    inventory, model references, and prompt text.  Link inputs become links.
    """
    if isinstance(workflow, (str, bytes, bytearray)):
        try:
            workflow = json.loads(
                workflow.decode("utf-8", "replace")
                if isinstance(workflow, (bytes, bytearray)) else workflow
            )
        except (ValueError, TypeError):
            return None
    if not isinstance(workflow, dict):
        return None
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return None

    # link id -> (origin node id, origin slot)
    link_src: dict[int, tuple[str, int]] = {}
    for link in workflow.get("links") or []:
        if isinstance(link, list) and len(link) >= 3:
            try:
                link_src[int(link[0])] = (str(link[1]), int(link[2]))
            except (TypeError, ValueError):
                continue

    api: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        ctype = node.get("type")
        if nid is None or not isinstance(ctype, str):
            continue
        inputs: dict[str, Any] = {}
        widgets = node.get("widgets_values")
        named: list[str] = []
        for inp in node.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            name = inp.get("name")
            if not isinstance(name, str):
                continue
            lid = inp.get("link")
            if lid is not None:
                try:
                    src = link_src.get(int(lid))
                except (TypeError, ValueError):
                    src = None
                if src:
                    inputs[name] = [src[0], src[1]]
                    continue
            if inp.get("widget"):
                named.append(name)
        if isinstance(widgets, list):
            table = WIDGET_NAMES.get(ctype)
            for i, val in enumerate(widgets):
                if i < len(named):
                    key = named[i]
                elif table and i < len(table):
                    key = table[i]
                else:
                    key = f"widget_{i}"
                if key not in inputs:
                    inputs[key] = val
        elif isinstance(widgets, dict):
            for k, val in widgets.items():
                if isinstance(k, str) and k not in inputs:
                    inputs[k] = val
        api[str(nid)] = {"class_type": ctype, "inputs": inputs,
                         "_meta": {"title": node.get("title")}}
    return api or None


def _find_samplers(graph: dict) -> list[str]:
    out = []
    for nid, node in graph.items():
        ct = node.get("class_type") if isinstance(node, dict) else None
        if not isinstance(ct, str):
            continue
        if ct in SAMPLER_EXACT or SAMPLER_CLASS_RE.search(ct):
            inputs = node_inputs(graph, nid)
            score = sum(1 for k in ("positive", "negative", "seed", "steps", "cfg") if k in inputs)
            out.append((score, nid))
    out.sort(key=lambda t: (-t[0], t[1]))
    return [nid for _s, nid in out]


def _text_from_conditioning(graph: dict, node_id: str, slot: int = 0, *, depth: int = 0,
                            seen: frozenset = frozenset()) -> Resolved:
    """Follow a conditioning chain back to the text encoder that produced it.

    ``slot`` matters: a pass-through such as ``ControlNetApplyAdvanced`` carries
    the positive conditioning on output 0 and the negative on output 1, so
    ignoring the slot is exactly how "first text seen is positive" goes wrong.
    """
    if depth > MAX_DEPTH or node_id in seen:
        return UNRESOLVED
    ct = class_type(graph, node_id) or ""
    inputs = node_inputs(graph, node_id)

    # A zeroed conditioning is definitively empty.  Terminate here instead of
    # walking upstream into the encoder that fed it, which is the positive one.
    if _is_zero_out(ct):
        return Resolved(None, "zeroed", source_node_id=node_id,
                        source_class_type=ct, depth=depth)

    if "TextEncode" in ct or "TextEncoder" in ct or ct in VALUE_PRODUCER_MAP:
        names, exclusive = _encoder_input_names(inputs, slot)
        for name in names:
            if name not in inputs:
                continue
            r = resolve_value(graph, inputs[name], owner=node_id, _depth=depth)
            if r.resolved and as_text(r):
                return r
            if r.origin == "link":
                return r
            if r.resolved:
                # Present but blank: an honest "no negative prompt".
                return Resolved(None, "empty", source_node_id=node_id,
                                source_class_type=ct, depth=depth)
        if exclusive:
            # The negative input exists but yielded nothing; never borrow the
            # positive text to fill the gap.
            return Resolved(None, "empty", source_node_id=node_id,
                            source_class_type=ct, depth=depth)

    seen2 = seen | {node_id}
    has_pos, has_neg = is_link(inputs.get("positive")), is_link(inputs.get("negative"))
    order: list[str] = []
    if has_pos and has_neg:
        # Slot 0 -> positive, slot 1 -> negative on dual pass-through nodes.
        order = ["negative", "positive"] if slot >= 1 else ["positive", "negative"]
        order = order[:1]
    elif has_pos:
        order = ["positive"]
    elif has_neg:
        order = ["negative"]
    order += ["conditioning", "cond", "conditioning_1", "conditioning_to", "input"]

    for name in order:
        v = inputs.get(name)
        if is_link(v):
            r = _text_from_conditioning(graph, str(v[0]), int(v[1]), depth=depth + 1, seen=seen2)
            if r.resolved:
                return r
    for name, v in inputs.items():
        if name in order or not is_link(v):
            continue
        r = _text_from_conditioning(graph, str(v[0]), int(v[1]), depth=depth + 1, seen=seen2)
        if r.resolved:
            return r
    for name in _TEXT_INPUT_NAMES:
        if name in inputs:
            r = resolve_value(graph, inputs[name], owner=node_id, _depth=depth)
            if r.resolved and as_text(r):
                return r
    # Unnamed UI widget on a text-encoder node: the first long string wins.
    if "TextEncode" in ct or "TextEncoder" in ct:
        for name, v in inputs.items():
            if name.startswith("widget_") and isinstance(v, str) and v.strip():
                return Resolved(v, "widget", source_node_id=node_id, depth=depth)
    return UNRESOLVED


def _fallback_prompts(graph: dict) -> tuple[Resolved, Resolved]:
    """Document-order heuristic used only when no sampler node exists."""
    texts: list[tuple[str, Resolved]] = []
    for nid in sorted(graph, key=lambda k: (len(k), k)):
        ct = class_type(graph, nid) or ""
        if "TextEncode" not in ct and "Text" not in ct:
            continue
        inputs = node_inputs(graph, nid)
        for name in ("text", "text_g", "prompt", "string", "value"):
            if name in inputs:
                r = resolve_value(graph, inputs[name], owner=nid)
                if as_text(r):
                    texts.append((nid, r))
                break
    pos = texts[0][1] if texts else UNRESOLVED
    neg = UNRESOLVED
    for _nid, r in texts[1:]:
        t = (as_text(r) or "").lower()
        if t and (len(t) < len(as_text(pos) or "") or "worst" in t or "bad" in t):
            neg = r
            break
    if neg is UNRESOLVED and len(texts) > 1:
        neg = texts[1][1]
    return pos, neg


def collect_model_refs(graph: dict) -> tuple[list[dict], list[dict], int]:
    """Return (models, loras, unresolved_count) referenced anywhere in the graph."""
    models: list[dict] = []
    loras: list[dict] = []
    unresolved = 0
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not isinstance(ct, str):
            continue
        inputs = node_inputs(graph, nid)
        mapping = MODEL_LOADER_INPUTS.get(ct)
        lora_map = LORA_LOADER_INPUTS.get(ct)
        for name, raw in inputs.items():
            category: str | None = None
            is_lora = False
            if mapping and name in mapping:
                category = mapping[name]
            elif lora_map is not None and (name in lora_map or name.startswith("lora")):
                category = lora_map.get(name, "loras")
                is_lora = True
            elif isinstance(raw, str) and raw.lower().endswith(MODEL_EXTS):
                # Unnamed UI widgets land here (``widget_0`` etc.); the value
                # still names a real model file, so classify it by context.
                lname = name.lower()
                if "lora" in lname or "lora" in ct.lower():
                    category, is_lora = "loras", True
                else:
                    category = _guess_category(name, ct)
            elif is_link(raw):
                continue
            if category is None:
                continue
            r = resolve_value(graph, raw, owner=nid)
            ref = as_model_name(r)
            if ref is None:
                if r.origin in ("link", "unresolved"):
                    unresolved += 1
                continue
            entry = {
                "ref_name": ref, "category": category, "via_class": ct, "via_input": name,
                "node_id": nid,
            }
            if is_lora:
                strength = None
                for sname in ("strength_model", "strength", "lora_strength", "weight"):
                    if sname in inputs:
                        strength = as_float(resolve_value(graph, inputs[sname], owner=nid))
                        if strength is not None:
                            break
                entry["strength"] = strength
                loras.append(entry)
            else:
                models.append(entry)
        # rgthree Power Lora Loader stores loras in dict-valued widgets.
        if ct.startswith("Power Lora Loader"):
            for name, raw in inputs.items():
                if isinstance(raw, dict) and isinstance(raw.get("lora"), str):
                    loras.append({
                        "ref_name": raw["lora"], "category": "loras", "via_class": ct,
                        "via_input": name, "node_id": nid,
                        "strength": raw.get("strength") if isinstance(
                            raw.get("strength"), (int, float)) else None,
                    })
    return models, loras, unresolved


def _guess_category(input_name: str, ct: str) -> str | None:
    n = input_name.lower()
    c = ct.lower()
    if "vae" in n or "vae" in c:
        return "vae"
    if "ckpt" in n or "checkpoint" in c:
        return "checkpoints"
    if "unet" in n or "unet" in c or "diffusion" in n:
        return "diffusion_models"
    if "clip_vision" in n:
        return "clip_vision"
    if "clip" in n or "text_encoder" in n:
        return "text_encoders"
    if "control" in n or "control" in c:
        return "controlnet"
    if "upscale" in n or "upscale" in c:
        return "upscale_models"
    if "style" in n:
        return "style_models"
    if "ipadapter" in n or "ipadapter" in c:
        return "ipadapter"
    if "model" in n:
        return "checkpoints"
    return None


def graph_hash(graph: dict) -> str:
    """Stable hash of the graph's structure, ignoring seeds and node ordering."""
    parts = []
    for nid in sorted(graph):
        node = graph.get(nid)
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        inputs = node.get("inputs")
        keys = sorted(inputs) if isinstance(inputs, dict) else []
        parts.append(f"{ct}|{','.join(keys)}")
    h = hashlib.blake2b("\n".join(parts).encode("utf-8", "replace"), digest_size=16)
    return h.hexdigest()


def summarize_graph(prompt: Any = None, workflow: Any = None) -> GraphSummary:
    """The only entry point the outputs/workflows phases call."""
    graph = normalize_prompt_graph(prompt)
    if graph is None:
        graph = normalize_prompt_graph(workflow) or ui_graph_to_api(workflow)
    s = GraphSummary()
    if not graph:
        return s

    s.node_count = len(graph)
    types: dict[str, int] = {}
    for node in graph.values():
        ct = node.get("class_type") if isinstance(node, dict) else None
        if isinstance(ct, str):
            types[ct] = types.get(ct, 0) + 1
    s.node_types = sorted(types)
    s.graph_hash = graph_hash(graph)

    prov: dict = {}
    unresolved = 0

    samplers = _find_samplers(graph)
    pos_r: Resolved = UNRESOLVED
    neg_r: Resolved = UNRESOLVED
    sampler_node: str | None = None

    for nid in samplers:
        inputs = node_inputs(graph, nid)
        if "positive" in inputs or "negative" in inputs:
            sampler_node = nid
            pv, nv = inputs.get("positive"), inputs.get("negative")
            if is_link(pv):
                pos_r = _text_from_conditioning(graph, str(pv[0]), int(pv[1]))
            elif isinstance(pv, str):
                pos_r = Resolved(pv, "literal", source_node_id=nid)
            if (is_link(pv) and is_link(nv) and str(pv[0]) == str(nv[0])
                    and int(pv[1]) == int(nv[1])):
                # Both slots consume the very same conditioning output, so this
                # graph has no separate negative prompt at all.
                neg_r = Resolved(None, "same_as_positive", source_node_id=str(nv[0]),
                                 source_class_type=class_type(graph, nv[0]))
            elif is_link(nv):
                neg_r = _text_from_conditioning(graph, str(nv[0]), int(nv[1]))
            elif isinstance(nv, str):
                neg_r = Resolved(nv, "literal", source_node_id=nid)
            if as_text(pos_r) or as_text(neg_r):
                break
    if sampler_node is None and samplers:
        sampler_node = samplers[0]
    if not as_text(pos_r) and not as_text(neg_r):
        fb_pos, fb_neg = _fallback_prompts(graph)
        if as_text(fb_pos):
            pos_r, neg_r = fb_pos, fb_neg

    s.positive_prompt = as_text(pos_r)
    s.negative_prompt = as_text(neg_r)
    if (s.negative_prompt is not None and s.negative_prompt == s.positive_prompt
            and pos_r.source_node_id is not None
            and pos_r.source_node_id == neg_r.source_node_id):
        # Same text from the same node is a duplicate, not a negative prompt.
        # An honest NULL beats showing the user a wrong negative.
        neg_r = Resolved(None, "same_as_positive",
                         source_node_id=neg_r.source_node_id,
                         source_class_type=neg_r.source_class_type)
        s.negative_prompt = None
    prov["positive_prompt"] = pos_r.as_provenance()
    prov["negative_prompt"] = neg_r.as_provenance()
    for r in (pos_r, neg_r):
        if not r.resolved and r.origin not in DETERMINED_EMPTY_ORIGINS:
            unresolved += 1

    if sampler_node is not None:
        inputs = node_inputs(graph, sampler_node)
        field_map = (
            ("seed", ("seed", "noise_seed", "rand_seed"), as_seed),
            ("steps", ("steps", "num_steps"), as_int),
            ("cfg", ("cfg", "cfg_scale", "guidance"), as_float),
            ("denoise", ("denoise",), as_float),
            ("sampler", ("sampler_name", "sampler"), as_text),
            ("scheduler", ("scheduler",), as_text),
        )
        for attr, names, conv in field_map:
            for name in names:
                if name not in inputs:
                    continue
                r = resolve_value(graph, inputs[name], owner=sampler_node)
                val = conv(r)
                if val is not None:
                    setattr(s, attr, val)
                    prov[attr] = r.as_provenance()
                    break
                if not r.resolved:
                    prov[attr] = r.as_provenance()
                    unresolved += 1
                    break

    # Latent dimensions.
    for nid, node in graph.items():
        ct = node.get("class_type") if isinstance(node, dict) else None
        if not isinstance(ct, str) or "Latent" not in ct:
            continue
        inputs = node_inputs(graph, nid)
        if "width" in inputs and "height" in inputs:
            w = as_int(resolve_value(graph, inputs["width"], owner=nid))
            h = as_int(resolve_value(graph, inputs["height"], owner=nid))
            if w and h:
                s.width, s.height = w, h
                break

    s.models, s.loras, model_unresolved = collect_model_refs(graph)
    unresolved += model_unresolved
    if s.models:
        order = {"checkpoints": 0, "diffusion_models": 1, "unet": 2}
        best = min(s.models, key=lambda m: order.get(m.get("category") or "", 9))
        s.primary_model = best["ref_name"]
    s.unresolved_count = unresolved
    s.provenance = prov
    return s
