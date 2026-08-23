"""Architecture / base-model detection over the FULL tensor key set (fixes B3).

Layer 0 integrity -> Layer 1 declared metadata -> Layer 2 adapter detection ->
Layer 3 structural prefix signature -> Layer 3b component decomposition ->
Layer 4 shape probes -> Layer 5 filename/category priors.

Never inspects only ``keys[:100]``: safetensors key order is arbitrary, which is
precisely why the previous implementation mislabelled ``flux1-dev-fp8`` as a VAE.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import arch_rules as R
from .safetensors_header import DTYPE_BITS, PRECISION_BY_DTYPE

_NUM_RE = re.compile(r"\.\d+\.")


@dataclass
class ArchResult:
    base_model_family: str = "Unknown"
    base_model_variant: str | None = None
    model_role: str = "unknown"
    modality: str = "unknown"
    architecture_label: str | None = None
    arch_source: str = "none"
    arch_confidence: float = 0.0
    is_adapter: bool = False
    adapter_format: str | None = None
    adapter_rank: int | None = None
    adapter_alpha: float | None = None
    is_bundled: bool = False
    components: dict = field(default_factory=dict)
    param_count_primary: int | None = None
    param_count_total: int | None = None
    precision: str | None = None
    quantization: str | None = None
    prediction_type: str | None = None
    resolution_hint: str | None = None
    signals: list[str] = field(default_factory=list)

    def components_json(self) -> str | None:
        if not self.components:
            return None
        try:
            return json.dumps(self.components, ensure_ascii=False)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Key-set summary
# ---------------------------------------------------------------------------

@dataclass
class KeyView:
    keys: list[str]
    top: set[str]
    lvl2: set[str]
    joined: str

    @classmethod
    def build(cls, keys) -> KeyView:
        top: set[str] = set()
        lvl2: set[str] = set()
        for k in keys:
            parts = k.split(".")
            top.add(parts[0])
            if len(parts) > 1:
                lvl2.add(parts[0] + "." + parts[1])
        return cls(list(keys), top, lvl2, "\n".join(keys))

    def has(self, token: str) -> bool:
        if token.startswith("~"):
            return token[1:] in self.joined
        return token in self.top or token in self.lvl2


# ---------------------------------------------------------------------------
# Layer 1 - declared metadata
# ---------------------------------------------------------------------------

def _family_from_text(text: str) -> str | None:
    t = text.lower()
    for needle, family in R.METADATA_MAP:
        if needle in t:
            return family
    return None


def family_from_metadata(md: dict) -> tuple[str | None, str | None]:
    """Return (family, signal) from ``__metadata__``, probing in a fixed order."""
    if not isinstance(md, dict) or not md:
        return None, None
    for key in R.METADATA_KEYS:
        val = md.get(key)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            try:
                val = json.dumps(val)
            except (TypeError, ValueError):
                continue
        text = str(val)
        fam = _family_from_text(text)
        if fam:
            return fam, f"{key}={text[:80]}"
    cfg = md.get("config")
    if isinstance(cfg, str) and cfg.strip().startswith("{"):
        try:
            parsed = json.loads(cfg)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            blob = json.dumps(parsed)[:4000]
            fam = _family_from_text(blob)
            if fam:
                cname = None
                node = parsed.get("transformer") if isinstance(parsed.get("transformer"), dict) else parsed
                if isinstance(node, dict):
                    cname = node.get("_class_name") or (
                        node.get("architectures") or [None])[0] if isinstance(
                        node.get("architectures"), list) else node.get("_class_name")
                return fam, f"config={cname or blob[:60]}"
    return None, None


def prediction_from_metadata(md: dict) -> str | None:
    for key in ("modelspec.prediction_type", "prediction_type", "ss_prediction_type"):
        v = md.get(key)
        if isinstance(v, str) and v:
            low = v.lower()
            if "v_pred" in low or low == "v":
                return "v_prediction"
            if "flow" in low:
                return "flow"
            if "eps" in low:
                return "epsilon"
            return low[:32]
    return None


def resolution_from_metadata(md: dict) -> str | None:
    for key in ("modelspec.resolution", "ss_resolution", "resolution"):
        v = md.get(key)
        if isinstance(v, str) and re.fullmatch(r"\d{2,5}\s*[x,]\s*\d{2,5}", v.strip()):
            return re.sub(r"\s*[x,]\s*", "x", v.strip())
    return None


# ---------------------------------------------------------------------------
# Layer 2 - adapter detection
# ---------------------------------------------------------------------------

def detect_adapter(view: KeyView, shapes: dict) -> tuple[bool, str | None, int | None, float | None, list[str]]:
    counts: dict[str, int] = {}
    total = len(view.keys) or 1
    for k in view.keys:
        for suffix, fmt in R.ADAPTER_SUFFIXES:
            if k.endswith(suffix):
                counts[fmt] = counts.get(fmt, 0) + 1
                break
    if not counts:
        return False, None, None, None, []
    matched = sum(counts.values())
    real = {f: c for f, c in counts.items() if f != "kohya_alpha"}
    real_matched = sum(real.values())
    # A bare ``.alpha`` suffix is not proof of an adapter: Stable Audio's base
    # model carries 96 of them.  Require a genuine LoRA/LoKr/OFT suffix, or an
    # overwhelming alpha-only ratio.
    if real_matched / total < 0.05 and not (
        not real and counts.get("kohya_alpha", 0) / total >= 0.30
    ):
        return False, None, None, None, []

    fmt = max(real or counts, key=lambda f: (real or counts)[f])
    if fmt == "kohya_alpha":
        fmt = "kohya"

    rank = None
    for k in view.keys:
        if k.endswith((".lora_A.weight", ".lora_down.weight", ".lora.down.weight",
                       ".lora_A.default.weight")):
            shape = shapes.get(k)
            if isinstance(shape, list) and shape:
                rank = int(shape[0])
                break
        if k.endswith((".lora_B.weight", ".lora_up.weight", ".lora.up.weight")):
            shape = shapes.get(k)
            if isinstance(shape, list) and len(shape) > 1:
                rank = int(shape[1])
                break
    signals = [f"adapter:{fmt} ({matched}/{total} keys)"]
    return True, fmt, rank, None, signals


def strip_adapter_keys(keys) -> list[str]:
    """Remove adapter suffixes and wrapper prefixes to expose the base structure."""
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        s = k
        for suffix, _fmt in R.ADAPTER_SUFFIXES:
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        changed = True
        while changed:
            changed = False
            for prefix in R.ADAPTER_PREFIXES:
                if s.startswith(prefix) and len(s) > len(prefix):
                    s = s[len(prefix):]
                    changed = True
                    break
        s = s.replace("_", ".") if s.startswith("lora.") else s
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Layer 3 - structural rules
# ---------------------------------------------------------------------------

def match_rules(view: KeyView) -> tuple[str, str, str, str, float, bool, str] | None:
    for (name, requires, forbids, family, role, modality, label, conf, ambiguous) in R.RULES:
        if not requires:
            continue
        if not all(view.has(t) for t in requires):
            continue
        if any(view.has(t) for t in forbids):
            continue
        return family, role, modality, label, conf, ambiguous, name
    return None


# ---------------------------------------------------------------------------
# Layer 3b - component decomposition
# ---------------------------------------------------------------------------

def _component_of(key: str) -> str:
    parts = key.split(".")
    head = parts[0]
    for prefix, comp in R.COMPONENT_PREFIXES:
        if head == prefix:
            if comp == "unet" and len(parts) > 1:
                sub = parts[1]
                for p2, c2 in R.COMPONENT_PREFIXES:
                    if sub == p2 and c2 != "unet":
                        return c2
                if sub == "diffusion_model":
                    return "unet"
            return comp
    if head in R.UNET_COMPONENT_KEYS:
        return "unet"
    return "other"


def decompose(keys, shapes: dict, dtypes: dict) -> dict:
    comps: dict[str, dict] = {}
    for k in keys:
        comp = _component_of(k)
        shape = shapes.get(k) or []
        n = 1
        for d in shape:
            if not isinstance(d, int) or d < 0:
                n = 0
                break
            n *= d
        entry = comps.setdefault(comp, {"params": 0, "tensors": 0, "dtypes": {}})
        entry["params"] += n
        entry["tensors"] += 1
        dt = dtypes.get(k)
        if dt:
            entry["dtypes"][dt] = entry["dtypes"].get(dt, 0) + max(1, n)
    for entry in comps.values():
        dts = entry.pop("dtypes", {})
        entry["dtype"] = max(dts, key=dts.get) if dts else None
    return comps


def _primary_component(comps: dict) -> str | None:
    for name in ("unet", "other"):
        if name in comps and comps[name]["params"] > 0:
            return name
    if not comps:
        return None
    return max(comps, key=lambda c: comps[c]["params"])


# ---------------------------------------------------------------------------
# Layer 4 - shape probes
# ---------------------------------------------------------------------------

_CTX_KEY_RE = re.compile(
    r"(input_blocks|down_blocks)\..*(attn2|attn_2)\.to_k\.weight$"
)


def sd_variant_from_shapes(view: KeyView, shapes: dict) -> tuple[str | None, str | None]:
    """768 -> SD1.5, 1024 -> SD2.x, 2048 -> SDXL."""
    ctx = None
    for k, shape in shapes.items():
        if _CTX_KEY_RE.search(k) and isinstance(shape, list) and len(shape) > 1:
            ctx = int(shape[1])
            break
    if ctx is None:
        for k, shape in shapes.items():
            if k.endswith("attn2.to_k.weight") and isinstance(shape, list) and len(shape) > 1:
                ctx = int(shape[1])
                break
    if ctx == 768:
        return "SD1.5", f"cross-attn context dim {ctx}"
    if ctx == 1024:
        return "SD2.x", f"cross-attn context dim {ctx}"
    if ctx == 2048:
        return "SDXL", f"cross-attn context dim {ctx}"
    if view.has("~add_embedding.linear_1") or view.has("~label_emb"):
        return "SDXL", "add_embedding/label_emb present"
    return None, None


# ---------------------------------------------------------------------------
# Layer 5 - priors
# ---------------------------------------------------------------------------

def family_from_filename(stem: str) -> tuple[str | None, str | None]:
    s = stem.lower().replace(" ", "_")
    for token, family in R.FILENAME_FAMILY:
        if token in s:
            return family, f"filename token '{token}'"
    return None, None


def variant_from_filename(stem: str) -> str | None:
    s = stem.lower()
    for token, variant in R.FILENAME_VARIANT:
        if token in s:
            return variant
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def detect(*, keys, shapes: dict, dtypes: dict, metadata: dict, category: str,
           stem: str, file_size: int = 0, fmt: str = "safetensors") -> ArchResult:
    res = ArchResult()
    keys = list(keys or [])
    shapes = shapes or {}
    dtypes = dtypes or {}
    metadata = metadata or {}

    cat_role, cat_modality = R.CATEGORY_ROLE.get(category, ("unknown", "unknown"))
    res.model_role = cat_role
    res.modality = cat_modality

    view = KeyView.build(keys)

    # ---- Layer 3b: components + params (independent of the winning rule) ----
    if keys:
        comps = decompose(keys, shapes, dtypes)
        real = {k: v for k, v in comps.items() if v["params"] > 0}
        res.components = real
        res.param_count_total = sum(v["params"] for v in real.values()) or None
        primary = _primary_component(real)
        if primary:
            res.param_count_primary = real[primary]["params"]
        heavy = [k for k, v in real.items()
                 if v["params"] > 0.01 * (res.param_count_total or 1) and k != "other"]
        res.is_bundled = len(heavy) >= 2

    # ---- precision / quantization ------------------------------------------
    if dtypes:
        weight: dict[str, int] = {}
        for k, dt in dtypes.items():
            n = 1
            for d in shapes.get(k, []) or []:
                n *= d if isinstance(d, int) and d > 0 else 1
            weight[dt] = weight.get(dt, 0) + max(1, n)
        top = max(weight, key=weight.get)
        res.precision = PRECISION_BY_DTYPE.get(top, top.lower())
        distinct = {PRECISION_BY_DTYPE.get(d, d.lower()) for d in weight
                    if DTYPE_BITS.get(d, 0) >= 4}
        if top in ("F8_E4M3", "F8_E5M2"):
            res.quantization = "fp8_" + ("e4m3" if top == "F8_E4M3" else "e5m2")
        elif top in ("I8", "U8"):
            res.quantization = "int8"
        elif top.startswith(("Q", "IQ")):
            res.quantization = top.lower()
        elif len(distinct) > 2:
            res.precision = "mixed"
    if view.has("scaled_fp8") or view.has("~scaled_fp8"):
        res.quantization = "comfy_scaled_fp8"
        res.precision = res.precision or "fp8"
    qm = metadata.get("_quantization_metadata")
    if isinstance(qm, str) and not res.quantization:
        low = qm.lower()
        if "float8_e4m3" in low:
            res.quantization = "fp8_e4m3"
        elif "int8" in low:
            res.quantization = "int8"
    if isinstance(metadata.get("general.file_type"), int):
        res.quantization = f"gguf_ft{metadata['general.file_type']}"

    res.prediction_type = prediction_from_metadata(metadata)
    res.resolution_hint = resolution_from_metadata(metadata)

    # ---- Layer 2: adapter ---------------------------------------------------
    is_adapter, afmt, rank, _alpha, asignals = detect_adapter(view, shapes)
    base_view = view
    if is_adapter:
        res.is_adapter = True
        res.adapter_format = afmt
        res.adapter_rank = rank
        res.model_role = "lora"
        res.signals.extend(asignals)
        base_view = KeyView.build(strip_adapter_keys(keys))
        for mk in ("lora_alpha", "ss_network_alpha", "network_alpha"):
            v = metadata.get(mk)
            try:
                if v is not None:
                    res.adapter_alpha = float(str(v).split(",")[0])
                    break
            except (TypeError, ValueError):
                continue
        if rank is None:
            for mk in ("lora_rank", "ss_network_dim", "network_dim"):
                v = metadata.get(mk)
                try:
                    if v is not None:
                        res.adapter_rank = int(float(str(v).split(",")[0]))
                        break
                except (TypeError, ValueError):
                    continue

    # ---- Layer 3: structural ------------------------------------------------
    struct = match_rules(base_view)
    ambiguous = True
    struct_family: str | None = None
    if struct:
        fam, role, modality, label, conf, ambiguous, rule_name = struct
        struct_family = fam
        res.architecture_label = label
        res.arch_source = "structural"
        res.arch_confidence = conf
        res.signals.append(f"rule:{rule_name}")
        if not res.is_adapter:
            res.model_role = role
            res.modality = modality
        else:
            res.modality = modality
        if fam != "Other":
            res.base_model_family = fam
    else:
        res.signals.append("rule:none")

    if base_view.top:
        res.signals.append("prefixes: " + ",".join(sorted(base_view.top)[:10]))

    # ---- Layer 1: declared metadata (authoritative) -------------------------
    md_family, md_signal = family_from_metadata(metadata)
    if md_family:
        if (struct_family is not None and struct_family != md_family
                and struct_family != "Other"):
            # The structural rule identified a different family, so its label no
            # longer describes this model.  Metadata wins; the label follows.
            res.architecture_label = None
        res.base_model_family = md_family
        res.arch_source = "metadata"
        res.arch_confidence = 0.95
        res.signals.append(md_signal or "metadata")

    # ---- Layer 4: shape probes ---------------------------------------------
    if res.base_model_family in ("SD1.5", "SD2.x", "SDXL") and res.arch_source != "metadata":
        variant, sig = sd_variant_from_shapes(base_view, shapes)
        if variant:
            res.base_model_family = variant
            res.arch_source = "shape"
            res.arch_confidence = max(res.arch_confidence, 0.88)
            res.signals.append(sig or "shape probe")

    # ---- Layer 5: priors ----------------------------------------------------
    # Auxiliary roles (text encoders, upscalers, depth nets...) have no diffusion
    # base family, so a filename token there would be a guess, not a signal.
    auxiliary = res.model_role in R.AUXILIARY_ROLES
    fn_family, fn_signal = (None, None) if auxiliary else family_from_filename(stem)
    use_prior = (
        res.base_model_family in ("Unknown", "Other")
        or (ambiguous and res.arch_source not in ("metadata", "shape"))
    )
    if fn_family and use_prior:
        if res.base_model_family in ("Unknown", "Other"):
            res.base_model_family = fn_family
            res.arch_source = "prior"
            res.arch_confidence = min(res.arch_confidence or 0.0, 0.5) or 0.45
        elif fn_family != res.base_model_family:
            res.base_model_family = fn_family
            res.arch_source = "prior"
            res.arch_confidence = 0.5
        res.signals.append(fn_signal or "filename prior")

    variant = variant_from_filename(stem)
    if variant and variant in ("Pony", "Illustrious", "NoobAI"):
        if res.base_model_family == "SDXL":
            res.base_model_variant = variant
    elif variant:
        res.base_model_variant = variant

    if res.modality == "unknown" or res.base_model_family in R.MODALITY_BY_FAMILY:
        res.modality = R.MODALITY_BY_FAMILY.get(res.base_model_family, res.modality)
    if res.modality == "unknown":
        res.modality = cat_modality

    # Roles that the category knows better than the structure.
    if not res.is_adapter and category in ("controlnet", "clip_vision", "upscale_models",
                                           "latent_upscale_models", "embeddings",
                                           "style_models", "gligen", "hypernetworks"):
        res.model_role = cat_role
    if res.is_adapter:
        res.model_role = "lora"

    if res.architecture_label is None:
        res.architecture_label = _fallback_label(res, category)
    if res.is_adapter and res.architecture_label and "LoRA" not in res.architecture_label:
        res.architecture_label = f"{res.architecture_label} LoRA"

    # A file whose header parsed cleanly is never "Unknown": it is a real model
    # from a family outside the frozen vocabulary, which the vocabulary calls
    # 'Other'.  'Unknown' is reserved for files we could not read.
    if res.base_model_family == "Unknown" and keys:
        res.base_model_family = "Other"
        if res.arch_source == "none":
            res.arch_source = "prior"
            res.arch_confidence = 0.3
        res.signals.append("family outside canonical vocabulary")

    if res.arch_source == "none" and res.base_model_family == "Unknown":
        res.arch_confidence = 0.0
    enforce_label_family(res, category)
    res.signals = res.signals[:8]
    return res


# A label naming a family other than the detected one is what the DETAILS panel
# shows the user, so a stale structural label surviving a metadata override reads
# as "22B video model = audio checkpoint".  This makes that class impossible.
_FAMILY_LABEL_RE = tuple(
    (fam, re.compile(r"(?<![\w.-])" + re.escape(fam) + r"(?![\w.-])", re.IGNORECASE))
    for fam in R.FAMILIES if fam not in ("Other", "Unknown")
)


def label_names_family(label: str | None) -> str | None:
    """Return the canonical family a label names, or None."""
    if not label:
        return None
    for fam, rx in _FAMILY_LABEL_RE:
        if rx.search(label):
            return fam
    return None


def enforce_label_family(res: ArchResult, category: str) -> None:
    """architecture_label must never name a family other than base_model_family."""
    named = label_names_family(res.architecture_label)
    if named is None or named == res.base_model_family:
        return
    res.signals.append(f"label '{named}' contradicted family; relabelled")
    res.architecture_label = _fallback_label(res, category)
    if res.is_adapter and "LoRA" not in res.architecture_label:
        res.architecture_label = f"{res.architecture_label} LoRA"


def _fallback_label(res: ArchResult, category: str) -> str:
    role = res.model_role.replace("_", " ")
    fam = res.base_model_family
    if fam not in ("Unknown", "Other"):
        return f"{fam} {role}"
    return f"{category or 'model'} ({role})"


def format_params(n: int | None) -> str | None:
    if not n or n <= 0:
        return None
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)
