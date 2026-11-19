"""Memory arithmetic: what a model needs against what this machine can give.

Everything in this module is an **estimate** and every payload carries
``"inferred": true``.  The inputs are measured (file sizes, installed memory);
the conclusion is a planning figure, not a benchmark.

The budget
----------
A machine is never planned to 100%.  From the installed memory the advisor
subtracts, in order:

1. a **system reserve** - what the desktop, the browser and the OS already
   hold and will not give back;
2. a **headroom** share - the safety margin the owner asked for.

What is left is the *safe* budget.  The headroom itself is the difference
between "safe" and "max": a model that only fits by spending the headroom still
runs, but with nothing to spare for a larger image, a second LoRA or a browser
tab, and that is exactly what the ``max`` verdict means.

The verdicts, best first
------------------------
``optimal``  fits in 85% of the safe budget - room to work
``good``     fits in the safe budget
``max``      fits only by using the headroom - the largest thing that runs
             entirely on the GPU
``offload``  larger than the GPU: part of it is streamed from system memory
             (or, for a language model, split between GPU and CPU) - slow
``cpu``      no usable GPU; runs on the processor from system memory
``no``       does not fit in this machine's memory at all
``base``     an add-on (LoRA, ControlNet, embedding) whose cost is its base
             model's - shown with the add-on's own small overhead
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

GIB = 1024 ** 3
MIB = 1024 ** 2

DEFAULT_VRAM_HEADROOM_PCT = 15
DEFAULT_RAM_HEADROOM_PCT = 25
MIN_HEADROOM_PCT = 5
MAX_HEADROOM_PCT = 50

#: What the OS and desktop hold on a discrete GPU that also drives a display.
DISCRETE_SYSTEM_RESERVE = int(0.8 * GIB)
#: What the rest of the system needs out of unified / system memory.
RAM_SYSTEM_RESERVE_MIN = 4 * GIB
RAM_SYSTEM_RESERVE_SHARE = 0.12

OPTIMAL_SHARE = 0.85

KINDS = ("image", "video", "llm", "lora", "audio", "vision", "controlnet",
         "upscale", "vae", "text_encoder", "embedding", "3d", "other")
ADDON_KINDS = frozenset({"lora", "controlnet", "embedding", "vae", "text_encoder"})

#: Working memory on top of the weights while sampling, per kind: a fixed part
#: (latents, attention buffers, decoder) plus a share of the weights.
WORKING_SET = {
    "image": (int(1.5 * GIB), 0.10),
    "video": (int(3.5 * GIB), 0.12),
    "audio": (int(1.0 * GIB), 0.10),
    "3d": (int(2.5 * GIB), 0.12),
    "vision": (int(1.0 * GIB), 0.12),
    "upscale": (int(1.5 * GIB), 0.25),
    "other": (int(1.0 * GIB), 0.12),
}
#: A language model's context cache and scratch buffers at a default context.
LLM_FIXED_OVERHEAD = int(0.6 * GIB)
LLM_OVERHEAD_SHARE = 0.10
#: An add-on's own footprint above its base model.
ADDON_OVERHEAD = int(0.25 * GIB)
#: Smallest slice of a diffusion model that must stay on the GPU for block
#: streaming to be worth attempting.
OFFLOAD_MIN_GPU_SHARE = 0.25
OFFLOAD_MIN_GPU_BYTES = 3 * GIB

VERDICT_ORDER = ("optimal", "good", "max", "offload", "cpu", "base", "no", "unknown")
VERDICT_LABEL = {
    "optimal": "Optimal", "good": "Good fit", "max": "Maximum",
    "offload": "Offload", "cpu": "CPU only", "base": "Add-on",
    "no": "Too large", "unknown": "Unknown",
}
RUNS = frozenset({"optimal", "good", "max", "offload", "cpu"})
COMFORTABLE = frozenset({"optimal", "good"})
ON_GPU = frozenset({"optimal", "good", "max"})

SPEED_LABEL = {"fast": "Fast", "moderate": "Moderate", "slow": "Slow",
               "very_slow": "Very slow", "none": "-"}


@dataclass(frozen=True)
class Budget:
    """The memory this machine can offer, after reserve and headroom."""

    backend: str
    unified: bool
    vram_total: int          # one device - what a diffusion pipeline can use
    vram_pooled: int         # every discrete device - what a GGUF runner can use
    vram_reserve: int
    vram_safe: int
    vram_max: int
    pooled_safe: int
    pooled_max: int
    ram_total: int
    ram_reserve: int
    ram_safe: int
    ram_max: int
    vram_headroom_pct: int
    ram_headroom_pct: int
    disk_free: int | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["inferred"] = True
        return data


def clamp_pct(value: object, default: int) -> int:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return default
    return max(MIN_HEADROOM_PCT, min(MAX_HEADROOM_PCT, number))


def make_budget(profile: dict, *, vram_headroom_pct: object = None,
                ram_headroom_pct: object = None,
                disk_free: int | None = None) -> Budget:
    """Derive the planning budget from a hardware profile."""
    accelerator = profile.get("accelerator") or {}
    backend = str(accelerator.get("backend") or "cpu")
    unified = bool(accelerator.get("unified"))
    ram_total = int((profile.get("memory") or {}).get("total_bytes") or 0)
    vram_total = int(accelerator.get("vram_total_bytes") or 0)
    pooled = int(accelerator.get("pooled_vram_bytes") or vram_total)
    vram_pct = clamp_pct(vram_headroom_pct, DEFAULT_VRAM_HEADROOM_PCT)
    ram_pct = clamp_pct(ram_headroom_pct, DEFAULT_RAM_HEADROOM_PCT)

    ram_reserve = max(RAM_SYSTEM_RESERVE_MIN, int(ram_total * RAM_SYSTEM_RESERVE_SHARE))
    ram_reserve = min(ram_reserve, ram_total // 2)
    ram_max = max(0, ram_total - ram_reserve)
    ram_safe = max(0, int(ram_total * (1 - ram_pct / 100.0)) - ram_reserve)

    if backend == "cpu" or vram_total <= 0:
        return Budget(backend="cpu", unified=False, vram_total=0, vram_pooled=0,
                      vram_reserve=0, vram_safe=0, vram_max=0, pooled_safe=0,
                      pooled_max=0, ram_total=ram_total, ram_reserve=ram_reserve,
                      ram_safe=ram_safe, ram_max=ram_max,
                      vram_headroom_pct=vram_pct, ram_headroom_pct=ram_pct,
                      disk_free=disk_free)

    if unified:
        # One pool.  The GPU share is already a fraction of RAM; the ceiling is
        # whichever is smaller - that share, or RAM minus the system reserve.
        vram_reserve = 0
        vram_max = min(vram_total, ram_max)
        vram_safe = max(0, int(vram_max * (1 - vram_pct / 100.0)))
        pooled_max, pooled_safe = vram_max, vram_safe
    else:
        vram_reserve = min(DISCRETE_SYSTEM_RESERVE, vram_total // 4)
        vram_max = max(0, vram_total - vram_reserve)
        vram_safe = max(0, int(vram_total * (1 - vram_pct / 100.0)) - vram_reserve)
        pooled_max = max(0, pooled - vram_reserve)
        pooled_safe = max(0, int(pooled * (1 - vram_pct / 100.0)) - vram_reserve)

    return Budget(backend=backend, unified=unified, vram_total=vram_total,
                  vram_pooled=pooled, vram_reserve=vram_reserve,
                  vram_safe=vram_safe, vram_max=vram_max,
                  pooled_safe=pooled_safe, pooled_max=pooled_max,
                  ram_total=ram_total, ram_reserve=ram_reserve,
                  ram_safe=ram_safe, ram_max=ram_max,
                  vram_headroom_pct=vram_pct, ram_headroom_pct=ram_pct,
                  disk_free=disk_free)


# ---------------------------------------------------------------------------
# Requirement
# ---------------------------------------------------------------------------

def _smallest_per_role(companions: list[dict] | None) -> dict[str, int]:
    """The lightest companion of each role - what a minimal setup loads."""
    best: dict[str, int] = {}
    for item in companions or []:
        size = int(item.get("bytes") or 0)
        if size <= 0:
            continue
        role = str(item.get("role") or "other")
        if role not in best or size < best[role]:
            best[role] = size
    return best


def requirement(kind: str, weights_bytes: int, *, companions: list[dict] | None = None,
                runtime: str | None = None, bundled: bool = False) -> dict:
    """Peak memory for one variant.  All figures in bytes.

    ``gpu_bytes``     what has to be resident on the GPU while it works;
    ``companion_bytes`` encoders and decoders that are loaded, used and released
                      around the main model (they need system memory, and on a
                      unified-memory machine they share the same pool);
    ``ram_bytes``     system memory needed alongside a GPU run;
    ``total_bytes``   everything at once - what a CPU-only or unified-memory
                      run must hold.
    """
    kind = kind if kind in KINDS else "other"
    weights = max(0, int(weights_bytes or 0))
    roles = {} if bundled else _smallest_per_role(companions)
    companion_total = sum(roles.values())
    largest_companion = max(roles.values(), default=0)

    if kind == "llm":
        overhead = LLM_FIXED_OVERHEAD + int(weights * LLM_OVERHEAD_SHARE)
        if runtime == "transformers":
            overhead = LLM_FIXED_OVERHEAD + int(weights * 0.20)
        gpu = weights + overhead + companion_total  # a projector stays loaded
        ram = int(1.5 * GIB)
        total = gpu + ram
    elif kind in ADDON_KINDS:
        overhead = ADDON_OVERHEAD
        gpu = weights + overhead
        ram = weights
        total = gpu
    else:
        fixed, share = WORKING_SET.get(kind, WORKING_SET["other"])
        overhead = fixed + int(weights * share)
        # The sampler holds the main model and its working set; encoders are
        # swapped in and out around it, so only the largest one ever overlaps.
        gpu = max(weights + overhead, largest_companion + int(0.5 * GIB))
        ram = max(largest_companion, weights // 2) + 2 * GIB
        # Everything in one pool (unified memory, or a CPU run): the encoders
        # are released before sampling, but freed memory is not instantly
        # reusable, so half of them is counted against the peak.
        total = max(weights + overhead + companion_total // 2,
                    largest_companion + GIB)

    return {"weights_bytes": weights, "overhead_bytes": int(overhead),
            "companion_bytes": int(companion_total),
            "gpu_bytes": int(gpu), "ram_bytes": int(ram),
            "total_bytes": int(total), "inferred": True}


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def _speed(verdict: str, kind: str, backend: str) -> str:
    if verdict in ("no", "unknown", "base"):
        return "none"
    if verdict == "cpu":
        return "very_slow" if kind != "llm" else "slow"
    if verdict == "offload":
        return "slow" if backend == "cuda" else "very_slow"
    heavy = kind in ("video", "3d")
    if backend == "cuda":
        return "moderate" if verdict == "max" and heavy else "fast"
    if backend in ("rocm", "ipex"):
        return "moderate"
    # mps / directml: capable, but well behind CUDA for the same memory.
    return "slow" if heavy else "moderate"


def _pct(need: int, of: int) -> float | None:
    return round(need / of * 100.0, 1) if of > 0 else None


def assess(kind: str, need: dict, budget: Budget, *,
           download_bytes: int | None = None) -> dict:
    """Place one requirement against the budget and explain the result."""
    kind = kind if kind in KINDS else "other"
    gpu_need, total_need = need["gpu_bytes"], need["total_bytes"]
    weights = need["weights_bytes"]
    reasons: list[str] = []

    if weights <= 0:
        verdict = "unknown"
        reasons.append("The file size is not published, so the requirement "
                       "cannot be estimated.")
    elif kind in ADDON_KINDS:
        verdict = "base"
        reasons.append("An add-on: it costs its base model's memory plus about "
                       f"{need['overhead_bytes'] / GIB:.2f} GB of its own.")
    elif budget.backend == "cpu":
        if kind == "llm" and total_need <= budget.ram_safe:
            verdict = "cpu"
            reasons.append("No usable GPU: runs on the processor from system "
                           "memory. Expect a few tokens per second.")
        elif total_need <= budget.ram_safe:
            verdict = "cpu"
            reasons.append("No usable GPU: generation on the processor is "
                           "possible but takes minutes per image.")
        else:
            verdict = "no"
            reasons.append("Needs more system memory than this machine can "
                           "safely give.")
    else:
        llm = kind == "llm"
        safe = budget.pooled_safe if llm else budget.vram_safe
        ceiling = budget.pooled_max if llm else budget.vram_max
        # On unified memory everything shares one pool, so the encoders count
        # against the same budget instead of living in separate system memory.
        need_here = total_need if budget.unified and not llm else gpu_need
        if need_here <= safe * OPTIMAL_SHARE:
            verdict = "optimal"
            reasons.append("Fits with room to spare for larger outputs, extra "
                           "adapters and other applications.")
        elif need_here <= safe:
            verdict = "good"
            reasons.append("Fits inside the safe budget with the configured "
                           "headroom left free.")
        elif need_here <= ceiling:
            verdict = "max"
            reasons.append("Runs fully on the GPU, but only by using the safety "
                           "headroom - close other GPU applications first.")
        elif llm and total_need <= ceiling + budget.ram_safe and not budget.unified:
            verdict = "offload"
            share = max(0, min(100, int(ceiling / max(1, gpu_need) * 100)))
            reasons.append(f"Too large for the GPU alone: about {share}% of the "
                           "layers fit on it and the rest run on the processor.")
        elif (not llm and not budget.unified
              and ceiling >= max(OFFLOAD_MIN_GPU_BYTES,
                                 int(weights * OFFLOAD_MIN_GPU_SHARE)
                                 + need["overhead_bytes"])
              and need["weights_bytes"] + need["companion_bytes"] <= budget.ram_safe):
            verdict = "offload"
            reasons.append("Larger than the GPU: ComfyUI can stream the model "
                           "from system memory in low-VRAM mode, several times "
                           "slower.")
        elif budget.unified and total_need <= budget.ram_max:
            verdict = "offload"
            reasons.append("Exceeds the GPU share of unified memory; it will "
                           "page to disk and run very slowly.")
        else:
            verdict = "no"
            reasons.append("Needs more memory than this machine has, even with "
                           "offloading.")
        if verdict in ("optimal", "good", "max") and not budget.unified \
                and need["ram_bytes"] > budget.ram_safe:
            reasons.append("System memory is the tighter limit: loading will "
                           "use swap. Close other applications or add RAM.")
            if verdict == "optimal":
                verdict = "good"

    disk_ok = None
    if download_bytes and budget.disk_free is not None:
        disk_ok = download_bytes <= budget.disk_free
        if not disk_ok:
            reasons.append("Not enough free disk space on the models volume "
                           "for the download.")

    llm = kind == "llm"
    ceiling = (budget.pooled_max if llm else budget.vram_max) or budget.ram_max
    safe = (budget.pooled_safe if llm else budget.vram_safe) or budget.ram_safe
    shown = total_need if (budget.unified and not llm) or budget.backend == "cpu" \
        else gpu_need
    return {
        "verdict": verdict,
        "label": VERDICT_LABEL[verdict],
        "runs": verdict in RUNS,
        "speed": _speed(verdict, kind, budget.backend),
        "need_bytes": int(shown),
        "safe_bytes": int(safe),
        "ceiling_bytes": int(ceiling),
        "used_pct_of_safe": _pct(shown, safe),
        "used_pct_of_ceiling": _pct(shown, ceiling),
        "headroom_bytes": int(safe - shown),
        "disk_ok": disk_ok,
        "reasons": reasons,
        "inferred": True,
    }


def low_quant(precision: object) -> bool:
    """A quantisation under 4 bits per weight (Q2, Q3, IQ1-IQ3, ternary)."""
    text = str(precision or "").upper()
    return text.startswith(("Q1", "Q2", "Q3", "IQ1", "IQ2", "IQ3", "TQ1", "TQ2"))


def verdict_rank(verdict: str) -> int:
    try:
        return VERDICT_ORDER.index(verdict)
    except ValueError:
        return len(VERDICT_ORDER)


# ---------------------------------------------------------------------------
# Catalog entries
# ---------------------------------------------------------------------------

def assess_variant(entry: dict, variant: dict, budget: Budget) -> dict:
    kind = str(entry.get("kind") or "other")
    need = requirement(kind, int(variant.get("bytes") or 0),
                       companions=entry.get("companions"),
                       runtime=entry.get("runtime"),
                       bundled=bool(variant.get("bundled")))
    companion_download = 0 if variant.get("bundled") else \
        sum(_smallest_per_role(entry.get("companions")).values())
    result = assess(kind, need, budget,
                    download_bytes=int(variant.get("bytes") or 0) + companion_download)
    result["requirement"] = need
    result["download_bytes"] = int(variant.get("bytes") or 0) + companion_download
    return result


def assess_entry(entry: dict, budget: Budget) -> dict:
    """Every variant assessed, plus the two picks the interface leads with.

    ``optimum``  the highest-fidelity variant that is ``optimal`` or ``good``;
    ``maximum``  the highest-fidelity variant that still runs entirely on the
                 GPU (falling back to whatever runs at all on a CPU-only box);
    ``stretch``  a still larger variant that only runs by offloading.
    Fidelity within one model is ordered by size: a larger file of the same
    model is a less aggressive quantisation.
    """
    variants = []
    for variant in entry.get("variants") or []:
        result = assess_variant(entry, variant, budget)
        variants.append({"id": variant.get("id"), **result})
    by_size = sorted(range(len(variants)),
                     key=lambda i: int((entry["variants"][i]).get("bytes") or 0),
                     reverse=True)
    optimum = next((variants[i]["id"] for i in by_size
                    if variants[i]["verdict"] in COMFORTABLE), None)
    if entry.get("kind") == "llm" and optimum is not None:
        # Below 4 bits a language model degrades faster than it shrinks, so a
        # comfortable 4-bit-or-better variant beats a larger-but-crushed one.
        solid = next((variants[i]["id"] for i in by_size
                      if variants[i]["verdict"] in COMFORTABLE
                      and not low_quant(entry["variants"][i].get("precision"))), None)
        optimum = solid or optimum
    on_gpu = next((variants[i]["id"] for i in by_size
                   if variants[i]["verdict"] in ON_GPU), None)
    stretch = next((variants[i]["id"] for i in by_size if variants[i]["runs"]), None)
    maximum = on_gpu or stretch
    lead_id = optimum or maximum
    lead = next((v for v in variants if v["id"] == lead_id), None)
    if lead is None and variants:
        lead = min(variants, key=lambda v: (verdict_rank(v["verdict"]),
                                            v["need_bytes"]))
    verdict = lead["verdict"] if lead else "unknown"
    return {
        "verdict": verdict,
        "label": VERDICT_LABEL[verdict],
        "runs": bool(lead and lead["runs"]) or verdict == "base",
        "speed": lead["speed"] if lead else "none",
        "lead_variant": lead["id"] if lead else None,
        "optimum_variant": optimum,
        "maximum_variant": maximum,
        "stretch_variant": stretch if stretch != maximum else None,
        "need_bytes": lead["need_bytes"] if lead else None,
        "used_pct_of_safe": lead["used_pct_of_safe"] if lead else None,
        "used_pct_of_ceiling": lead["used_pct_of_ceiling"] if lead else None,
        "reasons": lead["reasons"] if lead else [],
        "variants": variants,
        "inferred": True,
    }
