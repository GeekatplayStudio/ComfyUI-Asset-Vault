"""Launch settings that keep headroom, derived from the same budget as the verdicts.

The advisor never plans to use 100% of a GPU, so the settings it suggests tell
the runtimes the same thing: reserve the system share and the headroom, load one
model at a time, and fall back gracefully instead of failing at the limit.

Everything here is **text for the owner to copy**.  This module writes no file
outside its own return value and changes no setting on the machine.
"""

from __future__ import annotations

from . import fit

GIB = fit.GIB


def _gb(value: int) -> float:
    return round(value / GIB, 1)


def _comfyui(budget: fit.Budget, profile: dict) -> dict:
    flags: list[dict] = []
    env: list[dict] = []
    notes: list[str] = []
    if budget.backend == "cpu":
        flags.append({"flag": "--cpu", "value": None,
                      "why": "No usable GPU was found; run everything on the processor."})
        notes.append("Image and video generation on a processor takes minutes "
                     "per image. Prefer small quantised models.")
    else:
        held_back = max(0, budget.vram_total - budget.vram_safe)
        if budget.unified:
            notes.append(
                "Unified memory: ComfyUI cannot reserve a slice of it by flag, "
                "so the limit is set through the two PyTorch values below.")
            share = max(0.5, round(1 - budget.vram_headroom_pct / 100.0, 2))
            env.append({"name": "PYTORCH_MPS_HIGH_WATERMARK_RATIO", "value": f"{share:.2f}",
                        "why": "Hard ceiling on GPU allocations as a share of the "
                               "recommended maximum; leaves the headroom free."})
            env.append({"name": "PYTORCH_MPS_LOW_WATERMARK_RATIO",
                        "value": f"{max(0.4, share - 0.15):.2f}",
                        "why": "Point at which cached GPU memory starts being "
                               "released; must stay below the high watermark."})
            flags.append({"flag": "--force-fp16", "value": None,
                          "why": "Half precision halves memory on Apple GPUs "
                                 "with no visible quality cost for most models."})
        else:
            flags.append({"flag": "--reserve-vram", "value": f"{_gb(held_back):.1f}",
                          "why": f"Keeps {_gb(held_back):.1f} GB free for the "
                                 "desktop, the browser and the safety headroom."})
            env.append({"name": "PYTORCH_CUDA_ALLOC_CONF",
                        "value": "expandable_segments:True",
                        "why": "Reduces fragmentation, the usual cause of an "
                               "out-of-memory error with memory still free."})
            total_gb = budget.vram_total / GIB
            if total_gb < 4.5:
                flags.append({"flag": "--novram", "value": None,
                              "why": "Under 4 GB: keep weights in system memory "
                                     "and move them to the GPU one block at a time."})
            elif total_gb < 9:
                flags.append({"flag": "--lowvram", "value": None,
                              "why": "8 GB class: split models so only the active "
                                     "part occupies the GPU."})
            else:
                notes.append("ComfyUI's default memory mode (smart offloading) "
                             "is right for this GPU; no VRAM-mode flag is needed.")
        if budget.backend == "directml":
            flags.append({"flag": "--directml", "value": None,
                          "why": "This GPU is driven through DirectML on Windows."})
    if budget.ram_total and budget.ram_total < 24 * GIB:
        notes.append("System memory is modest: avoid keeping several large "
                     "models cached, and expect reloads when switching models.")
    command = " ".join(f["flag"] + (f" {f['value']}" if f["value"] else "") for f in flags)
    return {"flags": flags, "env": env, "command_line": command, "notes": notes}


