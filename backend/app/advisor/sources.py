"""Where catalog data comes from, and one normaliser per source.

Six sources, all public, all read-only JSON:

``huggingface``   the Hub API - model facts, exact file sizes, licence, the
                  host's own scan status.  The backbone of the catalog.
``civitai``       community checkpoints, LoRAs, embeddings and their scan results.
``comfy_manager`` the ComfyUI-Manager model list: curated, ComfyUI-ready files
                  with the folder each one belongs in.
``openmodeldb``   the upscaler database.
``ollama``        the Ollama library - language models by pull name.
``github``        repository search, kept as *leads*: a new project worth a look
                  that is not yet a catalog entry.

Outbound rules, the whole security boundary of this module:

* every URL is built here from constants plus identifiers that passed
  :func:`valid_repo_id` - no caller ever supplies a URL;
* every URL is checked against ``enable.hosts`` (``KIND_CATALOG``) before the
  request, and every redirect hop again before it is followed;
* requests carry **no credentials and nothing about this machine** - no API
  key, no hardware profile, no library contents;
* responses are size-capped JSON, parsed as data.  Nothing fetched is ever
  executed, imported or written anywhere but the catalog file.

The normalisers are pure functions over already-fetched JSON, so the mapping
from each source's shape to a catalog entry is tested without a network.
"""

from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import quote

from .. import config as buildcfg
from ..core import db as dbmod
from ..core.errors import UpstreamUnavailable
from ..enable import hosts

log = logging.getLogger("vault.advisor.sources")

USER_AGENT = f"GeekatplayAssetVault/{buildcfg.VERSION} (catalog)"
TIMEOUT_S = 20.0
MAX_JSON_BYTES = 12 * 1024 * 1024

HF_BASE = "https://huggingface.co"
CIVITAI_BASE = "https://civitai.com"
GITHUB_API = "https://api.github.com"
COMFY_MANAGER_LIST = ("https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager"
                      "/main/model-list.json")
OPENMODELDB_LIST = "https://openmodeldb.info/api/v1/models.json"
OLLAMA_TAGS = "https://ollama.com/api/tags"
#: This app's own source list, so pins and queries can improve between releases.
#: Pinned to the project repository; not configurable.
VAULT_SOURCES = ("https://raw.githubusercontent.com/GeekatplayStudio/"
                 "ComfyUI-Asset-Vault/main/backend/app/advisor/data/sources.json")

MIB = 1024 ** 2
GIB = 1024 ** 3

WEIGHT_EXTS = ("safetensors", "sft", "gguf", "ckpt", "pt", "pth", "bin")
MIN_WEIGHT_BYTES = 2 * MIB
MAX_VARIANTS = 40
MAX_COMPANIONS = 12
MAX_TAGS = 14
MAX_DESCRIPTION = 420

_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_SHARD = re.compile(r"[-_.]0*(\d+)[-_]of[-_]0*(\d+)", re.IGNORECASE)
_PRECISION = re.compile(
    r"(?<![a-z0-9])("
    r"q\d+_k_[sml]|q\d+_k|q\d+_[01]|iq\d+_[a-z]+|q\d+|"
    r"fp8[_-]?scaled|fp8[_-]?e4m3fn|fp16|fp32|fp8|bf16|f16|f32|"
    r"int8[a-z_]*|int4|nvfp4|nf4|bnb[-_]?4bit|4bit|8bit|svdq[a-z0-9_-]*"
    r")(?![a-z0-9])", re.IGNORECASE)

HF_KIND_BY_TASK = {
    "text-to-image": "image", "image-to-image": "image",
    "unconditional-image-generation": "image", "image-text-to-image": "image",
    "text-to-video": "video", "image-to-video": "video",
    "video-to-video": "video", "image-text-to-video": "video",
    "text-generation": "llm", "image-text-to-text": "llm", "any-to-any": "llm",
    "text-to-speech": "audio", "text-to-audio": "audio", "audio-to-audio": "audio",
    "automatic-speech-recognition": "audio",
    "text-to-3d": "3d", "image-to-3d": "3d",
    "image-feature-extraction": "vision", "zero-shot-image-classification": "vision",
}

SAVE_PATH_BY_ROLE = {
    "main": "models/diffusion_models", "checkpoint": "models/checkpoints",
    "text_encoder": "models/text_encoders", "vae": "models/vae",
    "vision": "models/clip_vision", "lora": "models/loras",
    "controlnet": "models/controlnet", "upscale": "models/upscale_models",
    "embedding": "models/embeddings", "gguf_unet": "models/unet",
}

BUNDLED_PIPELINES = ("StableDiffusionPipeline", "StableDiffusionXLPipeline")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def valid_repo_id(value: object) -> bool:
    """``owner/name`` in the Hub's own alphabet - nothing that can alter a URL."""
    text = str(value or "")
    # fullmatch, not match-with-$: "$" also matches before a trailing newline,
    # and an id is spliced into a URL.
    return bool(_REPO_ID.fullmatch(text)) and ".." not in text


