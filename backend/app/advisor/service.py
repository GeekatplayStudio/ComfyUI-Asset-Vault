"""What the API serves: hardware + catalog + budget, joined and shaped.

This module is the only place that combines the other advisor modules, so the
router stays presentation-only and each of the others stays independently
testable.
"""

from __future__ import annotations

import contextlib
import math
import threading

from ..core import config_service
from ..core import db as dbmod
from ..core.errors import NotFoundError, ValidationError
from . import catalog, fit, hardware, security, sources, updater

KIND_LABELS = {
    "image": "Image", "video": "Video", "llm": "Language models", "lora": "LoRA",
    "audio": "Audio", "3d": "3D", "vision": "Vision", "controlnet": "ControlNet",
    "upscale": "Upscalers", "vae": "VAE", "text_encoder": "Text encoders",
    "embedding": "Embeddings", "other": "Other",
}
#: The order the interface lists kinds in - generators first, add-ons last.
KIND_ORDER = ("image", "video", "llm", "audio", "3d", "lora", "controlnet",
              "upscale", "vision", "text_encoder", "vae", "embedding", "other")

SORTS = ("recommended", "name", "-downloads", "-updated", "need", "-need",
         "-security", "security", "-size", "size")
DEFAULT_SORT = "recommended"
NEW_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
RECOMMEND_PER_KIND = 6
MAX_LIMIT = 500

#: Indexed ComfyUI model folder -> advisor kind.
CATEGORY_KIND = {
    "checkpoints": "image", "diffusion_models": "image", "unet": "image",
    "diffusers": "image", "loras": "lora", "vae": "vae", "vae_approx": "vae",
    "clip": "text_encoder", "text_encoders": "text_encoder",
    "clip_vision": "vision", "controlnet": "controlnet",
    "embeddings": "embedding", "upscale_models": "upscale",
    "latent_upscale_models": "upscale", "LLM": "llm", "llm": "llm",
    "audio_encoders": "audio", "ipadapter": "controlnet", "sams": "vision",
    "style_models": "controlnet", "gligen": "controlnet", "photomaker": "controlnet",
    "hypernetworks": "lora", "insightface": "vision", "ultralytics": "vision",
    "detection": "vision", "background_removal": "vision",
}

_cache_lock = threading.RLock()
_summary_cache: tuple[tuple, list[dict]] | None = None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def settings() -> dict:
    cfg = config_service.get_config()
    kinds = [k for k in (cfg.raw.get("advisor_kinds") or []) if k in fit.KINDS]
    return {
        "vram_headroom_pct": fit.clamp_pct(cfg.raw.get("advisor_vram_headroom_pct"),
                                           fit.DEFAULT_VRAM_HEADROOM_PCT),
        "ram_headroom_pct": fit.clamp_pct(cfg.raw.get("advisor_ram_headroom_pct"),
                                          fit.DEFAULT_RAM_HEADROOM_PCT),
        "auto_update": bool(cfg.raw.get("advisor_auto_update", True)),
        "kinds": kinds,
        "online_enabled": bool(cfg.online_enabled),
        "limits": {"min_headroom_pct": fit.MIN_HEADROOM_PCT,
                   "max_headroom_pct": fit.MAX_HEADROOM_PCT},
        "kind_options": [{"id": k, "label": KIND_LABELS[k]} for k in KIND_ORDER],
    }


def update_settings(patch: dict) -> dict:
    clean: dict = {}
    for key in ("vram_headroom_pct", "ram_headroom_pct"):
        if patch.get(key) is not None:
            value = int(patch[key])
            if not fit.MIN_HEADROOM_PCT <= value <= fit.MAX_HEADROOM_PCT:
                raise ValidationError(
                    f"{key} must be between {fit.MIN_HEADROOM_PCT} and "
                    f"{fit.MAX_HEADROOM_PCT}.", details={"field": key})
            clean["advisor_" + key] = value
    if patch.get("auto_update") is not None:
        clean["advisor_auto_update"] = bool(patch["auto_update"])
    if patch.get("kinds") is not None:
        unknown = [k for k in patch["kinds"] if k not in fit.KINDS]
        if unknown:
            raise ValidationError(f"Unknown model type: {unknown[0]}",
                                  details={"field": "kinds"})
        clean["advisor_kinds"] = sorted(set(patch["kinds"]), key=KIND_ORDER.index)
    if clean:
        config_service.set_config(clean)
        _invalidate()
    return settings()


