"""Where a fetched file goes - derived, never supplied (SECURITY_REVIEW R3).

The destination of every download is computed here from two things the server
already knows: the **node input name** that referenced the file, and the set of
configured roots.  A client cannot influence it, and neither can the source: a
``Content-Disposition`` filename is treated as a hint that must survive the same
validation as everything else, and it is only ever used for display.

The map below is frozen on purpose.  ComfyUI resolves a loader's file list from
a folder chosen by ``folder_paths``; ``ckpt_name`` means ``models/checkpoints``
and nothing else, so a table is the correct shape and a heuristic is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..core import config_service
from ..core.errors import PathNotAllowed, ValidationError
from ..core.pathsafe import Root, normalize, resolve_within_roots, validate_filename

#: Node input name -> ComfyUI model folder.  REQUIREMENTS_R2 C9.3.
#: ``graph_utils.MODEL_LOADER_INPUTS`` maps *(class, input)*; this maps the input
#: name alone, so a loader class the vault has never seen still lands correctly
#: as long as it uses ComfyUI's conventional input names.
INPUT_CATEGORY: dict[str, str] = {
    "ckpt_name": "checkpoints",
    "config_name": "configs",
    "lora_name": "loras",
    "lora": "loras",
    "lora_0": "loras",
    "lora_1": "loras",
    "lora_2": "loras",
    "lora_3": "loras",
    "unet_name": "diffusion_models",
    "vae_name": "vae",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name3": "text_encoders",
    "clip_name4": "text_encoders",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
    "gligen_name": "gligen",
    "hypernetwork_name": "hypernetworks",
    "photomaker_model_name": "photomaker",
    "ipadapter_file": "ipadapter",
    "instantid_file": "instantid",
    "audio_encoder_name": "audio_encoders",
    "upscale_model_name": "upscale_models",
    "embedding_name": "embeddings",
    "model_path": "diffusers",
    "sam_model_name": "sams",
}

#: ``CLIPVisionLoader`` also calls its input ``clip_name`` but means
#: ``clip_vision``, and ``UpscaleModelLoader`` calls its input ``model_name``.
#: Where the class is known, the class wins - which is why the class table is
#: consulted first.
_AMBIGUOUS_INPUTS = frozenset({"model_name", "model", "name", "clip_name"})

#: Every category the server is willing to write into.  A category outside this
#: set is reported, never fetched - it would mean inventing a folder inside the
#: owner's install from data that came off the internet.
ALLOWED_CATEGORIES: frozenset[str] = frozenset(config_service.MODEL_CATEGORY_DIRS)

#: Extensions a model download may carry.  Anything else is refused before a
#: socket is opened: the fetcher exists to place weights, not executables.
ALLOWED_MODEL_EXTS: frozenset[str] = frozenset({
    ".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx",
    ".npz", ".yaml", ".json", ".pkl", ".engine",
})

MAX_FILENAME_LEN = 180


@dataclass(frozen=True)
class Destination:
    category: str
    root: Root
    directory: str
    abs_path: str
    filename: str

    def as_dict(self) -> dict:
        return {"category": self.category, "root_id": self.root.id,
                "root_label": self.root.label, "directory": self.directory,
                "abs_path": self.abs_path, "filename": self.filename}


def category_for(via_class: str | None, via_input: str | None,
                 stored: str | None = None) -> str | None:
    """Resolve the ComfyUI model folder for one dependency reference.

    Order: the *(class, input)* table the parser already uses, then the frozen
    input-name table, then whatever the indexer stored.  Never a guess from the
    filename - a name is attacker-influenced, an input name is not.
    """
    from ..parsers import graph_utils

    cls = str(via_class or "")
    inp = str(via_input or "")
    for table in (graph_utils.MODEL_LOADER_INPUTS, graph_utils.LORA_LOADER_INPUTS):
        entry = table.get(cls)
        if entry and inp in entry:
            return entry[inp]
    if inp and inp not in _AMBIGUOUS_INPUTS:
        hit = INPUT_CATEGORY.get(inp)
        if hit:
            return hit
    if inp in _AMBIGUOUS_INPUTS and INPUT_CATEGORY.get(inp) and not stored:
        return INPUT_CATEGORY[inp]
    if stored and str(stored) in ALLOWED_CATEGORIES:
        return str(stored)
    return None


def safe_basename(name: str | None, *, source: str = "reference") -> str:
    """Validate a filename that came from a workflow, an API, or a header.

    Every rejection is a hard refusal, never a sanitising rewrite: silently
    "fixing" ``../../../../Windows/System32/evil.dll`` into ``evil.dll`` would
    place a file the user never agreed to.
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValidationError(f"The {source} has no filename.")
    if "\x00" in raw:
        raise ValidationError(f"The {source} filename contains a NUL byte.")
    # A reference may legitimately carry a sub-folder ("SDXL/foo.safetensors").
    # The sub-folder is dropped, not walked: only the last component is used,
    # and it then has to pass the same validation a rename does.
    flattened = raw.replace("\\", "/")
    if flattened != raw and ":" in raw:
        raise ValidationError(f"The {source} filename is a drive-qualified path.")
    last = flattened.rsplit("/", 1)[-1]
    if last != raw and (raw.startswith("/") or ".." in flattened.split("/")):
        raise ValidationError(f"The {source} filename tries to leave its folder.")
    if len(last) > MAX_FILENAME_LEN:
        raise ValidationError(
            f"The {source} filename is longer than {MAX_FILENAME_LEN} characters.")
    validate_filename(last)
    ext = os.path.splitext(last)[1].lower()
    if ext not in ALLOWED_MODEL_EXTS:
        raise ValidationError(
            f"'{ext or last}' is not a model file extension this app will download.",
            details={"allowed": sorted(ALLOWED_MODEL_EXTS)})
    return last


