"""What a workflow *does* - one vocabulary for templates and the owner's graphs.

``capability_tags`` (graph_workflow) answers "which node families appear", which
is too coarse to browse by: ``LoadImage`` alone made 410 of 689 official
templates ``img2img``, and nothing said "removes a background" or "is a utility".
This module answers the question an owner actually asks - *what can this
workflow do for me?* - with a fixed list of tasks, plus whether it runs on local
models or on a paid partner API.

Two evidence sources, in this order:

1. **ComfyUI's own template tags** (``index.json``: "Image Upscale", "Remove
   Background", "Image to Video" ...).  These are curated by the ComfyUI team,
   so for a template they win outright.  Its category ("Image Tools", "3D
   Model" ...) is the fallback when the tags say nothing specific.
2. **The node classes in the graph**, for everything else - the owner's own
   workflows and the examples bundled with custom nodes.

Pure functions; no I/O.  ``tasks()`` is ordered by :data:`PRIMARY_ORDER` so its
first element is the workflow's main job, which is what grouping and sorting use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

#: (key, label) in the order the interface lists them.  Keys are API values and
#: are frozen once released; labels may be reworded.
TASKS: tuple[tuple[str, str], ...] = (
    ("text_to_image", "Text to image"),
    ("image_edit", "Image edit"),
    ("upscale", "Upscale"),
    ("remove_background", "Remove background"),
    ("face", "Face & character"),
    ("controlnet", "ControlNet & pose"),
    ("text_to_video", "Text to video"),
    ("image_to_video", "Image to video"),
    ("video_edit", "Video edit"),
    ("lip_sync", "Lip sync & talking"),
    ("three_d", "3D"),
    ("audio", "Audio & speech"),
    ("llm", "Text & LLM"),
    ("design", "Vector & design"),
    ("utility", "Utilities"),
)
TASK_KEYS = tuple(k for k, _ in TASKS)
TASK_LABELS = dict(TASKS)

#: Which task is a workflow's *main* job when it does several.  A text-to-image
#: graph with a hires-fix upscale is a text-to-image workflow; a video workflow
#: that also loads an image is an image-to-video one.
PRIMARY_ORDER: tuple[str, ...] = (
    "three_d", "lip_sync", "image_to_video", "text_to_video", "video_edit", "audio",
    # Text-to-image outranks upscale (a hires pass does not change what the graph
    # is for), but upscale outranks image edit: "load, upscale with a model,
    # refine with a light img2img pass" is an upscale workflow.
    "text_to_image", "upscale", "remove_background", "image_edit", "face",
    "controlnet", "design", "llm", "utility",
)

#: Tasks the graph identifies so specifically that a file name never overrides
#: them ("..._I2V_InfiniteTalk" is a lip-sync workflow, not a plain I2V one).
SPECIFIC = frozenset({"lip_sync", "three_d"})

#: Words in a workflow's file name or title that say what it is for.  They only
#: pick the main task among those the graph already shows, or replace a bare
#: "utility" when the graph could not be read deeply enough to say.
NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("upscal", "upscale"), ("rembg", "remove_background"), ("removebg", "remove_background"),
    ("removebackground", "remove_background"), ("bgremov", "remove_background"),
    ("lipsync", "lip_sync"), ("talking", "lip_sync"),
    ("inpaint", "image_edit"), ("outpaint", "image_edit"), ("img2img", "image_edit"),
    ("i2v", "image_to_video"), ("img2vid", "image_to_video"),
    ("imagetovideo", "image_to_video"), ("t2v", "text_to_video"),
    ("texttovideo", "text_to_video"), ("v2v", "video_edit"),
    ("t2i", "text_to_image"), ("txt2img", "text_to_image"),
    ("texttoimage", "text_to_image"), ("tts", "audio"), ("music", "audio"),
)

RUNS_ON = ("local", "api")

# ---------------------------------------------------------------------------
# Evidence 1: ComfyUI's template tags and categories
# ---------------------------------------------------------------------------

#: Lower-cased ``index.json`` tag -> task.  Generic tags ("Image", "Video",
#: "API", "Int8", "Turbo") map to nothing on purpose.
TEMPLATE_TAG_TASKS: dict[str, str] = {
    "text to image": "text_to_image",
    "image edit": "image_edit", "inpainting": "image_edit", "outpainting": "image_edit",
    "relight": "image_edit", "style transfer": "image_edit",
    "style reference": "image_edit", "restoration": "image_edit", "color": "image_edit",
    "long image": "image_edit", "layout": "design",
    "image upscale": "upscale", "video upscale": "upscale",
    "remove background": "remove_background",
    "character reference": "face", "portrait": "face", "virtual try-on": "face",
    "fashion": "face",
    "controlnet": "controlnet", "pose map": "controlnet",
    "text to video": "text_to_video",
    "image to video": "image_to_video", "flf2v": "image_to_video",
    "reference to video": "image_to_video",
    "video edit": "video_edit", "video to video": "video_edit",
    "video extend": "video_edit", "motion control": "video_edit",
    "character replacement": "video_edit",
    "lip sync": "lip_sync", "audio to video": "lip_sync",
    "3d": "three_d", "image to 3d": "three_d", "text to 3d": "three_d",
    "audio": "audio", "text to audio": "audio", "text to music": "audio",
    "text to speech": "audio", "tts": "audio", "voice cloning": "audio",
    "music": "audio", "audio editing": "audio", "speech to text": "audio",
    "text generation": "llm", "llm": "llm",
    "vector": "design", "brand design": "design", "web design": "design",
    "node basics": "utility", "depth": "utility", "normal estimation": "utility",
    "element segmentation": "utility", "layer decompose": "utility",
}

#: ``index.json`` category -> task, used only when no tag was specific.
TEMPLATE_CATEGORY_TASKS: dict[str, str] = {
    "image": "text_to_image", "video": "text_to_video", "3d model": "three_d",
    "image tools": "utility", "video tools": "video_edit", "audio": "audio",
    "llm": "llm", "character & fashion": "face", "product & ads": "image_edit",
    "vector": "design", "brand & design": "design", "node basics": "utility",
}

# ---------------------------------------------------------------------------
# Evidence 2: node classes.  Needles are matched case-insensitively as
# substrings of the class names (spaces and underscores removed), the same way
# capability_tags are - except providers, which are matched as a class-name
# *prefix*: "vidu" is inside "individual" and "imagen" inside "ImageNoise".
# ---------------------------------------------------------------------------

#: ComfyUI's partner-API nodes are named after the provider:
#: ``KlingTextToVideoNode``, ``IdeogramV3``, ``OpenAIGPTImage1``, ``GeminiNode``.
API_PREFIXES = (
    "kling", "luma", "runway", "veo", "pika", "minimax", "hailuo", "ideogram",
    "recraft", "stability", "openai", "fluxpro", "fluxkontextpro", "flux2pro", "bfl",
    "tripo", "rodin", "moonvalley", "pixverse", "vidu", "bytedance", "seedream",
    "seedance", "gemini", "google", "topaz", "elevenlabs", "meshy", "sora",
    "anthropic", "atlascloud", "wavespeed", "replicate", "falai",
)
#: Classes ending like this are API wrappers whatever the provider.
API_SUFFIXES = ("api", "apinode")
#: Providers whose nodes generate a still image (local text-to-image is
#: recognised by its empty latent instead).
API_IMAGE_PREFIXES = ("ideogram", "openaidalle", "openaigptimage", "fluxpro", "flux2pro",
                      "seedream", "recraft", "stabilitystableimage", "geminiimage",
                      "googleimagen", "lumaimage", "bytedanceimage")
REMOVE_BG_NEEDLES = (
    "rembg", "rmbg", "birefnet", "removebackground", "backgroundremov", "inspyrenet",
    "briarmbg", "briaremove", "ben2", "transparentbg",
)
#: Model-based upscaling only.  ``ImageScaleBy`` is a resize and
#: ``LatentUpscale`` a hires-fix step inside a text-to-image graph.
UPSCALE_NEEDLES = (
    "upscalemodel", "imageupscale", "ultimatesdupscale", "supir", "seedvr",
    "genupscale", "upscaler", "esrgan", "ccsr", "topazimageenhance", "topazvideo",
)
FACE_NEEDLES = (
    "facedetailer", "reactor", "instantid", "pulid", "facerestore", "faceswap",
    "faceid", "faceanalysis", "codeformer", "gfpgan", "facefusion", "portrait",
)
CONTROLNET_NEEDLES = (
    "controlnet", "openpose", "dwpose", "depthanything", "canny", "lineart",
    "preprocessor", "aio_preprocessor", "zoe", "midas", "softedge",
)
#: Besides any class with "video" in its name (``SaveVideo``, ``VHS_VideoCombine``,
#: ``WanImageToVideo``, ``KlingTextToVideoNode``) - minus the loaders below.
VIDEO_NEEDLES = (
    "wanvace", "wanfirstlastframe", "svdimg2vid", "animatediff", "ltxv", "cogvideo",
    "mochi", "savewebm", "saveanimated", "framepack", "skyreels", "wan22",
)
VIDEO_INPUT_NEEDLES = ("loadvideo", "videoinput", "loadvideopath", "videoloader")
#: Classes with "video" in the name that neither make nor take a video.
VIDEO_NOT_OUTPUT = (*VIDEO_INPUT_NEEDLES, "videoinfo", "videohelper", "getvideo",
                    "videosettings")
IMAGE_INPUT_NEEDLES = (
    "loadimage", "imagetovideo", "img2vid", "i2v", "firstlastframe", "startend",
    "startimage", "referencelatent",
)
FRAME_TOOL_NEEDLES = ("rife", "film vfi", "filmvfi", "frameinterp", "gimmvfi")
LIP_SYNC_NEEDLES = (
    "lipsync", "wav2lip", "latentsync", "infinitetalk", "multitalk", "fantasytalking",
    "humo", "wans2v", "sonic", "echomimic", "talking",
)
THREE_D_NEEDLES = (
    "hunyuan3d", "hy3d", "savemesh", "previewmesh", "trimesh", "savglb", "saveglb",
    "load3d", "preview3d", "triposr", "trellis", "tripo", "rodin", "meshy", "3dgen",
    "pointcloud", "gaussiansplat", "voxel",
)
AUDIO_NEEDLES = (
    "saveaudio", "previewaudio", "acestep", "stableaudio", "vaeencodeaudio",
    "vaedecodeaudio", "tts", "f5tts", "kokoro", "chatterbox", "whisper", "mmaudio",
    "musicgen", "elevenlabs", "audioldm", "emptylatentaudio", "musicmapper",
)
LLM_NEEDLES = (
    "llm", "ollama", "openaichat", "geminichat", "claude", "anthropic", "chatgpt",
    "qwenvl", "qwen2vl", "llava", "joycaption", "florence2", "promptgenerator",
    "prompt generator", "textgenerat", "captioner", "describeimage",
)
DESIGN_NEEDLES = ("svg", "vector", "recraft")
INPAINT_EDIT_NEEDLES = (
    "inpaint", "outpaint", "imagepadforoutpaint", "setlatentnoisemask", "kontext",
    "qwenimageedit", "imageedit", "editmodel", "referencelatent", "instructpix2pix",
    "relight", "ic-light", "iclight", "styletransfer", "ipadapter", "redux",
)
EMPTY_LATENT_NEEDLES = ("emptylatentimage", "emptysd3latentimage", "emptyflux",
                        "emptyhunyuanimage", "emptyqwenimage", "emptychroma",
                        "emptylatent")


def _squash(name: str) -> str:
    """``"VHS_VideoCombine"`` -> ``"vhsvideocombine"``; ``"Remove Background"`` ->
    ``"removebackground"`` - one spelling for every needle to match against."""
    return "".join(ch for ch in str(name).lower() if ch not in " _-.")


def _has(classes: list[str], needles: Iterable[str]) -> bool:
    squashed = [_squash(n) for n in needles]
    return any(n in c for c in classes for n in squashed)


def _starts(classes: list[str], prefixes: Iterable[str]) -> bool:
    return any(c.startswith(p) for c in classes for p in prefixes)


def is_api_graph(node_types: Mapping[str, int] | Iterable[str]) -> bool:
    classes = [_squash(c) for c in node_types]
    return (_starts(classes, API_PREFIXES)
            or any(c.endswith(API_SUFFIXES) for c in classes))


def _graph_tasks(node_types: Mapping[str, int] | Iterable[str]) -> list[str]:
    classes = [_squash(c) for c in node_types]
    found: set[str] = set()

    has_sampler = any("sampler" in c and "select" not in c for c in classes)
    is_video = (any("video" in c and not any(n in c for n in VIDEO_NOT_OUTPUT)
                    for c in classes)
                or _has(classes, VIDEO_NEEDLES))
    is_3d = _has(classes, THREE_D_NEEDLES)
    is_lip = _has(classes, LIP_SYNC_NEEDLES)
    audio_out = _has(classes, AUDIO_NEEDLES)
    api_image = _starts(classes, API_IMAGE_PREFIXES)
    loads_image = any(c.startswith("loadimage") for c in classes)

    if is_3d:
        found.add("three_d")
    if is_lip:
        found.add("lip_sync")
    if is_video:
        if _has(classes, VIDEO_INPUT_NEEDLES):
            found.add("video_edit")
        elif _has(classes, IMAGE_INPUT_NEEDLES):
            found.add("image_to_video")
        else:
            found.add("text_to_video")
    elif _has(classes, FRAME_TOOL_NEEDLES) or _has(classes, VIDEO_INPUT_NEEDLES):
        found.add("video_edit")
    if audio_out and not is_video and not is_lip:
        found.add("audio")
    if _has(classes, REMOVE_BG_NEEDLES):
        found.add("remove_background")
    if _has(classes, UPSCALE_NEEDLES):
        found.add("upscale")
    if _has(classes, FACE_NEEDLES):
        found.add("face")
    if _has(classes, CONTROLNET_NEEDLES):
        found.add("controlnet")
    if _has(classes, DESIGN_NEEDLES):
        found.add("design")
    if _has(classes, LLM_NEEDLES):
        found.add("llm")

    if not (is_video or is_3d or is_lip or (audio_out and not has_sampler)):
        if _has(classes, INPAINT_EDIT_NEEDLES):
            found.add("image_edit")
        if has_sampler or api_image:
            if _has(classes, EMPTY_LATENT_NEEDLES) or (api_image and not loads_image):
                found.add("text_to_image")
            elif (loads_image and api_image) or _has(classes, ("vaeencode",)):
                # Encoding an existing image - loaded, or made by another node
                # (a QR code, a render) - and resampling it.
                found.add("image_edit")
            elif not found & {"upscale", "image_edit", "face"}:
                # A sampler and nothing that says otherwise: the common case.
                found.add("text_to_image")

    if not found:
        # Loads, inspects, converts or segments without generating anything.
        found.add("utility")
    return _ordered(found)


def _ordered(found: Iterable[str]) -> list[str]:
    s = set(found)
    return [k for k in PRIMARY_ORDER if k in s]


def _template_tasks(tags: Iterable[str] | None, category: str | None) -> list[str]:
    found = {TEMPLATE_TAG_TASKS[t.strip().lower()] for t in (tags or [])
             if isinstance(t, str) and t.strip().lower() in TEMPLATE_TAG_TASKS}
    if not found and category:
        mapped = TEMPLATE_CATEGORY_TASKS.get(str(category).strip().lower())
        if mapped:
            found.add(mapped)
    return _ordered(found)


def _name_hints(*names: str | None) -> list[str]:
    text = " ".join(_squash(n) for n in names if n)
    out: list[str] = []
    for word, task in NAME_HINTS:
        if word in text and task not in out:
            out.append(task)
    return out


def graph_classes(data: object, *, max_depth: int = 8, max_nodes: int = 20000) -> set[str]:
    """Every node class in a UI graph, including those inside subgraph
    definitions.  A workflow that wraps its whole pipeline in one subgraph shows
    only ``SaveImage`` at the top level; this is what lets it still be read."""
    out: set[str] = set()
    seen = 0

    def visit(obj: object, depth: int) -> None:
        nonlocal seen
        if depth > max_depth or not isinstance(obj, dict):
            return
        for node in obj.get("nodes") or []:
            if seen >= max_nodes:
                return
            if isinstance(node, dict):
                seen += 1
                t = node.get("type") or node.get("class_type")
                if isinstance(t, str) and t:
                    out.add(t[:200])
        defs = obj.get("definitions")
        subs = defs.get("subgraphs") if isinstance(defs, dict) else None
        for entries in (subs, obj.get("subgraphs")):
            if isinstance(entries, list):
                for entry in entries:
                    visit(entry, depth + 1)

    visit(data, 0)
    return out


def classify(node_types: Mapping[str, int] | Iterable[str] | None = None, *,
             template_tags: Iterable[str] | None = None,
             template_category: str | None = None,
             open_source: bool | None = None,
             is_template: bool = False,
             names: Iterable[str | None] = ()) -> dict:
    """``{"tasks": [...], "primary": str, "runs_on": "local"|"api"}``.

    ``tasks`` is never empty: a graph that generates nothing is a ``utility``.
    ``names`` (file name, title) only ever choose among what the graph shows,
    except that they replace a bare ``utility``.
    """
    node_types = node_types or {}
    tasks: list[str] = []
    if is_template:
        tasks = _template_tasks(template_tags, template_category)
    if not tasks:
        tasks = _graph_tasks(node_types)
        hints = _name_hints(*names)
        if tasks == ["utility"] and hints:
            tasks = _ordered(hints)
        elif tasks[0] not in SPECIFIC:
            for hint in hints:
                if hint in tasks:
                    tasks = [hint] + [t for t in tasks if t != hint]
                    break

    tag_api = any(isinstance(t, str) and t.strip().lower() == "api"
                  for t in (template_tags or []))
    if is_template and (open_source is False or tag_api):
        runs_on = "api"
    elif is_template and open_source is True:
        runs_on = "local"
    else:
        runs_on = "api" if is_api_graph(node_types) else "local"
    return {"tasks": tasks, "primary": tasks[0], "runs_on": runs_on}


def label(task: str | None) -> str:
    return TASK_LABELS.get(str(task or ""), str(task or ""))