# ---------------------------------------------------------------------------
# Context: profile + budget
# ---------------------------------------------------------------------------

def _marked_paths() -> dict[str, str]:
    """Which volumes hold the install and the model folders."""
    cfg = config_service.get_config()
    marks: dict[str, str] = {}
    if cfg.comfyui_path:
        marks[str(cfg.comfyui_path)] = "ComfyUI"
    # An unreadable root must not hide the hardware profile.
    with contextlib.suppress(Exception):
        for _category, path, _root in config_service.model_dirs(cfg):
            marks.setdefault(str(path), "Models")
    return marks


def _models_volume_free(profile: dict) -> int | None:
    volumes = profile.get("volumes") or []
    for role in ("Models", "ComfyUI"):
        for volume in volumes:
            if role in (volume.get("roles") or []):
                return int(volume["free_bytes"])
    return int(volumes[0]["free_bytes"]) if volumes else None


def context(*, refresh: bool = False) -> tuple[dict, fit.Budget, dict]:
    prefs = settings()
    profile = hardware.profile(refresh=refresh, marked_paths=_marked_paths())
    budget = fit.make_budget(profile,
                             vram_headroom_pct=prefs["vram_headroom_pct"],
                             ram_headroom_pct=prefs["ram_headroom_pct"],
                             disk_free=_models_volume_free(profile))
    return profile, budget, prefs


def hardware_report(*, refresh: bool = False) -> dict:
    profile, budget, _prefs = context(refresh=refresh)
    if refresh:
        _invalidate()
    return {**profile, "budget": budget.as_dict()}


def live(*, history: bool = False) -> dict:
    profile, budget, _prefs = context()
    reading = hardware.sample(history=history)
    reading["budget"] = {"vram_safe": budget.vram_safe, "vram_max": budget.vram_max,
                         "vram_total": budget.vram_total,
                         "ram_safe": budget.ram_safe + budget.ram_reserve,
                         "ram_total": budget.ram_total,
                         "vram_headroom_pct": budget.vram_headroom_pct,
                         "ram_headroom_pct": budget.ram_headroom_pct,
                         "unified": budget.unified, "backend": budget.backend}
    reading["accelerator"] = profile.get("accelerator")
    return reading


# ---------------------------------------------------------------------------
# Catalog rows
# ---------------------------------------------------------------------------

def _popularity(entry: dict) -> float:
    downloads = entry.get("downloads") or 0
    return min(75.0, 20.0 + 12.0 * math.log10(float(downloads) + 1.0)) if downloads else 30.0


def _rank(entry: dict, verdict: str, score: int, is_new: bool) -> float:
    quality = float(entry.get("quality") or _popularity(entry))
    bonus = {"optimal": 30, "good": 24, "max": 8, "offload": -12, "cpu": -8,
             "base": 0, "no": -70, "unknown": -40}.get(verdict, 0)
    return quality + bonus + (score - 70) / 5.0 + (3 if is_new else 0)


def _variant(entry: dict, variant_id: str | None) -> dict | None:
    for variant in entry.get("variants") or []:
        if variant.get("id") == variant_id:
            return variant
    return None


def _pick(entry: dict, assessment: dict, variant_id: str | None) -> dict | None:
    """One named variant with its own verdict - never the entry's lead verdict."""
    variant = _variant(entry, variant_id)
    result = next((v for v in assessment["variants"] if v["id"] == variant_id), None)
    if variant is None or result is None:
        return None
    return {"id": variant_id, "label": variant.get("label"),
            "precision": variant.get("precision"), "format": variant.get("format"),
            "bytes": variant.get("bytes"), "download_url": variant.get("download_url"),
            "verdict": result["verdict"], "verdict_label": result["label"],
            "speed": result["speed"], "need_bytes": result["need_bytes"],
            "used_pct_of_ceiling": result["used_pct_of_ceiling"]}