def fetch_json(url: str, *, client=None, max_bytes: int = MAX_JSON_BYTES,
               accept_statuses: tuple[int, ...] = ()) -> tuple[int, object]:
    """GET one allowlisted URL and return ``(status, parsed_json)``.

    Raises :class:`UpstreamUnavailable` on a network failure, an oversized or
    non-JSON body, or a redirect that leaves the allowlist.  A status listed in
    ``accept_statuses`` is returned with ``None`` instead of raising.
    """
    import httpx

    checked = hosts.check(url, kind=hosts.KIND_CATALOG)
    owns = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT,
                                             "Accept": "application/json"})
    try:
        for hop in range(hosts.MAX_REDIRECTS + 1):
            try:
                with client.stream("GET", checked.url) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        checked = hosts.check_redirect(
                            response.headers.get("location"), current=checked, hop=hop)
                        continue
                    if status in accept_statuses:
                        return status, None
                    if status >= 400:
                        raise UpstreamUnavailable(
                            f"{checked.host} answered {status}.",
                            details={"host": checked.host, "status": status})
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise UpstreamUnavailable(
                                f"{checked.host} sent more than "
                                f"{max_bytes // MIB} MB; refused.",
                                details={"host": checked.host})
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(
                    f"Could not reach {checked.host}.",
                    details={"host": checked.host, "error": type(exc).__name__}) from exc
            try:
                return status, json.loads(b"".join(chunks).decode("utf-8", "replace"))
            except ValueError as exc:
                raise UpstreamUnavailable(
                    f"{checked.host} did not return JSON.",
                    details={"host": checked.host}) from exc
        raise UpstreamUnavailable("Too many redirects.", details={"host": checked.host})
    finally:
        if owns:
            client.close()


def head_status(url: str, *, client=None) -> dict:
    """Is a download link alive?  One HEAD, no body, no redirect followed.

    Only ever called with a URL taken from a stored catalog entry, and only
    for hosts on the *model* allowlist.  A 3xx to the host's CDN counts as
    alive; the redirect target is deliberately not requested.
    """
    import httpx

    try:
        checked = hosts.check(url, kind=hosts.KIND_MODEL)
    except hosts.HostNotAllowed as exc:
        return {"state": "unchecked", "status": None,
                "message": f"This host is not checked automatically ({exc.reason})."}
    owns = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT})
    try:
        try:
            response = client.head(checked.url)
        except httpx.HTTPError as exc:
            return {"state": "unreachable", "status": None,
                    "message": f"Could not reach {checked.host} ({type(exc).__name__})."}
        status = response.status_code
        size = response.headers.get("x-linked-size") or (
            response.headers.get("content-length") if status < 300 else None)
        bytes_ = int(size) if size and str(size).isdigit() else None
        if status < 400:
            return {"state": "ok", "status": status, "bytes": bytes_,
                    "message": "The link is live."}
        if status in (401, 403):
            return {"state": "gated", "status": status,
                    "message": "The link is live but needs a signed-in account "
                               "or an accepted licence on the host."}
        if status == 404:
            return {"state": "dead", "status": status,
                    "message": "The host says this file no longer exists."}
        if status == 429:
            return {"state": "unreachable", "status": status,
                    "message": "The host is rate-limiting requests; try again later."}
        return {"state": "unreachable", "status": status,
                "message": f"{checked.host} answered {status}."}
    finally:
        if owns:
            client.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def clean_text(value: object, limit: int = MAX_DESCRIPTION) -> str | None:
    """Plain text from an HTML or markdown blob, truncated on a word."""
    text = str(value or "")
    if not text.strip():
        return None
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)  # "<b>small</b>." -> "small."
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "..."
    return text or None