def resolve_destination(category: str, filename: str, *, cfg=None) -> Destination:
    """Derive the absolute target and prove it lands inside a configured root.

    Called **before the first byte is written**, and again for the ``.part``
    file, so nothing is ever created outside ``resolve_within_roots``.
    """
    cfg = cfg or config_service.get_config()
    cat = str(category or "").strip()
    if cat not in ALLOWED_CATEGORIES:
        raise ValidationError(
            f"'{cat or '(unknown)'}' is not a ComfyUI model folder this app writes to.",
            details={"category": cat})
    clean = safe_basename(filename)

    directory: Path | None = None
    owner: Root | None = None
    for name, path, root in config_service.model_dirs(cfg):
        if name.lower() == cat.lower():
            directory, owner = Path(path), root
            break
    if directory is None:
        comfy = next((r for r in cfg.roots if r.kind == "comfyui"), None)
        if comfy is None:
            raise PathNotAllowed(
                "No ComfyUI root is configured, so there is nowhere to put a model.",
                details={"category": cat})
        directory, owner = Path(comfy.path) / "models" / cat, comfy

    target = normalize(directory / clean)
    resolved, root = resolve_within_roots(target, cfg.roots)
    if not str(resolved).lower().endswith(clean.lower()):
        raise PathNotAllowed(
            "The derived destination does not end in the validated filename.",
            details={"category": cat, "filename": clean})
    return Destination(category=cat, root=owner or root,
                       directory=str(normalize(directory)),
                       abs_path=str(resolved), filename=clean)


def custom_nodes_destination(folder_name: str, *, cfg=None) -> Destination:
    """Where a node package is cloned: ``<comfyui>/custom_nodes/<folder>``."""
    cfg = cfg or config_service.get_config()
    clean = str(folder_name or "").strip()
    validate_filename(clean)
    if len(clean) > MAX_FILENAME_LEN:
        raise ValidationError("The package folder name is too long.")
    comfy = next((r for r in cfg.roots if r.kind == "comfyui"), None)
    if comfy is None:
        raise PathNotAllowed("No ComfyUI root is configured.",
                             details={"folder": clean})
    base = Path(comfy.path) / "custom_nodes"
    resolved, root = resolve_within_roots(normalize(base / clean), cfg.roots)
    return Destination(category="custom_nodes", root=root, directory=str(normalize(base)),
                       abs_path=str(resolved), filename=clean)


def content_disposition_hint(header: str | None) -> str | None:
    """R3: a server-supplied filename is a *hint*, and is validated identically.

    Returns ``None`` when the header is absent or does not survive validation -
    the derived name is used instead.  It is never used to choose a directory.
    """
    raw = str(header or "")
    if not raw:
        return None
    candidate: str | None = None
    for part in raw.split(";"):
        token = part.strip()
        lowered = token.lower()
        if lowered.startswith("filename*="):
            value = token.split("=", 1)[1].strip()
            candidate = value.split("''", 1)[-1]
            break
        if lowered.startswith("filename="):
            candidate = token.split("=", 1)[1].strip().strip('"')
    if not candidate:
        return None
    try:
        from urllib.parse import unquote

        return safe_basename(unquote(candidate), source="server-supplied")
    except ValidationError:
        return None