def _summary(entry: dict, budget: fit.Budget, first_seen: dict, now: int) -> dict:
    assessment = fit.assess_entry(entry, budget)
    lead = _variant(entry, assessment["lead_variant"]) or entry["variants"][0]
    score = security.score_entry(entry, lead)
    seen = first_seen.get(entry["id"]) or 0
    is_new = bool(seen and now - int(seen) < NEW_WINDOW_MS)
    return {
        "id": entry["id"], "name": entry["name"], "publisher": entry.get("publisher"),
        "kind": entry["kind"], "kind_label": KIND_LABELS.get(entry["kind"], "Other"),
        "source": entry.get("source"), "family": entry.get("family"),
        "task": entry.get("task"), "runtime": entry.get("runtime"),
        "license": entry.get("license"), "gated": bool(entry.get("gated")),
        "downloads": entry.get("downloads"), "likes": entry.get("likes"),
        "updated_at": entry.get("updated_at"), "created_at": entry.get("created_at"),
        "params": entry.get("params"), "url": entry.get("url"),
        "curated": bool(entry.get("curated")), "is_new": is_new,
        "variant_count": len(entry["variants"]),
        "description": entry.get("description"),
        "lead": {"id": lead.get("id"), "label": lead.get("label"),
                 "precision": lead.get("precision"), "format": lead.get("format"),
                 "bytes": lead.get("bytes"), "download_url": lead.get("download_url"),
                 "direct": bool(lead.get("direct")), "save_path": lead.get("save_path")},
        "fit": {k: assessment[k] for k in (
            "verdict", "label", "runs", "speed", "need_bytes", "used_pct_of_safe",
            "used_pct_of_ceiling", "optimum_variant", "maximum_variant",
            "stretch_variant", "inferred")},
        "fit_reason": (assessment["reasons"] or [None])[0],
        "security": {k: score[k] for k in ("score", "grade", "label", "tone", "inferred")},
        "rank": round(_rank(entry, assessment["verdict"], score["score"], is_new)
                      - (12 if entry["kind"] == "llm"
                         and fit.low_quant(lead.get("precision")) else 0), 2),
        "maximum": _pick(entry, assessment, assessment["maximum_variant"]),
        "stretch": _pick(entry, assessment, assessment.get("stretch_variant")),
    }


def _invalidate() -> None:
    global _summary_cache
    with _cache_lock:
        _summary_cache = None


def _summaries() -> list[dict]:
    """Every catalog entry assessed for this machine.  Cached per budget."""
    global _summary_cache
    _profile, budget, _prefs = context()
    cat = catalog.load()
    key = (cat["generated_at"], cat["origin"], len(cat["entries"]),
           tuple(sorted(budget.as_dict().items())))
    with _cache_lock:
        if _summary_cache and _summary_cache[0] == key:
            return _summary_cache[1]
    first_seen = catalog.load_state().get("first_seen") or {}
    now = dbmod.now_ms()
    rows = [_summary(e, budget, first_seen, now) for e in cat["entries"]
            if not e.get("nsfw")]
    with _cache_lock:
        _summary_cache = (key, rows)
    return rows


def _sort(rows: list[dict], sort: str) -> list[dict]:
    if sort == "name":
        return sorted(rows, key=lambda r: r["name"].lower())
    if sort == "-downloads":
        return sorted(rows, key=lambda r: r["downloads"] or 0, reverse=True)
    if sort == "-updated":
        return sorted(rows, key=lambda r: r["updated_at"] or "", reverse=True)
    if sort in ("need", "-need"):
        return sorted(rows, key=lambda r: r["fit"]["need_bytes"] or 0,
                      reverse=sort.startswith("-"))
    if sort in ("security", "-security"):
        return sorted(rows, key=lambda r: r["security"]["score"],
                      reverse=sort.startswith("-"))
    if sort in ("size", "-size"):
        return sorted(rows, key=lambda r: r["lead"]["bytes"] or 0,
                      reverse=sort.startswith("-"))
    return sorted(rows, key=lambda r: r["rank"], reverse=True)


def _facet(rows: list[dict], getter, order: tuple = ()) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        key = getter(row)
        if key:
            counts[key] = counts.get(key, 0) + 1
    keys = [k for k in order if k in counts] + sorted(k for k in counts if k not in order)
    return [{"value": k, "count": counts[k]} for k in keys]