def parse_size(value: object) -> int | None:
    """``"4.71MB"`` / ``"23.8GB"`` / ``335304388`` -> bytes."""
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    match = re.match(r"\s*([\d.]+)\s*([KMGT]?)i?B?\s*$", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    scale = {"": 1, "K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}[match.group(2).upper()]
    try:
        return int(float(match.group(1)) * scale)
    except ValueError:
        return None


def precision_of(filename: str) -> str | None:
    """The quantisation or precision token in a filename, normalised."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = _SHARD.sub("", stem)
    found = _PRECISION.findall(stem)
    if not found:
        return None
    token = found[-1]  # the last token is the specific one
    token = token.lower().replace("-", "_")
    return token.upper() if token.startswith(("q", "iq", "f1", "f3")) else token


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def file_role(path: str) -> str:
    """What a weight file is, from its path inside a repository."""
    low = path.lower()
    parts = low.split("/")
    name = parts[-1]
    folders = parts[:-1]
    joined = "/".join(folders)
    if "mmproj" in name:
        return "projector"
    if any(f in ("text_encoder", "text_encoders", "text_encoder_2", "text_encoder_3",
                 "clip", "llm", "t5", "tokenizer") or f.startswith("text_encoder")
           for f in folders):
        return "text_encoder"
    if any(f in ("clip_vision", "image_encoder", "image_encoders") for f in folders):
        return "vision"
    if any(f in ("vae", "vae_approx", "vae_decoder", "vae_encoder") for f in folders) \
            or re.search(r"(^|[_-])(vae|ae)([_.-]|$)", name):
        return "vae"
    if "loras" in folders or "lora" in folders or "lora" in name:
        return "lora"
    if "controlnet" in joined or "controlnet" in name:
        return "controlnet"
    if any(f in ("upscale_models", "upscalers") for f in folders):
        return "upscale"
    if re.search(r"(^|[_-])(t5|umt5|clip_[lg]|clip-[lg]|t5xxl|qwen[\d._]*[a-z_]*vl)", name) \
            and not any(f in ("diffusion_models", "transformer", "unet") for f in folders):
        return "text_encoder"
    if any(f in ("checkpoints",) for f in folders):
        return "checkpoint"
    return "main"


def _is_noise(path: str, size: int) -> bool:
    name = path.lower().rsplit("/", 1)[-1]
    if _ext(path) not in WEIGHT_EXTS or size < MIN_WEIGHT_BYTES:
        return True
    return any(token in name for token in (
        "optimizer", "training_args", "scheduler", "tokenizer", "rng_state",
        "scaler", "trainer_state", "events.out", "openvino", "tf_model",
        "flax_model", "onnx"))


_KIND_HINTS = (
    ("video", ("video", "wan", "ltx", "animate", "i2v", "t2v", "minimax-h3", "pan2",
               "mochi", "cogvideo", "svd", "longcat", "framepack")),
    ("audio", ("audio", "music", "tts", "voice", "speech", "ace-step", "whisper",
               "kokoro", "sound", "roformer")),
    ("3d", ("3d", "trellis", "mesh", "tripo", "idol", "splat")),
    ("upscale", ("upscal", "esrgan", "super-resolution", "seedvr")),
    ("vision", ("birefnet", "moge", "depth", "segment", "sam", "clip-vit",
                "siglip", "florence", "dino", "pose", "matting", "detect")),
    ("image", ("image", "flux", "stable-diffusion", "sdxl", "qwen-image", "krea",
               "z-image", "hidream", "chroma", "lumina", "fibo", "kolors", "pixart")),
)


def guess_kind(repo: str, tags: list[str], paths: list[str]) -> str:
    """A type for a repository that declares no task, from its name and files."""
    haystack = (repo.split("/", 1)[-1] + " " + " ".join(tags)).lower()
    for kind, needles in _KIND_HINTS:
        if any(n in haystack for n in needles):
            return kind
    joined = " ".join(paths).lower()
    if "diffusion_models/" in joined or "checkpoints/" in joined:
        return "image"
    return "other"


def _group_shards(files: list[dict]) -> list[dict]:
    """Merge ``model-00001-of-00003`` shards into one logical file."""
    groups: dict[str, dict] = {}
    for item in files:
        key = _SHARD.sub("", item["path"])
        group = groups.get(key)
        if group is None:
            groups[key] = {"path": key, "first": item["path"], "bytes": item["bytes"],
                           "parts": 1, "sha256": item.get("sha256"),
                           "role": item["role"]}
        else:
            group["bytes"] += item["bytes"]
            group["parts"] += 1
            group["sha256"] = None
            if item["path"] < group["first"]:
                group["first"] = item["path"]
    return list(groups.values())


def hf_resolve_url(repo: str, path: str) -> str:
    return f"{HF_BASE}/{repo}/resolve/main/{quote(path)}"


def _label_for(path: str, precision: str | None) -> str:
    stem = _SHARD.sub("", path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    del precision  # the stem already carries it; kept for call-site clarity
    return stem if len(stem) <= 56 else stem[:53] + "..."


# ---------------------------------------------------------------------------
# Hugging Face
# ---------------------------------------------------------------------------

def hf_kind(detail: dict, hint_kind: str | None = None) -> str:
    tags = {str(t).lower() for t in detail.get("tags") or []}
    task = str(detail.get("pipeline_tag") or "")
    if any(t.startswith("base_model:adapter:") for t in tags) or "lora" in tags:
        return "lora"
    if "controlnet" in tags:
        return "controlnet"
    if hint_kind:
        return hint_kind
    return HF_KIND_BY_TASK.get(task, "other")


def hf_base_model(detail: dict) -> str | None:
    """The upstream repository a quantised or fine-tuned repo was made from."""
    for tag in detail.get("tags") or []:
        text = str(tag)
        if text.startswith("base_model:") and text.count(":") == 1:
            candidate = text.split(":", 1)[1]
            if valid_repo_id(candidate):
                return candidate
    for tag in detail.get("tags") or []:
        text = str(tag)
        if text.startswith("base_model:"):
            candidate = text.rsplit(":", 1)[-1]
            if valid_repo_id(candidate):
                return candidate
    return None


def hf_scan(detail: dict) -> tuple[str, str | None]:
    status = detail.get("securityRepoStatus")
    if not isinstance(status, dict):
        return "unknown", None
    issues = [i for i in status.get("filesWithIssues") or []
              if not isinstance(i, dict)
              or str(i.get("level") or "unsafe").lower() in ("unsafe", "suspicious")]
    if issues:
        names = [str(i.get("path")) for i in issues if isinstance(i, dict)][:3]
        return "issues", ("Hugging Face flagged " + (", ".join(names) or
                                                    f"{len(issues)} file(s)") + ".")
    return ("clean" if status.get("scansDone") else "pending"), None


def normalize_hf(detail: dict, *, hint: dict | None = None,
                 trust: dict | None = None) -> dict | None:
    """One Hub model document (``?blobs=true``) -> one catalog entry, or ``None``."""
    hint = hint or {}
    repo = str(detail.get("id") or detail.get("modelId") or "")
    if not valid_repo_id(repo) or detail.get("private") or detail.get("disabled"):
        return None
    author = repo.split("/", 1)[0]
    card = detail.get("cardData") if isinstance(detail.get("cardData"), dict) else {}
    tags = [str(t) for t in detail.get("tags") or []]
    low_tags = {t.lower() for t in tags}

    raw_files = []
    code_files = 0
    for sibling in detail.get("siblings") or []:
        path = str(sibling.get("rfilename") or "")
        if path.endswith(".py"):
            code_files += 1
        size = sibling.get("size")
        lfs = sibling.get("lfs") if isinstance(sibling.get("lfs"), dict) else {}
        size = int(lfs.get("size") or size or 0)
        if not path or _is_noise(path, size):
            continue
        raw_files.append({"path": path, "bytes": size, "sha256": lfs.get("sha256"),
                          "role": file_role(path)})
    if not raw_files:
        return None

    kind = hf_kind(detail, hint.get("kind"))
    if kind == "other":
        kind = guess_kind(repo, tags, [f["path"] for f in raw_files])
    is_gguf_repo = any(_ext(f["path"]) == "gguf" for f in raw_files)
    diffusers_cfg = (detail.get("config") or {}).get("diffusers") \
        if isinstance(detail.get("config"), dict) else None
    pipeline_class = str((diffusers_cfg or {}).get("_class_name") or "")
    bundled_repo = pipeline_class in BUNDLED_PIPELINES

    grouped = _group_shards(raw_files)
    mains: list[dict] = []
    companions: list[dict] = []
    for group in grouped:
        role = group["role"]
        top_level = "/" not in group["path"]
        if kind == "lora" or (kind == "llm" and role not in ("projector",)):
            role = "main"
        elif role == "checkpoint":
            role = "main"
            group["bundled"] = True
        elif role == "main" and top_level and bundled_repo:
            group["bundled"] = True
        if role == "main":
            mains.append(group)
        else:
            group["role"] = role
            companions.append(group)

    if kind == "llm" and is_gguf_repo:
        mains = [m for m in mains if _ext(m["first"]) == "gguf"]
    if not mains:
        # A repository of encoders or VAEs only: its files are the product.
        mains, companions = companions, []
        if mains:
            kind = {"text_encoder": "text_encoder", "vae": "vae",
                    "vision": "vision", "upscale": "upscale", "lora": "lora",
                    "controlnet": "controlnet"}.get(mains[0]["role"], kind)
    if not mains:
        return None
    # A pickle twin of a file that also ships as safetensors adds risk and no
    # value: list the data-only one.
    safe_stems = {m["path"].rsplit(".", 1)[0] for m in mains
                  if _ext(m["first"]) in ("safetensors", "sft")}
    mains = [m for m in mains if _ext(m["first"]) in ("safetensors", "sft", "gguf")
             or m["path"].rsplit(".", 1)[0] not in safe_stems]
    companions = [c for c in companions
                  if _ext(c["first"]) in ("safetensors", "sft", "gguf")
                  or not any(_ext(o["first"]) in ("safetensors", "sft")
                             and o["path"].rsplit(".", 1)[0] == c["path"].rsplit(".", 1)[0]
                             for o in companions)]
    # Helper weights that sit beside a large model (a duration head, a tiny
    # decoder) are not variants of it.
    largest = max(m["bytes"] for m in mains)
    if kind in ("image", "video", "3d", "audio") and largest >= 2 * GIB:
        mains = [m for m in mains if m["bytes"] >= max(256 * MIB, largest * 0.04)]
    elif kind == "llm":
        # Importance matrices and auxiliary heads ship beside the quantised
        # weights; they are not something to load as the model.
        mains = [m for m in mains if "imatrix" not in m["path"].lower()
                 and m["bytes"] >= largest * 0.08]

    safetensors = detail.get("safetensors") if isinstance(detail.get("safetensors"), dict) else {}
    gguf = detail.get("gguf") if isinstance(detail.get("gguf"), dict) else {}
    repo_precisions = [str(k).lower() for k in (safetensors.get("parameters") or {})]

    if kind == "llm":
        runtime = "gguf" if is_gguf_repo else "transformers"
    elif kind in ("image", "video") and is_gguf_repo:
        runtime = "comfyui-gguf"
    elif any(f in g["path"] for g in mains + companions
             for f in ("split_files/", "diffusion_models/", "text_encoders/")):
        runtime = "comfyui"
    elif diffusers_cfg or any(g["path"].startswith(("transformer/", "unet/")) for g in mains):
        runtime = "diffusers"
    else:
        runtime = "comfyui" if kind in ("image", "video", "lora", "controlnet",
                                        "vae", "upscale", "text_encoder") else "other"

    variants = []
    for group in sorted(mains, key=lambda g: g["bytes"], reverse=True)[:MAX_VARIANTS]:
        ext = _ext(group["first"])
        precision = precision_of(group["path"])
        folder = group["path"].rsplit("/", 1)[0] if "/" in group["path"] else ""
        diffusers_dir = folder in ("transformer", "unet") and runtime == "diffusers"
        if precision is None and diffusers_dir and len(repo_precisions) == 1:
            precision = repo_precisions[0]
        label = ("Diffusers " + (precision or "weights")) if diffusers_dir \
            else _label_for(group["path"], precision)
        if kind == "lora":
            save_role = "lora"
        elif group.get("bundled"):
            save_role = "checkpoint"
        elif ext == "gguf" and kind in ("image", "video"):
            save_role = "gguf_unet"
        elif kind in SAVE_PATH_BY_ROLE:
            save_role = kind
        else:
            save_role = "main"
        variants.append({
            "id": group["path"],
            "label": label,
            "precision": precision,
            "format": ext,
            "bytes": int(group["bytes"]),
            "parts": group["parts"],
            "file": group["first"],
            "download_url": hf_resolve_url(repo, group["first"]) if group["parts"] == 1
            else f"{HF_BASE}/{repo}/tree/main/{quote(folder)}".rstrip("/"),
            "direct": group["parts"] == 1,
            "sha256": group.get("sha256"),
            "bundled": bool(group.get("bundled")),
            "save_path": SAVE_PATH_BY_ROLE.get(save_role)
            if kind not in ("llm", "audio", "3d", "other") else None,
        })

    companion_rows = []
    for group in sorted(companions, key=lambda g: (g["role"], g["bytes"]))[:MAX_COMPANIONS]:
        if group["role"] in ("lora", "controlnet", "upscale"):
            continue  # optional extras, not something the model needs to run
        companion_rows.append({
            "role": group["role"],
            "label": _label_for(group["path"], precision_of(group["path"])),
            "precision": precision_of(group["path"]),
            "format": _ext(group["first"]),
            "bytes": int(group["bytes"]),
            "download_url": hf_resolve_url(repo, group["first"]) if group["parts"] == 1
            else f"{HF_BASE}/{repo}/tree/main",
            "save_path": SAVE_PATH_BY_ROLE.get(group["role"]),
        })

    scan, scan_detail = hf_scan(detail)
    trust = trust or {}
    if author in (trust.get("official") or ()):
        publisher_trust = "official"
    elif author in (trust.get("trusted") or ()):
        publisher_trust = "trusted"
    else:
        publisher_trust = "unknown"

    licence = card.get("license") or next(
        (t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
    if licence == "other" and card.get("license_name"):
        licence = str(card["license_name"])
    base_model = hf_base_model(detail)
    task = str(detail.get("pipeline_tag") or "") or None
    name = str(hint.get("name") or repo.split("/", 1)[1])
    params = safetensors.get("total") or gguf.get("total") or hint.get("params")

    description = hint.get("note") or clean_text(card.get("description"))
    if not description:
        bits = [f"{(task or kind).replace('-', ' ').capitalize()} model published by {author}."]
        if base_model and base_model != repo:
            bits.append(f"Derived from {base_model}.")
        if gguf.get("context_length"):
            bits.append(f"Context length {int(gguf['context_length']):,} tokens.")
        description = " ".join(bits)

    keep_tags = [t for t in tags if ":" not in t and t.lower() not in
                 ("region:us", "safetensors", "endpoints_compatible")][:MAX_TAGS]
    return {
        "id": f"hf:{repo}",
        "source": "huggingface",
        "name": name,
        "publisher": author,
        "kind": kind,
        "task": task,
        "family": hint.get("family") or (base_model.split("/", 1)[1] if base_model else None),
        "base_model": base_model,
        "description": description,
        "url": f"{HF_BASE}/{repo}",
        "license": str(licence) if licence else None,
        "gated": bool(detail.get("gated")),
        "downloads": detail.get("downloads"),
        "likes": detail.get("likes"),
        "trending": detail.get("trendingScore"),
        "created_at": detail.get("createdAt"),
        "updated_at": detail.get("lastModified"),
        "revision": detail.get("sha"),
        "tags": keep_tags,
        "params": int(params) if params else None,
        "context_length": gguf.get("context_length"),
        "architecture": gguf.get("architecture") or pipeline_class or None,
        "runtime": runtime,
        "variants": variants,
        "companions": companion_rows,
        "safety": {"scan": scan, "scan_detail": scan_detail,
                   "publisher_trust": publisher_trust,
                   "code_files": code_files,
                   "custom_code": "custom_code" in low_tags,
                   "hash_published": any(v.get("sha256") for v in variants)},
        "nsfw": "not-for-all-audiences" in low_tags,
        "curated": bool(hint.get("curated")),
        "quality": hint.get("quality"),
        "fetched_at": dbmod.now_ms(),
    }


def hf_list_url(query: dict) -> str:
    """A Hub listing URL from one discovery query (constants and enums only)."""
    params = []
    for key in ("pipeline_tag", "filter", "author", "library", "search"):
        value = query.get(key)
        if value and re.fullmatch(r"[A-Za-z0-9._ -]{1,64}", str(value)):
            params.append(f"{key}={quote(str(value))}")
    sort = query.get("sort") if query.get("sort") in (
        "downloads", "likes", "trendingScore", "lastModified", "createdAt") else "downloads"
    limit = max(1, min(60, int(query.get("limit") or 20)))
    params += [f"sort={sort}", "direction=-1", f"limit={limit}"]
    return f"{HF_BASE}/api/models?" + "&".join(params)


def hf_detail_url(repo: str) -> str:
    if not valid_repo_id(repo):
        raise ValueError(f"not a repository id: {repo!r}")
    return f"{HF_BASE}/api/models/{repo}?blobs=true&securityStatus=true"


# ---------------------------------------------------------------------------
# Civitai
# ---------------------------------------------------------------------------

CIVITAI_KIND = {
    "checkpoint": "image", "lora": "lora", "locon": "lora", "dora": "lora",
    "lycoris": "lora", "textualinversion": "embedding", "controlnet": "controlnet",
    "upscaler": "upscale", "vae": "vae", "motionmodule": "video",
}
CIVITAI_TYPES = ("Checkpoint", "LORA", "LoCon", "DoRA", "TextualInversion",
                 "Controlnet", "Upscaler", "VAE", "MotionModule")
_VIDEO_BASES = ("video", "wan", "ltx", "hunyuan", "mochi", "cogvideo", "svd")


def civitai_list_url(query: dict) -> str:
    kind_type = query.get("types") if query.get("types") in CIVITAI_TYPES else "LORA"
    sort = query.get("sort") if query.get("sort") in (
        "Most Downloaded", "Highest Rated", "Newest") else "Most Downloaded"
    period = query.get("period") if query.get("period") in (
        "AllTime", "Year", "Month", "Week", "Day") else "Month"
    limit = max(1, min(100, int(query.get("limit") or 30)))
    return (f"{CIVITAI_BASE}/api/v1/models?limit={limit}&types={kind_type}"
            f"&sort={quote(sort)}&period={period}&nsfw=false")


def _civitai_scan(files: list[dict]) -> tuple[str, str | None]:
    results = [str(f.get(k) or "") for f in files
               for k in ("pickleScanResult", "virusScanResult")]
    if any(r.lower() == "danger" for r in results):
        return "issues", "Civitai's scanner flagged a file in this version."
    if results and all(r.lower() == "success" for r in results):
        return "clean", None
    return ("pending" if results else "unknown"), None


def normalize_civitai(model: dict) -> dict | None:
    """One Civitai model document -> one catalog entry, or ``None``."""
    model_id = model.get("id")
    if not isinstance(model_id, int) or model.get("nsfw") or model.get("poi") \
            or model.get("minor"):
        return None
    kind = CIVITAI_KIND.get(str(model.get("type") or "").lower())
    if kind is None:
        return None
    variants = []
    first_files: list[dict] = []
    family = None
    created = None
    for version in (model.get("modelVersions") or [])[:4]:
        files = [f for f in version.get("files") or []
                 if str(f.get("type") or "") in ("Model", "Pruned Model")]
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        if primary is None or not primary.get("sizeKB"):
            continue
        if not first_files:
            first_files = files
            family = version.get("baseModel")
            created = version.get("publishedAt")
        meta = primary.get("metadata") if isinstance(primary.get("metadata"), dict) else {}
        hashes = primary.get("hashes") if isinstance(primary.get("hashes"), dict) else {}
        name = str(primary.get("name") or "")
        fmt = "safetensors" if str(meta.get("format") or "").lower() == "safetensor" \
            else (_ext(name) or "ckpt")
        version_id = version.get("id")
        variants.append({
            "id": f"v{version_id}",
            "label": str(version.get("name") or name)[:60],
            "precision": meta.get("fp"),
            "format": fmt,
            "bytes": int(float(primary["sizeKB"]) * 1024),
            "parts": 1,
            "file": name,
            "download_url": f"{CIVITAI_BASE}/api/download/models/{version_id}"
            if isinstance(version_id, int) else None,
            "direct": True,
            "sha256": str(hashes.get("SHA256") or "").lower() or None,
            "bundled": kind == "image",
            "base_model": version.get("baseModel"),
            "trained_words": [str(w) for w in (version.get("trainedWords") or [])[:8]],
            "save_path": SAVE_PATH_BY_ROLE.get(
                "checkpoint" if kind == "image" else kind),
        })
    if not variants:
        return None
    if kind == "image" and any(k in str(family or "").lower() for k in _VIDEO_BASES):
        kind = "video"
    scan, scan_detail = _civitai_scan(first_files)
    stats = model.get("stats") if isinstance(model.get("stats"), dict) else {}
    creator = model.get("creator") if isinstance(model.get("creator"), dict) else {}
    commercial = model.get("allowCommercialUse")
    licence = "civitai: " + ("commercial use allowed"
                             if commercial and commercial != ["None"]
                             and commercial != "None" else "non-commercial")
    return {
        "id": f"civitai:{model_id}",
        "source": "civitai",
        "name": str(model.get("name") or f"Model {model_id}")[:120],
        "publisher": str(creator.get("username") or "unknown"),
        "kind": kind,
        "task": str(model.get("type") or "").lower() or None,
        "family": family,
        "base_model": family,
        "description": clean_text(model.get("description")),
        "url": f"{CIVITAI_BASE}/models/{model_id}",
        "license": licence,
        "gated": False,
        "downloads": stats.get("downloadCount"),
        "likes": stats.get("thumbsUpCount"),
        "trending": None,
        "created_at": created,
        "updated_at": created,
        "revision": str(variants[0]["id"]),
        "tags": [str(t) for t in (model.get("tags") or [])[:MAX_TAGS]],
        "params": None,
        "runtime": "comfyui",
        "variants": variants,
        "companions": [],
        "safety": {"scan": scan, "scan_detail": scan_detail,
                   "publisher_trust": "unknown", "code_files": 0,
                   "custom_code": False,
                   "hash_published": any(v.get("sha256") for v in variants)},
        "nsfw": False,
        "curated": False,
        "quality": None,
        "fetched_at": dbmod.now_ms(),
    }


# ---------------------------------------------------------------------------
# ComfyUI-Manager model list
# ---------------------------------------------------------------------------

COMFY_TYPE_KIND = {
    "checkpoint": "image", "checkpoints": "image", "diffusion_model": "image",
    "unet": "image", "lora": "lora", "vae": "vae", "taesd": "vae",
    "vae_approx": "vae", "upscale": "upscale", "clip": "text_encoder",
    "text_encoders": "text_encoder", "text_encoder": "text_encoder",
    "clip_vision": "vision", "controlnet": "controlnet", "t2i-adapter": "controlnet",
    "t2i-style": "controlnet", "embedding": "embedding", "embeddings": "embedding",
    "ip-adapter": "controlnet", "insightface": "vision", "sam": "vision",
    "animatediff": "video", "motion lora": "lora", "gligen": "controlnet",
}


def normalize_comfy_manager(item: dict) -> dict | None:
    """One ComfyUI-Manager ``model-list.json`` row -> one catalog entry."""
    filename = str(item.get("filename") or "")
    url = str(item.get("url") or "")
    size = parse_size(item.get("size"))
    if not filename or not url.startswith("https://") or not size:
        return None
    raw_type = str(item.get("type") or "").lower()
    kind = COMFY_TYPE_KIND.get(raw_type, "other")
    base = str(item.get("base") or "") or None
    if kind == "image" and base and any(k in base.lower() for k in _VIDEO_BASES):
        kind = "video"
    save_path = str(item.get("save_path") or "")
    if save_path in ("", "default"):
        save_path = SAVE_PATH_BY_ROLE.get(
            "checkpoint" if raw_type.startswith("checkpoint") else kind, "") or ""
    elif not save_path.startswith("models/"):
        save_path = "models/" + save_path
    reference = str(item.get("reference") or "")
    return {
        "id": "comfy:" + re.sub(r"[^A-Za-z0-9._-]+", "_", save_path + "/" + filename)[:140],
        "source": "comfy_manager",
        "name": str(item.get("name") or filename)[:120],
        "publisher": (reference.split("/")[3] if reference.count("/") >= 3 else "unknown"),
        "kind": kind,
        "task": raw_type or None,
        "family": base,
        "base_model": base,
        "description": clean_text(item.get("description")),
        "url": reference if reference.startswith("https://") else url,
        "license": None,
        "gated": False,
        "downloads": None, "likes": None, "trending": None,
        "created_at": None, "updated_at": None, "revision": None,
        "tags": [t for t in (raw_type, base) if t],
        "params": None,
        "runtime": "comfyui",
        "variants": [{
            "id": filename, "label": filename.rsplit(".", 1)[0][:60],
            "precision": precision_of(filename), "format": _ext(filename),
            "bytes": size, "parts": 1, "file": filename,
            "download_url": url, "direct": True, "sha256": None,
            "bundled": raw_type.startswith("checkpoint"),
            "save_path": save_path or None,
        }],
        "companions": [],
        "safety": {"scan": "unknown", "scan_detail": None,
                   "publisher_trust": "curated", "code_files": 0,
                   "custom_code": False, "hash_published": False},
        "nsfw": False,
        "curated": True,
        "quality": None,
        "fetched_at": dbmod.now_ms(),
    }


# ---------------------------------------------------------------------------
# OpenModelDB
# ---------------------------------------------------------------------------

def normalize_openmodeldb(model_id: str, item: dict) -> dict | None:
    """One OpenModelDB record -> one upscaler entry."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", str(model_id)) or not isinstance(item, dict):
        return None
    variants = []
    for resource in item.get("resources") or []:
        urls = [u for u in resource.get("urls") or [] if str(u).startswith("https://")]
        size = resource.get("size")
        fmt = str(resource.get("type") or "").lower()
        if not urls or not isinstance(size, int) or fmt not in WEIGHT_EXTS:
            continue
        # Two resources may share a format (two mirrors, two exports); the id
        # must still be unique within the entry, so later ones are numbered.
        variant_id = f"{model_id}.{fmt}"
        taken = {v["id"] for v in variants}
        suffix = 2
        while variant_id in taken:
            variant_id = f"{model_id}.{fmt}#{suffix}"
            suffix += 1
        variants.append({
            "id": variant_id, "label": f"{fmt} ({resource.get('platform')})",
            "precision": None, "format": fmt, "bytes": size, "parts": 1,
            "file": f"{model_id}.{fmt}", "download_url": urls[0], "direct": True,
            "sha256": resource.get("sha256"), "bundled": False,
            "save_path": SAVE_PATH_BY_ROLE["upscale"],
        })
    if not variants:
        return None
    variants.sort(key=lambda v: v["format"] != "safetensors")
    author = item.get("author")
    if isinstance(author, list):
        author = ", ".join(str(a) for a in author[:3])
    scale = item.get("scale")
    return {
        "id": f"omdb:{model_id}",
        "source": "openmodeldb",
        "name": str(item.get("name") or model_id)[:120],
        "publisher": str(author or "unknown"),
        "kind": "upscale",
        "task": f"{scale}x upscale" if scale else "upscale",
        "family": str(item.get("architecture") or "") or None,
        "base_model": None,
        "description": clean_text(item.get("description")),
        "url": f"https://openmodeldb.info/models/{model_id}",
        "license": str(item.get("license") or "") or None,
        "gated": False,
        "downloads": None, "likes": None, "trending": None,
        "created_at": (str(item["date"]) + "T00:00:00Z") if item.get("date") else None,
        "updated_at": None, "revision": None,
        "tags": [str(t) for t in (item.get("tags") or [])[:MAX_TAGS]],
        "params": None,
        "scale": scale,
        "runtime": "comfyui",
        "variants": variants,
        "companions": [],
        "safety": {"scan": "unknown", "scan_detail": None,
                   "publisher_trust": "curated", "code_files": 0,
                   "custom_code": False,
                   "hash_published": any(v.get("sha256") for v in variants)},
        "nsfw": False,
        "curated": True,
        "quality": None,
        "fetched_at": dbmod.now_ms(),
    }


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def normalize_ollama(item: dict) -> dict | None:
    """One ``/api/tags`` row -> one language-model entry (local weights only)."""
    name = str(item.get("name") or "")
    size = item.get("size")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", name) or not isinstance(size, int) \
            or size <= 0:
        return None  # size 0 is a cloud-hosted model: nothing runs locally
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    library = name.split(":", 1)[0]
    return {
        "id": f"ollama:{name}",
        "source": "ollama",
        "name": name,
        "publisher": "ollama library",
        "kind": "llm",
        "task": "text-generation",
        "family": details.get("family") or library,
        "base_model": None,
        "description": "Language model from the Ollama library. Capabilities: "
                       + (", ".join(str(c) for c in item.get("capabilities") or [])
                          or "completion") + f". Install with: ollama pull {name}",
        "url": f"https://ollama.com/library/{library}",
        "license": None,
        "gated": False,
        "downloads": None, "likes": None, "trending": None,
        "created_at": item.get("modified_at"),
        "updated_at": item.get("modified_at"),
        "revision": str(item.get("digest") or "") or None,
        "tags": [str(c) for c in (item.get("capabilities") or [])[:MAX_TAGS]],
        "params": None,
        "runtime": "ollama",
        "pull": f"ollama pull {name}",
        "variants": [{
            "id": name, "label": details.get("quantization_level") or "default",
            "precision": details.get("quantization_level") or None,
            "format": "gguf", "bytes": size, "parts": 1, "file": name,
            "download_url": f"https://ollama.com/library/{name}", "direct": False,
            "sha256": None, "bundled": False, "save_path": None,
        }],
        "companions": [],
        "safety": {"scan": "unknown", "scan_detail": None,
                   "publisher_trust": "trusted", "code_files": 0,
                   "custom_code": False, "hash_published": bool(item.get("digest"))},
        "nsfw": False,
        "curated": True,
        "quality": None,
        "fetched_at": dbmod.now_ms(),
    }


# ---------------------------------------------------------------------------
# GitHub leads
# ---------------------------------------------------------------------------

def github_search_url(query: dict) -> str:
    text = str(query.get("q") or "")
    if not re.fullmatch(r"[A-Za-z0-9 :._>+-]{1,120}", text):
        raise ValueError("unsupported GitHub query")
    limit = max(1, min(30, int(query.get("limit") or 10)))
    return (f"{GITHUB_API}/search/repositories?q={quote(text)}"
            f"&sort=stars&order=desc&per_page={limit}")


def normalize_github_lead(repo: dict) -> dict | None:
    url = str(repo.get("html_url") or "")
    if not url.startswith("https://github.com/") or repo.get("archived") \
            or repo.get("fork"):
        return None
    return {
        "name": str(repo.get("full_name") or "")[:120],
        "url": url,
        "description": clean_text(repo.get("description"), 240),
        "stars": repo.get("stargazers_count"),
        "updated_at": repo.get("pushed_at"),
        "topics": [str(t) for t in (repo.get("topics") or [])[:8]],
        "license": (repo.get("license") or {}).get("spdx_id")
        if isinstance(repo.get("license"), dict) else None,
    }