def _ollama(budget: fit.Budget) -> dict:
    env = [
        {"name": "OLLAMA_MAX_LOADED_MODELS", "value": "1",
         "why": "One model in memory at a time, so two never compete for the GPU."},
        {"name": "OLLAMA_NUM_PARALLEL", "value": "1",
         "why": "Each parallel request multiplies the context memory."},
        {"name": "OLLAMA_KEEP_ALIVE", "value": "5m",
         "why": "Release the model after five idle minutes so other "
                "applications get the memory back."},
        {"name": "OLLAMA_CONTEXT_LENGTH", "value": "8192",
         "why": "The context size the estimates assume; a larger context "
                "needs more memory than the verdicts account for."},
    ]
    if budget.backend != "cpu" and not budget.unified:
        held_back = max(0, budget.vram_total - budget.vram_safe)
        env.insert(0, {"name": "OLLAMA_GPU_OVERHEAD", "value": str(int(held_back)),
                       "why": f"Bytes of GPU memory to leave unused "
                              f"({_gb(held_back):.1f} GB: system share plus headroom)."})
    return {"env": env}


def _llama_cpp(budget: fit.Budget, profile: dict) -> dict:
    cpu = profile.get("cpu") or {}
    threads = cpu.get("performance_cores") or cpu.get("physical_cores") or 4
    args = [{"flag": "--ctx-size", "value": "8192",
             "why": "Matches the context size the estimates assume."},
            {"flag": "--threads", "value": str(threads),
             "why": "One thread per performance core; more adds contention, not speed."}]
    if budget.backend == "cpu":
        args.append({"flag": "--n-gpu-layers", "value": "0",
                     "why": "No usable GPU."})
    else:
        args.append({"flag": "--n-gpu-layers", "value": "999",
                     "why": "For models marked Optimal or Good, put every layer on "
                            "the GPU. For an Offload model lower this until the "
                            "load fits in the safe budget."})
    return {"args": args,
            "command_line": " ".join(f"{a['flag']} {a['value']}" for a in args)}


def build(profile: dict, budget: fit.Budget) -> dict:
    """The whole safe-use configuration for this machine."""
    comfyui = _comfyui(budget, profile)
    ollama = _ollama(budget)
    llama = _llama_cpp(budget, profile)

    env_lines = ["# Asset Vault - safe-use environment for this machine",
                 f"# Planned against {_gb(budget.vram_safe):.1f} GB of GPU memory "
                 f"({budget.vram_headroom_pct}% headroom) and "
                 f"{_gb(budget.ram_safe):.1f} GB of system memory "
                 f"({budget.ram_headroom_pct}% headroom).", ""]
    for row in comfyui["env"] + ollama["env"]:
        env_lines.append(f"# {row['why']}")
        env_lines.append(f"{row['name']}={row['value']}")
    launch_lines = ["# Asset Vault - ComfyUI launch arguments for this machine",
                    "# Append these to the python main.py line of your launcher.",
                    comfyui["command_line"] or "# (no extra arguments needed)"]

    if budget.backend == "cpu":
        summary = ("No usable GPU: plan against "
                   f"{_gb(budget.ram_safe):.1f} GB of system memory.")
    elif budget.unified:
        summary = (f"Plan against {_gb(budget.vram_safe):.1f} GB of the "
                   f"{_gb(budget.ram_total):.0f} GB unified memory; the rest stays "
                   "with the system.")
    else:
        summary = (f"Plan against {_gb(budget.vram_safe):.1f} GB of the "
                   f"{_gb(budget.vram_total):.0f} GB GPU: "
                   f"{_gb(budget.vram_reserve):.1f} GB is left to the system and "
                   f"{budget.vram_headroom_pct}% is kept as headroom.")

    return {
        "summary": summary,
        "budget": budget.as_dict(),
        "comfyui": comfyui,
        "ollama": ollama,
        "llama_cpp": llama,
        "files": [
            {"name": "comfyui_safe_launch.txt", "content": "\n".join(launch_lines) + "\n"},
            {"name": "advisor_safe.env", "content": "\n".join(env_lines) + "\n"},
        ],
        "rules": [
            "Close GPU-heavy applications (games, video editors, other "
            "generators) before a large run.",
            "A model marked Maximum fits only by spending the headroom: lower "
            "the resolution or batch size first if it fails.",
            "Each LoRA, ControlNet or extra adapter adds its own memory on top "
            "of the base model's figure.",
            "Higher resolutions and longer videos raise working memory beyond "
            "these estimates, which assume default sizes.",
        ],
        "inferred": True,
    }