def list_entries(*, kinds: list[str] | None = None, verdicts: list[str] | None = None,
                 source_ids: list[str] | None = None, q: str | None = None,
                 runs_only: bool = False, new_only: bool = False,
                 min_security: int | None = None, sort: str = DEFAULT_SORT,
                 limit: int = 100, offset: int = 0) -> dict:
    if sort not in SORTS:
        raise ValidationError(f"sort: unknown key '{sort}'",
                              details={"field": "sort", "allowed": list(SORTS)})
    rows = _summaries()
    prefs = settings()
    scope = kinds or prefs["kinds"] or None
    needle = (q or "").strip().lower()

    def matches(row: dict, *, skip: str | None = None) -> bool:
        if skip != "kind" and scope and row["kind"] not in scope:
            return False
        if skip != "verdict" and verdicts and row["fit"]["verdict"] not in verdicts:
            return False
        if skip != "source" and source_ids and row["source"] not in source_ids:
            return False
        if runs_only and not (row["fit"]["runs"] or row["fit"]["verdict"] == "base"):
            return False
        if new_only and not row["is_new"]:
            return False
        if min_security is not None and row["security"]["score"] < min_security:
            return False
        if needle:
            hay = " ".join(str(row.get(k) or "") for k in (
                "name", "publisher", "family", "task", "license", "id")).lower()
            if needle not in hay:
                return False
        return True

    filtered = [r for r in rows if matches(r)]
    ordered = _sort(filtered, sort)
    limit = max(1, min(MAX_LIMIT, int(limit)))
    offset = max(0, int(offset))
    page = ordered[offset:offset + limit]
    return {
        "items": page,
        "page": {"limit": limit, "offset": offset, "total": len(ordered),
                 "returned": len(page), "has_more": offset + len(page) < len(ordered)},
        "facets": {
            "kind": [{**f, "label": KIND_LABELS.get(f["value"], f["value"])}
                     for f in _facet([r for r in rows if matches(r, skip="kind")],
                                     lambda r: r["kind"], KIND_ORDER)],
            "verdict": [{**f, "label": fit.VERDICT_LABEL.get(f["value"], f["value"])}
                        for f in _facet([r for r in rows if matches(r, skip="verdict")],
                                        lambda r: r["fit"]["verdict"], fit.VERDICT_ORDER)],
            "source": _facet([r for r in rows if matches(r, skip="source")],
                             lambda r: r["source"]),
        },
        "meta": {"sort": sort, "scope": scope, "catalog_total": len(rows),
                 "inferred": True},
    }


def entry_detail(entry_id: str) -> dict:
    entry = catalog.get(entry_id)
    if entry is None:
        raise NotFoundError("That model is not in the catalog.",
                            details={"id": entry_id})
    _profile, budget, _prefs = context()
    assessment = fit.assess_entry(entry, budget)
    by_id = {v["id"]: v for v in assessment["variants"]}
    variants = [{**variant, "fit": by_id.get(variant.get("id")),
                 "security": {k: v for k, v in security.score_entry(
                     entry, variant).items() if k != "factors"}}
                for variant in entry["variants"]]
    lead = _variant(entry, assessment["lead_variant"]) or entry["variants"][0]
    first_seen = (catalog.load_state().get("first_seen") or {}).get(entry_id) or 0
    installed = _installed_matches(entry)
    return {
        **{k: v for k, v in entry.items() if k != "variants"},
        "kind_label": KIND_LABELS.get(entry["kind"], "Other"),
        "variants": variants,
        "fit": {k: v for k, v in assessment.items() if k != "variants"},
        "security": security.score_entry(entry, lead),
        "budget": budget.as_dict(),
        "first_seen_at": first_seen or None,
        "is_new": bool(first_seen and dbmod.now_ms() - int(first_seen) < NEW_WINDOW_MS),
        "installed": installed,
    }


def verify_link(entry_id: str, variant_id: str | None) -> dict:
    """Ask the host whether a stored download link is still live."""
    updater._require_online()
    entry = catalog.get(entry_id)
    if entry is None:
        raise NotFoundError("That model is not in the catalog.", details={"id": entry_id})
    variant = _variant(entry, variant_id) if variant_id else entry["variants"][0]
    if variant is None:
        raise NotFoundError("That variant is not in the catalog.",
                            details={"variant": variant_id})
    url = variant.get("download_url")
    if not url:
        return {"state": "unchecked", "status": None, "url": None,
                "message": "This variant has no direct download link."}
    result = sources.head_status(url)
    result["url"] = url
    result["checked_at"] = dbmod.now_ms()
    expected = variant.get("bytes")
    if result.get("bytes") and expected:
        result["size_matches"] = abs(int(result["bytes"]) - int(expected)) <= max(
            1024, int(expected) // 1000)
    return result


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def recommendations(*, kinds: list[str] | None = None,
                    per_kind: int = RECOMMEND_PER_KIND) -> dict:
    profile, budget, prefs = context()
    rows = _summaries()
    scope = kinds or prefs["kinds"] or list(KIND_ORDER)
    per_kind = max(1, min(20, int(per_kind)))
    groups = []
    totals = dict.fromkeys(fit.VERDICT_ORDER, 0)
    for kind in [k for k in KIND_ORDER if k in scope]:
        pool = [r for r in rows if r["kind"] == kind]
        if not pool:
            continue
        counts: dict[str, int] = {}
        for row in pool:
            verdict = row["fit"]["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
            totals[verdict] = totals.get(verdict, 0) + 1
        addon = kind in fit.ADDON_KINDS
        if addon:
            optimum = sorted(pool, key=lambda r: r["rank"], reverse=True)[:per_kind]
            maximum, stretch = [], []
        else:
            optimum = sorted((r for r in pool if r["fit"]["verdict"] in fit.COMFORTABLE),
                             key=lambda r: r["rank"], reverse=True)[:per_kind]
            # "Maximum" is the heaviest variant that still runs on the GPU;
            # "stretch" is heavier still and only runs by offloading.
            gpu_verdicts = fit.ON_GPU if budget.backend != "cpu" else fit.RUNS
            maximum = sorted(
                (r for r in pool if r["maximum"]
                 and r["maximum"]["verdict"] in gpu_verdicts),
                key=lambda r: (r["maximum"]["need_bytes"], r["rank"]),
                reverse=True)[:3]
            stretch = sorted(
                (r for r in pool if r["stretch"]),
                key=lambda r: (r["stretch"]["need_bytes"], r["rank"]),
                reverse=True)[:2]
        groups.append({"kind": kind, "label": KIND_LABELS[kind], "addon": addon,
                       "total": len(pool), "counts": counts,
                       "optimum": optimum, "maximum": maximum, "stretch": stretch})
    runnable_total = sum(totals.get(v, 0) for v in fit.RUNS)
    return {
        "tier": profile.get("tier"),
        "accelerator": profile.get("accelerator"),
        "budget": budget.as_dict(),
        "scope": [k for k in KIND_ORDER if k in scope],
        "totals": totals,
        "runnable": runnable_total,
        "catalog_total": len(rows),
        "groups": groups,
        "inferred": True,
    }


# ---------------------------------------------------------------------------
# Installed models (already in the vault's index)
# ---------------------------------------------------------------------------

def _local_kind(row) -> str:
    kind = CATEGORY_KIND.get(str(row["category"] or ""), "other")
    modality = str(row["modality"] or "").lower()
    if kind == "image" and modality == "video":
        return "video"
    if kind == "image" and modality == "audio":
        return "audio"
    if row["is_adapter"] and kind in ("image", "other"):
        return "lora"
    return kind


def _catalog_by_filename() -> dict[str, tuple[dict, dict]]:
    index: dict[str, tuple[dict, dict]] = {}
    for entry in catalog.load()["entries"]:
        for variant in entry.get("variants") or []:
            name = str(variant.get("file") or "").rsplit("/", 1)[-1].lower()
            if name and name not in index:
                index[name] = (entry, variant)
    return index


def _local_row(row, budget: fit.Budget, by_file: dict) -> dict:
    kind = _local_kind(row)
    size = int(row["total_size"] or row["file_size"] or 0)
    match = by_file.get(str(row["filename"] or "").lower())
    companions = match[0].get("companions") if match and not row["is_bundled"] else None
    need = fit.requirement(kind, size, companions=companions,
                           runtime="gguf" if str(row["ext"] or "").lower().endswith("gguf")
                           else None, bundled=bool(row["is_bundled"]))
    assessment = fit.assess(kind, need, budget)
    notes = list(assessment["reasons"])
    if kind in ("image", "video") and not row["is_bundled"] and not companions:
        notes.append("This is the diffusion model only: add its text encoder "
                     "and VAE, which load around it.")
    score = security.score_local({
        "format": str(row["ext"] or "").lstrip("."), "integrity": row["integrity"],
        "hash_state": row["hash_state"], "civitai_state": row["civitai_state"],
        "download_url": row["civitai_url"]})
    return {
        "uid": f"model:{row['id']}", "id": int(row["id"]), "name": row["name"],
        "filename": row["filename"], "category": row["category"],
        "kind": kind, "kind_label": KIND_LABELS.get(kind, "Other"),
        "base_model": row["base_model_family"], "precision": row["precision"],
        "quantization": row["quantization"], "params": row["param_count_primary"],
        "size": size, "folder": row["folder"] or "", "is_bundled": bool(row["is_bundled"]),
        "missing": row["missing_since"] is not None,
        "fit": {k: assessment[k] for k in (
            "verdict", "label", "runs", "speed", "need_bytes", "safe_bytes",
            "ceiling_bytes", "used_pct_of_safe", "used_pct_of_ceiling",
            "headroom_bytes", "inferred")},
        "requirement": need,
        "reasons": notes,
        "security": score,
        "catalog_id": match[0]["id"] if match else None,
        "catalog_name": match[0]["name"] if match else None,
    }


def _installed_matches(entry: dict) -> list[dict]:
    """Indexed files whose name matches one of this entry's variants."""
    names = [str(v.get("file") or "").rsplit("/", 1)[-1] for v in entry.get("variants") or []]
    names = [n for n in names if n][:60]
    if not names:
        return []
    try:
        conn = dbmod.get_ro()
        marks = ",".join("?" for _ in names)
        found = dbmod.rows(
            conn, f"SELECT id, name, filename, folder FROM v_model_list "  # noqa: S608
                  f"WHERE missing_since IS NULL AND filename IN ({marks}) LIMIT 20",
            tuple(names))
    except Exception:  # noqa: BLE001 - no index yet is not an error here
        return []
    return [{"uid": f"model:{r['id']}", "name": r["name"], "filename": r["filename"],
             "folder": r["folder"] or ""} for r in found]


def installed(*, kinds: list[str] | None = None, verdicts: list[str] | None = None,
              q: str | None = None, sort: str = "-need", limit: int = 200,
              offset: int = 0) -> dict:
    _profile, budget, _prefs = context()
    try:
        conn = dbmod.get_ro()
        rows = dbmod.rows(conn, "SELECT * FROM v_model_list WHERE missing_since IS NULL")
    except Exception:  # noqa: BLE001 - an unindexed vault simply has no rows
        rows = []
    by_file = _catalog_by_filename()
    items = [_local_row(r, budget, by_file) for r in rows]
    needle = (q or "").strip().lower()

    def keep(item: dict, *, skip: str | None = None) -> bool:
        if skip != "kind" and kinds and item["kind"] not in kinds:
            return False
        if skip != "verdict" and verdicts and item["fit"]["verdict"] not in verdicts:
            return False
        return not needle or needle in (
            f"{item['name']} {item['filename']} {item['base_model']} {item['category']}".lower())

    filtered = [i for i in items if keep(i)]
    if sort == "name":
        filtered.sort(key=lambda i: i["name"].lower())
    elif sort in ("security", "-security"):
        filtered.sort(key=lambda i: i["security"]["score"], reverse=sort.startswith("-"))
    elif sort in ("size", "-size"):
        filtered.sort(key=lambda i: i["size"], reverse=sort.startswith("-"))
    elif sort in ("need", "-need"):
        filtered.sort(key=lambda i: i["fit"]["need_bytes"], reverse=sort.startswith("-"))
    else:
        raise ValidationError(f"sort: unknown key '{sort}'", details={"field": "sort"})
    limit = max(1, min(MAX_LIMIT, int(limit)))
    offset = max(0, int(offset))
    page = filtered[offset:offset + limit]
    counts: dict[str, int] = {}
    for item in [i for i in items if keep(i, skip="verdict")]:
        counts[item["fit"]["verdict"]] = counts.get(item["fit"]["verdict"], 0) + 1
    kind_counts: dict[str, int] = {}
    for item in [i for i in items if keep(i, skip="kind")]:
        kind_counts[item["kind"]] = kind_counts.get(item["kind"], 0) + 1
    return {
        "items": page,
        "page": {"limit": limit, "offset": offset, "total": len(filtered),
                 "returned": len(page), "has_more": offset + len(page) < len(filtered)},
        "facets": {
            "verdict": [{"value": v, "label": fit.VERDICT_LABEL[v], "count": counts[v]}
                        for v in fit.VERDICT_ORDER if v in counts],
            "kind": [{"value": k, "label": KIND_LABELS[k], "count": kind_counts[k]}
                     for k in KIND_ORDER if k in kind_counts],
        },
        "meta": {"sort": sort, "indexed_total": len(items), "inferred": True},
    }


def installed_detail(model_id: int) -> dict:
    _profile, budget, _prefs = context()
    try:
        conn = dbmod.get_ro()
        row = dbmod.one(conn, "SELECT * FROM v_model_list WHERE id = ?", (int(model_id),))
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        raise NotFoundError("That model is not in the index.", details={"id": model_id})
    item = _local_row(row, budget, _catalog_by_filename())
    item["budget"] = budget.as_dict()
    item["abs_path"] = row["abs_path"]
    item["hash_state"] = row["hash_state"]
    item["integrity"] = row["integrity"]
    return item


# ---------------------------------------------------------------------------
# Sources and overview
# ---------------------------------------------------------------------------

def sources_report() -> dict:
    defs = catalog.sources()
    state = catalog.load_state()
    cat = catalog.load()
    count = catalog.counts(cat["entries"])
    rows = []
    for source in defs["sources"]:
        run = (state.get("sources") or {}).get(source.get("id")) or {}
        entries = len(cat.get("leads") or []) if source.get("id") == "github" \
            else count["by_source"].get(source.get("id"), 0)
        rows.append({**source,
                     "entries": entries,
                     "last_ok": run.get("ok"), "last_at": run.get("at"),
                     "last_error": run.get("error"),
                     "last_elapsed_ms": run.get("elapsed_ms")})
    from ..enable import hosts

    return {
        "revision": defs["revision"], "origin": defs["origin"],
        "sources": rows,
        "pins": len((defs.get("pins") or {}).get("huggingface") or []),
        "user_pins": defs["user_pins"],
        "learned": defs["learned"][-60:],
        "learned_total": len(defs["learned"]),
        "queries": {k: (len(v) if isinstance(v, list) else 1)
                    for k, v in (defs.get("discovery") or {}).items()},
        "publishers": {k: len(v) for k, v in (defs.get("publishers") or {}).items()},
        "allowed_hosts": hosts.describe().get("catalog_hosts", []),
        "leads": cat.get("leads") or [],
        "counts": count,
        "update": updater.status(),
    }


def overview() -> dict:
    profile, budget, prefs = context()
    rows = _summaries()
    scope = prefs["kinds"] or None
    scoped = [r for r in rows if not scope or r["kind"] in scope]
    totals: dict[str, int] = {}
    for row in scoped:
        totals[row["fit"]["verdict"]] = totals.get(row["fit"]["verdict"], 0) + 1
    return {
        "tier": profile.get("tier"), "accelerator": profile.get("accelerator"),
        "cpu": profile.get("cpu"), "memory": profile.get("memory"),
        "os": profile.get("os"), "notes": profile.get("notes"),
        "budget": budget.as_dict(), "settings": prefs,
        "totals": totals, "catalog_total": len(scoped),
        "new": sum(1 for r in scoped if r["is_new"]),
        "update": updater.status(),
    }
