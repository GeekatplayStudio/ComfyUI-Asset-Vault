"""Tool catalogue: pure data.  26 tools.

The 13 in MCP_SPEC 5, the 8 file-operation tools in DECISIONS C5.2, and the
three job-control tools C5.2 promotes to writable (``vault_hash_enqueue``,
``vault_hash_cancel``, ``vault_embeddings_rebuild``) as first-class entries -
discoverability through ``tools/list`` is the whole point of a tool schema, so
they are not hidden behind arguments on another tool.

Every ``inputSchema`` / ``outputSchema`` is JSON Schema draft-07, declares
``"additionalProperties": false`` and carries ``$schema`` (MCP_SPEC 4).

Handlers are referenced by name and resolved from ``handlers.HANDLERS`` so this
module stays import-free and cheap - the stdio transport pays for it on every
cold start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DRAFT = "http://json-schema.org/draft-07/schema#"

UID_PATTERN = "^(model|node_package|node_class|workflow|output):[0-9]+$"

# --- frozen vocabularies (API_CONTRACT 16) -----------------------------------
KINDS = ("model", "node_package", "node_class", "workflow", "output")
MEDIA_KINDS = ("image", "video", "audio", "model3d", "text", "other")
MODALITIES = ("image", "video", "audio", "3d", "multimodal", "text", "unknown")
PRECISIONS = ("fp32", "fp16", "bf16", "fp8", "int8", "int4", "mixed", "unknown")
HASH_STATES = ("unhashed", "queued", "hashing", "done", "failed", "stale")
INTEGRITY_STATES = ("ok", "invalid_header", "not_a_model", "truncated", "unreadable",
                    "unsupported_format")
CONFIDENCES = ("declared", "inferred", "registry")
SCAN_PHASES = ("roots", "walk", "models", "nodes", "workflows", "outputs", "links",
               "index", "prune")

MAX_BATCH = 200

# --- schema shorthands -------------------------------------------------------
STR: dict[str, Any] = {"type": "string"}
INT: dict[str, Any] = {"type": "integer"}
NUM: dict[str, Any] = {"type": "number"}
BOOL: dict[str, Any] = {"type": "boolean"}
NSTR: dict[str, Any] = {"type": ["string", "null"]}
NINT: dict[str, Any] = {"type": ["integer", "null"]}
NNUM: dict[str, Any] = {"type": ["number", "null"]}
STRS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
TRUNCATED: dict[str, Any] = {"type": "boolean",
                             "description": "True when the payload was reduced to stay "
                                            "under the 512 KB structured-content cap."}


def uid(desc: str) -> dict:
    return {"type": "string", "pattern": UID_PATTERN, "description": desc}


def enum_array(values, desc: str) -> dict:
    return {"type": "array", "items": {"enum": list(values)}, "description": desc}


def limit(default: int = 25, maximum: int = 200) -> dict:
    return {"type": "integer", "minimum": 1, "maximum": maximum, "default": default}


OFFSET = {"type": "integer", "minimum": 0, "default": 0}


def schema(properties: dict, required: list[str] | None = None) -> dict:
    out: dict[str, Any] = {"$schema": DRAFT, "type": "object",
                           "properties": properties, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def item_schema(properties: dict, required: list[str] | None = None) -> dict:
    out: dict[str, Any] = {"type": "object", "properties": properties,
                           "additionalProperties": False}
    if required:
        out["required"] = required
    return out


# --- annotations -------------------------------------------------------------
READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
        "openWorldHint": False}
WRITE_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": False, "openWorldHint": False}
WRITE_SAFE = {"readOnlyHint": False, "destructiveHint": False,
              "idempotentHint": False, "openWorldHint": False}
JOB_CONTROL = {"readOnlyHint": False, "destructiveHint": False,
               "idempotentHint": True, "openWorldHint": False}

PAGE_OUT = {"total": INT, "returned": INT, "offset": OFFSET, "truncated": TRUNCATED}


@dataclass(frozen=True)
class ToolDef:
    name: str
    title: str
    description: str
    input_schema: dict
    output_schema: dict
    annotations: dict
    handler: str
    mutating: bool = False
    #: A mutating call that may destroy data needs the audit row + batch cap.
    audited: bool = False
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


# =============================================================================
# 5.1  vault_search
# =============================================================================
_SEARCH_HIT = item_schema({
    "uid": STR, "kind": STR, "title": STR, "subtitle": NSTR, "snippet": NSTR,
    "score": NUM, "matched": STRS, "path": NSTR,
}, ["uid", "kind", "title", "score"])

VAULT_SEARCH = ToolDef(
    name="vault_search",
    title="Search the vault",
    description=(
        "Search models, node packages, node classes, workflows and outputs by meaning "
        "and keyword. Use this for open-ended questions like 'a lora for anime style on "
        "FLUX' or 'workflows that do video upscaling'. Falls back to keyword-only search "
        "when the semantic index is unavailable."),
    input_schema=schema({
        "query": {"type": "string", "minLength": 1, "maxLength": 512,
                  "description": "Natural-language or keyword query."},
        "kinds": enum_array(KINDS, "Restrict to these asset kinds. Omit for all."),
        "semantic": {"type": "boolean", "default": True,
                     "description": "Use hybrid semantic ranking. Ignored (treated as "
                                    "false) when the embedding model is not installed."},
        "limit": limit(20, 100),
        "offset": OFFSET,
    }, ["query"]),
    output_schema=schema({
        "query": STR,
        "mode": {"enum": ["lexical", "hybrid"]},
        "semantic_available": BOOL,
        "semantic_unavailable_reason": NSTR,
        "total": INT,
        "results": {"type": "array", "items": _SEARCH_HIT},
        "truncated": TRUNCATED,
    }, ["query", "mode", "semantic_available", "total", "results"]),
    annotations=READ,
    handler="vault_search",
)

# =============================================================================
# 5.2  list_models
# =============================================================================
_MODEL_ROW = item_schema({
    "uid": STR, "name": STR, "filename": STR, "category": STR, "role": STR,
    "base_model": STR, "base_model_confidence": NUM, "modality": STR,
    "precision": NSTR, "params_display": NSTR, "size_bytes": INT,
    "hash_state": STR, "autov2": NSTR, "integrity": STR, "has_update": BOOL,
    "workflow_count": INT, "output_count": INT, "rel_path": STR,
}, ["uid", "name", "category", "role", "base_model", "size_bytes", "hash_state"])

LIST_MODELS = ToolDef(
    name="list_models",
    title="List models",
    description=(
        "List and filter installed models (checkpoints, LoRAs, VAEs, text encoders, "
        "ControlNets, upscalers, embeddings). Returns summary rows; call get_model for "
        "full detail."),
    input_schema=schema({
        "category": {"type": "array", "items": STR,
                     "description": "Folder categories, e.g. checkpoints, loras, vae, "
                                    "diffusion_models, text_encoders, controlnet."},
        "base_model": {"type": "array", "items": STR,
                       "description": "e.g. SDXL, FLUX.1, WAN, SD1.5."},
        "role": {"type": "array", "items": STR},
        "modality": enum_array(MODALITIES, "Restrict to these modalities."),
        "precision": {"type": "array", "items": STR},
        "hash_state": enum_array(HASH_STATES, "Restrict to these hash states."),
        "is_adapter": BOOL,
        "has_update": BOOL,
        "integrity_issues_only": {"type": "boolean", "default": False},
        "name_contains": {"type": "string", "maxLength": 512,
                          "description": "Lexical keyword match on the indexed name and "
                                         "metadata."},
        "min_size_bytes": {"type": "integer", "minimum": 0},
        "max_size_bytes": {"type": "integer", "minimum": 0},
        "sort": {"enum": ["name", "-name", "size", "-size", "modified", "-modified",
                          "params", "-params"], "default": "name"},
        "limit": limit(50, 200),
        "offset": OFFSET,
    }),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT, "total_bytes": INT,
        "models": {"type": "array", "items": _MODEL_ROW},
        "truncated": TRUNCATED,
    }, ["total", "returned", "models"]),
    annotations=READ,
    handler="list_models",
)

# =============================================================================
# 5.3  get_model
# =============================================================================
GET_MODEL = ToolDef(
    name="get_model",
    title="Model detail",
    description=(
        "Everything known about one model: identity, architecture detection and its "
        "signals, component/tensor breakdown, files on disk, hash state, cached Civitai "
        "metadata, update state, and which workflows and outputs use it. Supply exactly "
        "one of uid or name."),
    input_schema=schema({
        "uid": uid("Model uid, e.g. 'model:41'."),
        "name": {"type": "string", "minLength": 1, "maxLength": 400,
                 "description": "Model name or filename; case-insensitive exact match "
                                "first, then fuzzy."},
    }),
    output_schema=schema({
        "identity": item_schema({
            "uid": STR, "name": STR, "filename": NSTR, "category": NSTR, "role": NSTR,
            "base_model": NSTR, "base_model_variant": NSTR, "modality": NSTR,
            "architecture": NSTR, "is_adapter": BOOL, "size_bytes": INT,
            "modified_at": NINT, "rel_path": NSTR,
        }),
        "technical": item_schema({
            "tensor_count": NINT, "format": NSTR, "precision": NSTR,
            "quantization": NSTR, "prediction_type": NSTR, "resolution_hint": NSTR,
            "params_display": NSTR,
            "components": {"type": "array", "items": item_schema(
                {"name": STR, "params": INT, "dtype": NSTR, "share": NUM})},
            "detection": item_schema(
                {"source": NSTR, "confidence": NUM, "signals": STRS}),
        }),
        "files": {"type": "array", "items": item_schema({
            "abs_path": NSTR, "rel_path": NSTR, "size_bytes": INT,
            "modified_at": NINT, "format": NSTR, "hash_state": NSTR,
        })},
        "hash": item_schema({"state": STR, "sha256": NSTR, "autov2": NSTR}),
        "civitai": item_schema({
            "state": NSTR, "reason": NSTR, "note": NSTR, "url": NSTR,
            "description": NSTR, "trigger_words": STRS,
            "recommended_settings": {"type": ["object", "null"]}, "rating": NNUM,
        }),
        "update": item_schema({"has_update": BOOL, "latest_version_name": NSTR,
                               "benefits": NSTR}),
        "usage_notes": NSTR,
        "download": item_schema({"url": NSTR, "source": NSTR}),
        "usage": item_schema({
            "workflow_count": INT, "output_count": INT,
            "workflows": {"type": "array", "items": item_schema(
                {"uid": STR, "name": NSTR, "rel_path": NSTR, "occurrences": INT})},
        }),
        "integrity": item_schema({"status": NSTR, "note": NSTR}),
        "tags": STRS,
        "abs_path": NSTR,
    }, ["identity", "hash", "usage"]),
    annotations=READ,
    handler="get_model",
)

# =============================================================================
# 5.4  list_node_packages
# =============================================================================
_PACKAGE_ROW = item_schema({
    "uid": STR, "folder_name": NSTR, "display_name": NSTR, "author": NSTR,
    "description": NSTR, "is_official": BOOL, "enabled": BOOL, "class_count": INT,
    "extraction": item_schema({"status": NSTR, "strategies": STRS,
                               "confidence": NSTR}),
    "repo": item_schema({"url": NSTR, "suspect": BOOL, "branch": NSTR,
                         "commit": NSTR, "commit_at": NINT}),
    "update": item_schema({"state": NSTR, "has_update": BOOL,
                           "commits_behind": NINT}),
    "version": NSTR,
    "deps": {"type": "array", "items": STR},
    "workflow_count": INT,
}, ["uid", "display_name", "class_count"])

LIST_NODE_PACKAGES = ToolDef(
    name="list_node_packages",
    title="List node packages",
    description=(
        "List installed custom-node packages plus the synthetic '__comfyui_core__' "
        "package that holds the built-in node classes. Shows extraction confidence, git "
        "remote, update state and Python dependency health."),
    input_schema=schema({
        "official": BOOL,
        "enabled": BOOL,
        "has_update": BOOL,
        "author": {"type": "array", "items": STR},
        "name_contains": {"type": "string", "maxLength": 512},
        "extraction_status": {"type": "array", "items": STR},
        "sort": {"enum": ["name", "-classes", "-updated"], "default": "name"},
        "limit": limit(50, 200),
        "offset": OFFSET,
    }),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT,
        "packages": {"type": "array", "items": _PACKAGE_ROW},
        "truncated": TRUNCATED,
    }, ["total", "returned", "packages"]),
    annotations=READ,
    handler="list_node_packages",
)

# =============================================================================
# 5.5  list_node_classes
# =============================================================================
_CLASS_ROW = item_schema({
    "uid": STR, "node_id": STR, "display_name": NSTR, "class_name": NSTR,
    "category": NSTR, "description": NSTR,
    "package": item_schema({"uid": NSTR, "name": NSTR, "official": BOOL}),
    "inputs": item_schema({"required": {"type": "object"},
                           "optional": {"type": "object"}}),
    "outputs": item_schema({"types": {"type": ["array", "null"], "items": STR},
                            "names": {"type": ["array", "null"], "items": STR}}),
    "output_node": BOOL,
    "flags": item_schema({"deprecated": BOOL, "experimental": BOOL,
                          "api_node": BOOL}),
    "confidence": NSTR,
    "source": item_schema({"file": NSTR, "lineno": NINT, "strategy": NSTR}),
    "workflow_count": INT,
}, ["uid", "node_id"])

LIST_NODE_CLASSES = ToolDef(
    name="list_node_classes",
    title="List node classes",
    description=(
        "List node class signatures - required/optional inputs, output types and names, "
        "category and provenance. This is the tool to use when authoring or validating a "
        "ComfyUI graph, because it exposes every installed node's exact signature."),
    input_schema=schema({
        "package_uid": {"type": "string", "pattern": "^node_package:[0-9]+$"},
        "category": {"type": "array", "items": STR},
        "name_contains": {"type": "string", "maxLength": 512},
        "official": BOOL,
        "include_deprecated": {"type": "boolean", "default": False},
        "confidence": enum_array(CONFIDENCES, "Extraction confidence."),
        "limit": limit(50, 200),
        "offset": OFFSET,
    }),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT,
        "classes": {"type": "array", "items": _CLASS_ROW},
        "truncated": TRUNCATED,
    }, ["total", "returned", "classes"]),
    annotations=READ,
    handler="list_node_classes",
)

# =============================================================================
# 5.6  get_node_class
# =============================================================================
GET_NODE_CLASS = ToolDef(
    name="get_node_class",
    title="Node class detail",
    description=(
        "Full record for one node class by its ComfyUI class id (the 'class_type' that "
        "appears in a workflow graph), plus the workflows that use it and sibling "
        "classes in the same category. If the id exists in more than one package every "
        "match is returned in 'disambiguation'."),
    input_schema=schema({
        "node_id": {"type": "string", "minLength": 1, "maxLength": 200,
                    "description": "ComfyUI class id, e.g. 'KSampler'."},
        "package_uid": {"type": "string", "pattern": "^node_package:[0-9]+$"},
    }, ["node_id"]),
    output_schema=schema({
        "node_class": {"type": ["object", "null"]},
        "disambiguation": {"type": "array", "items": item_schema(
            {"uid": STR, "node_id": STR, "package": NSTR, "official": BOOL})},
        "workflows_using": {"type": "array", "items": item_schema(
            {"uid": STR, "name": NSTR, "occurrences": INT})},
        "similar_classes": {"type": "array", "items": item_schema(
            {"uid": STR, "node_id": STR, "display_name": NSTR, "category": NSTR})},
        "truncated": TRUNCATED,
    }, ["node_class", "workflows_using", "similar_classes"]),
    annotations=READ,
    handler="get_node_class",
)

# =============================================================================
# 5.7  list_workflows
# =============================================================================
_WORKFLOW_ROW = item_schema({
    "uid": STR, "name": STR, "rel_path": NSTR, "folder": NSTR, "format": NSTR,
    "title": NSTR, "description": NSTR, "capability_tags": STRS,
    "base_model": NSTR, "modality": NSTR, "node_count": INT,
    "missing_node_count": INT, "missing_model_count": INT, "is_runnable": BOOL,
    "prompt_summary": NSTR, "modified_at": NINT, "size_bytes": INT,
}, ["uid", "name", "is_runnable"])

LIST_WORKFLOWS = ToolDef(
    name="list_workflows",
    title="List workflows",
    description=(
        "List indexed workflow files with their capability tags, node counts and "
        "runnability. Filter by the node class or model a workflow uses to answer "
        "'which workflows use X'."),
    input_schema=schema({
        "name_contains": {"type": "string", "maxLength": 512},
        "folder": {"type": "string", "maxLength": 1024},
        "base_model": {"type": "array", "items": STR},
        "runnable": BOOL,
        "uses_node_class": {"type": "string", "maxLength": 200},
        "uses_model_uid": {"type": "string", "pattern": "^model:[0-9]+$"},
        "capability_tag": {"type": "string", "maxLength": 100},
        "sort": {"enum": ["name", "-name", "modified", "-modified", "size", "-size",
                          "nodes", "-nodes", "missing", "-missing"],
                 "default": "-modified"},
        "limit": limit(50, 200),
        "offset": OFFSET,
    }),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT,
        "workflows": {"type": "array", "items": _WORKFLOW_ROW},
        "truncated": TRUNCATED,
    }, ["total", "returned", "workflows"]),
    annotations=READ,
    handler="list_workflows",
)

# =============================================================================
# 5.8  inspect_workflow
# =============================================================================
_INSTALL_HINT = item_schema({"package": NSTR, "repo_url": NSTR, "source": STR})

INSPECT_WORKFLOW = ToolDef(
    name="inspect_workflow",
    title="Inspect a workflow",
    description=(
        "The headline tool: resolve one workflow's full dependency picture - prompts, "
        "node classes with their owning package, referenced model files with "
        "suggestions for near misses, and an install_hint from the ComfyUI-Manager "
        "registry for every node class that is not installed. Answers 'can I run this, "
        "and what do I need to install?'"),
    input_schema=schema({
        "uid": {"type": "string", "pattern": "^workflow:[0-9]+$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 400},
        "include_graph": {"type": "boolean", "default": False},
        "max_nodes": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
    }),
    output_schema=schema({
        "workflow": item_schema({
            "uid": STR, "name": STR, "rel_path": NSTR, "format": NSTR,
            "description": NSTR, "capability_tags": STRS, "base_model": NSTR,
            "modality": NSTR, "node_count": INT, "is_runnable": BOOL,
        }, ["uid", "name"]),
        "prompts": item_schema({"positive": NSTR, "negative": NSTR,
                                "unresolved_count": INT}),
        "nodes": {"type": "array", "items": item_schema({
            "class_type": STR, "count": INT, "status": STR,
            "package": {"type": ["object", "null"]},
        })},
        "dependencies": item_schema({
            "summary": item_schema({"total": INT, "satisfied": INT, "missing": INT,
                                    "ambiguous": INT, "unknown": INT}),
            "models": {"type": "array", "items": item_schema({
                "ref_name": NSTR, "category": NSTR, "via_class": NSTR,
                "via_input": NSTR, "status": STR, "uid": NSTR, "occurrences": INT,
                "suggestions": {"type": "array", "items": item_schema(
                    {"uid": STR, "name": NSTR, "similarity": NUM})},
            })},
            "nodes": {"type": "array", "items": item_schema({
                "class_type": NSTR, "status": STR, "uid": NSTR, "occurrences": INT,
                "package": {"type": ["object", "null"]},
                "install_hint": {"anyOf": [_INSTALL_HINT, {"type": "null"}]},
            })},
            "embeddings": {"type": "array", "items": item_schema(
                {"ref_name": NSTR, "status": STR})},
            "input_files": {"type": "array", "items": item_schema(
                {"ref_name": NSTR, "status": STR})},
        }),
        "can_run": BOOL,
        "blockers": STRS,
        "graph": {"type": ["object", "null"]},
        "truncated": TRUNCATED,
    }, ["workflow", "dependencies", "can_run", "blockers"]),
    annotations=READ,
    handler="inspect_workflow",
)

# =============================================================================
# 5.9  find_model_usage
# =============================================================================
FIND_MODEL_USAGE = ToolDef(
    name="find_model_usage",
    title="Find model usage",
    description=(
        "Where is this model actually used? Returns the workflows that reference it "
        "(with the loader class and input that names it), recent generated outputs that "
        "recorded it, and the models that co-occur with it in the same workflows - the "
        "answer to 'what pairs with this LoRA'."),
    input_schema=schema({
        "uid": {"type": "string", "pattern": "^model:[0-9]+$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 400},
        "include_outputs": {"type": "boolean", "default": True},
        "limit": limit(25, 200),
    }),
    output_schema=schema({
        "model": item_schema({"uid": STR, "name": STR}, ["uid", "name"]),
        "workflows": {"type": "array", "items": item_schema({
            "uid": STR, "name": NSTR, "rel_path": NSTR, "occurrences": INT,
            "via": {"type": "array", "items": item_schema(
                {"class": NSTR, "input": NSTR})},
            "match_method": NSTR,
        })},
        "outputs": item_schema({
            "count": INT,
            "recent": {"type": "array", "items": item_schema(
                {"uid": STR, "filename": NSTR, "created_at": NINT})},
        }),
        "co_occurring_models": {"type": "array", "items": item_schema({
            "uid": NSTR, "ref_name": STR, "shared_workflows": INT})},
        "truncated": TRUNCATED,
    }, ["model", "workflows", "outputs", "co_occurring_models"]),
    annotations=READ,
    handler="find_model_usage",
)

# =============================================================================
# 5.10  query_outputs
# =============================================================================
_OUTPUT_ROW = item_schema({
    "uid": STR, "filename": STR, "rel_path": NSTR, "media_kind": NSTR,
    "width": NINT, "height": NINT, "duration_ms": NINT, "size_bytes": INT,
    "created_at": NINT, "positive_prompt": NSTR, "negative_prompt": NSTR,
    "seed": NSTR, "steps": NINT, "cfg": NNUM, "sampler": NSTR, "scheduler": NSTR,
    "model_name": NSTR, "model_uid": NSTR, "workflow_uid": NSTR,
    "loras": {"type": "array", "items": item_schema(
        {"name": NSTR, "strength": NNUM, "uid": NSTR})},
    "unresolved_fields": STRS,
}, ["uid", "filename"])

QUERY_OUTPUTS = ToolDef(
    name="query_outputs",
    title="Query generated outputs",
    description=(
        "Search generated images, video and audio by their recorded generation "
        "metadata. 'unresolved_fields' names the metadata fields that were graph links "
        "and could not be resolved, so a null prompt means 'was a link', not 'was "
        "empty'. Text filters (prompt_contains, negative_contains, "
        "model_name_contains) are lexical matches against the search index."),
    input_schema=schema({
        "prompt_contains": {"type": "string", "maxLength": 512},
        "negative_contains": {"type": "string", "maxLength": 512},
        "model_uid": {"type": "string", "pattern": "^model:[0-9]+$"},
        "model_name_contains": {"type": "string", "maxLength": 512},
        "workflow_uid": {"type": "string", "pattern": "^workflow:[0-9]+$"},
        "media_kind": enum_array(MEDIA_KINDS, "Restrict to these media kinds."),
        "sampler": {"type": "array", "items": STR},
        "seed": {"type": "string", "maxLength": 64},
        "steps_min": {"type": "integer", "minimum": 0},
        "steps_max": {"type": "integer", "minimum": 0},
        "cfg_min": NUM, "cfg_max": NUM,
        "width_min": {"type": "integer", "minimum": 0},
        "width_max": {"type": "integer", "minimum": 0},
        "height_min": {"type": "integer", "minimum": 0},
        "height_max": {"type": "integer", "minimum": 0},
        "created_after": INT, "created_before": INT,
        "folder": {"type": "string", "maxLength": 1024},
        "favorite": BOOL,
        "min_rating": {"type": "integer", "minimum": 0, "maximum": 5},
        "has_metadata": BOOL,
        "sort": {"enum": ["created", "-created", "modified", "-modified", "name",
                          "-name", "size", "-size", "rating", "-rating", "width",
                          "-width", "height", "-height"], "default": "-created"},
        "limit": limit(25, 200),
        "offset": OFFSET,
    }),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT,
        "outputs": {"type": "array", "items": _OUTPUT_ROW},
        "truncated": TRUNCATED,
    }, ["total", "returned", "outputs"]),
    annotations=READ,
    handler="query_outputs",
)

# =============================================================================
# 5.11  vault_stats
# =============================================================================
_BREAKDOWNS = ("category", "base_model", "modality", "hash_state", "media_kind")

VAULT_STATS = ToolDef(
    name="vault_stats",
    title="Vault statistics",
    description=(
        "Scale and health of the whole installation in one call: counts and bytes for "
        "models, node packages, node classes, workflows and outputs, the configured "
        "roots, the last scan, which optional capabilities are actually available, and "
        "optional breakdowns. Call this first."),
    input_schema=schema({
        "breakdown": enum_array(_BREAKDOWNS, "Extra grouped counts to include."),
    }),
    output_schema=schema({
        "counts": item_schema({
            "models": INT, "model_files": INT, "models_hashed": INT,
            "node_packages": INT, "node_classes": INT, "workflows": INT,
            "workflows_broken": INT, "outputs": INT, "embedded": INT,
            "integrity_issues": INT, "missing": INT, "scan_errors": INT,
        }),
        "disk": item_schema({"models_bytes": INT, "outputs_bytes": INT,
                             "thumb_cache_bytes": INT, "db_bytes": INT}),
        "roots": {"type": "array", "items": item_schema({
            "id": INT, "kind": NSTR, "path": NSTR, "label": NSTR,
            "available": BOOL, "category": NSTR})},
        "last_scan": {"type": ["object", "null"]},
        "capabilities": item_schema({
            "semantic_search": BOOL, "semantic_reason": NSTR, "civitai": BOOL,
            "ollama": BOOL, "hashing_available": BOOL, "mutations_enabled": BOOL}),
        "breakdowns": {"type": "object"},
        "truncated": TRUNCATED,
    }, ["counts", "disk", "roots", "capabilities"]),
    annotations=READ,
    handler="vault_stats",
)

# =============================================================================
# 5.12  get_index_status
# =============================================================================
GET_INDEX_STATUS = ToolDef(
    name="get_index_status",
    title="Index status",
    description=(
        "Live scan / hash / embedding state. Use it to tell 'the vault reports 0 models "
        "because nothing has been indexed yet' from 'the vault genuinely has no "
        "models'."),
    input_schema=schema({}),
    output_schema=schema({
        "configured": BOOL,
        "comfyui_path": NSTR,
        "scan": item_schema({"active": BOOL, "phase": NSTR, "done": INT, "total": INT,
                             "last_completed_at": NINT, "error_count": INT}),
        "hash": item_schema({"queued": INT, "running": INT, "done": INT,
                             "failed": INT, "percent": NUM}),
        "embeddings": item_schema({"state": NSTR, "embedded": INT, "pending": INT,
                                   "reason": NSTR}),
        "health": item_schema({"status": STR, "issues": STRS}),
        "truncated": TRUNCATED,
    }, ["configured", "scan", "hash", "embeddings", "health"]),
    annotations=READ,
    handler="get_index_status",
)

# =============================================================================
# 5.13  vault_reindex
# =============================================================================
VAULT_REINDEX = ToolDef(
    name="vault_reindex",
    title="Reindex the vault",
    description=(
        "Start an index scan so the vault reflects what is on disk right now. "
        "'incremental' re-parses only what changed since the last scan; 'full' "
        "re-parses everything. Non-destructive: scanning never modifies a file. "
        "With wait=false it returns as soon as the job starts; with wait=true it "
        "streams notifications/progress and returns when the scan finishes."),
    input_schema=schema({
        "mode": {"enum": ["incremental", "full"], "default": "incremental"},
        "phases": enum_array(SCAN_PHASES, "Restrict the scan to these phases. Omit "
                                          "to run all of them."),
        "wait": {"type": "boolean", "default": False,
                 "description": "Block until the scan completes."},
    }),
    output_schema=schema({
        "job_id": NINT, "started": BOOL, "mode": NSTR, "state": NSTR,
        "message": STR, "truncated": TRUNCATED,
    }, ["started", "message"]),
    annotations=JOB_CONTROL,
    handler="vault_reindex",
    mutating=True,
    audited=True,
)

# =============================================================================
# DECISIONS C5.2 trailer - hash and embedding job control
# =============================================================================
VAULT_HASH_ENQUEUE = ToolDef(
    name="vault_hash_enqueue",
    title="Queue models for hashing",
    description=(
        "Queue full-file SHA-256 / AutoV2 hashing as a background job. Hashing is "
        "what unlocks Civitai matching, and it is opt-in because it is slow - the "
        "whole library is roughly a 2.8-hour job - so scope it with scope='category', "
        "scope='folder' or scope='ids' unless the user really means everything. "
        "The queue is resumable and survives a restart. Non-destructive: no file is "
        "modified."),
    input_schema=schema({
        "scope": {"enum": ["all", "unhashed", "category", "folder", "ids"],
                  "default": "unhashed",
                  "description": "'unhashed' skips files that already have a hash."},
        "category": {"type": "string", "maxLength": 200,
                     "description": "Required when scope='category', e.g. 'loras'."},
        "folder": {"type": "string", "maxLength": 1024,
                   "description": "Required when scope='folder'; relative, '/' "
                                  "separated."},
        "uids": {"type": "array", "items": {"type": "string",
                                            "pattern": "^model:[0-9]+$"},
                 "maxItems": MAX_BATCH,
                 "description": "Required when scope='ids'."},
        "root_id": {"type": "integer", "minimum": 1},
        "priority": {"type": "integer", "minimum": 0, "maximum": 100, "default": 5},
    }),
    output_schema=schema({
        "batch_id": NSTR, "queued": INT, "bytes_total": INT, "eta_ms": NINT,
        "scope": STR, "message": STR, "audit_id": NINT, "truncated": TRUNCATED,
    }, ["batch_id", "queued", "message"]),
    annotations=WRITE_SAFE,
    handler="vault_hash_enqueue",
    mutating=True,
    audited=True,
)

VAULT_HASH_CANCEL = ToolDef(
    name="vault_hash_cancel",
    title="Cancel queued hashing",
    description=(
        "Cancel hash jobs. Pass batch_id to cancel one batch (the id "
        "vault_hash_enqueue returned), uids to cancel specific models, or neither to "
        "stop the whole queue. Already-computed hashes are kept."),
    input_schema=schema({
        "batch_id": {"type": "string", "maxLength": 64},
        "uids": {"type": "array", "items": {"type": "string",
                                            "pattern": "^model:[0-9]+$"},
                 "maxItems": MAX_BATCH},
    }),
    output_schema=schema({
        "cancelled": INT, "batch_id": NSTR, "message": STR, "audit_id": NINT,
        "truncated": TRUNCATED,
    }, ["cancelled", "message"]),
    annotations=JOB_CONTROL,
    handler="vault_hash_cancel",
    mutating=True,
    audited=True,
)

VAULT_EMBEDDINGS_REBUILD = ToolDef(
    name="vault_embeddings_rebuild",
    title="Rebuild the semantic index",
    description=(
        "Recompute the local ONNX embedding index that backs semantic ranking in "
        "vault_search. Runs in the background and is incremental unless force=true. "
        "Fails with a clear reason when the embedding model is not installed - "
        "vault_stats reports that under capabilities.semantic_search. "
        "Non-destructive: no asset file is touched."),
    input_schema=schema({
        "kinds": enum_array(KINDS, "Restrict the rebuild to these asset kinds. "
                                   "Omit for all."),
        "force": {"type": "boolean", "default": False,
                  "description": "Recompute vectors that already exist."},
    }),
    output_schema=schema({
        "state": STR, "started": BOOL, "embedded": INT, "pending": INT,
        "message": STR, "audit_id": NINT, "truncated": TRUNCATED,
    }, ["state", "started", "message"]),
    annotations=JOB_CONTROL,
    handler="vault_embeddings_rebuild",
    mutating=True,
    audited=True,
)

# =============================================================================
# DECISIONS C5.2 - file operations
# =============================================================================
_OP_RESULT = item_schema({
    "uid": STR, "ok": BOOL, "path": NSTR, "name": NSTR, "mode": NSTR,
    "trash_id": NINT, "trash_path": NSTR, "uid_restored": NSTR, "unchanged": BOOL,
    "sidecars": NINT, "error_code": NSTR, "error_message": NSTR,
}, ["uid", "ok"])

_BATCH_OUT = {
    "requested": INT, "affected": INT, "failed": INT,
    "results": {"type": "array", "items": _OP_RESULT},
    "audit_id": NINT, "truncated": TRUNCATED,
}

VAULT_RENAME = ToolDef(
    name="vault_rename",
    title="Rename a file",
    description=(
        "Rename one indexed model, workflow or output file in place. Sidecars (preview "
        "images, .civitai.info, .json) follow the new stem by default. The extension is "
        "preserved unless keep_extension is false. Roots are enforced; no filesystem "
        "path is accepted."),
    input_schema=schema({
        "uid": uid("The item to rename."),
        "new_name": {"type": "string", "minLength": 1, "maxLength": 255,
                     "description": "New file name - a single path component, no "
                                    "slashes."},
        "keep_extension": {"type": "boolean", "default": True},
        "rename_sidecars": {"type": "boolean", "default": True},
    }, ["uid", "new_name"]),
    output_schema=schema(_BATCH_OUT, ["requested", "affected", "failed", "results"]),
    annotations=WRITE_DESTRUCTIVE,
    handler="vault_rename",
    mutating=True,
    audited=True,
)

VAULT_MOVE = ToolDef(
    name="vault_move",
    title="Move files",
    description=(
        f"Move up to {MAX_BATCH} indexed items into another folder inside a configured "
        "root. The destination is named by root id + relative folder, never by an "
        "absolute path. on_conflict decides what happens when the name is taken."),
    input_schema=schema({
        "uids": {"type": "array", "items": {"type": "string", "pattern": UID_PATTERN},
                 "minItems": 1, "maxItems": MAX_BATCH},
        "target_root_id": {"type": "integer", "minimum": 1},
        "target_folder": {"type": "string", "maxLength": 1024,
                          "description": "Folder relative to the root, '/' separated. "
                                         "Empty string means the root itself."},
        "create_missing": {"type": "boolean", "default": True},
        "on_conflict": {"enum": ["fail", "keep_both", "overwrite"], "default": "fail"},
    }, ["uids", "target_root_id", "target_folder"]),
    output_schema=schema(_BATCH_OUT, ["requested", "affected", "failed", "results"]),
    annotations=WRITE_DESTRUCTIVE,
    handler="vault_move",
    mutating=True,
    audited=True,
)

VAULT_DELETE = ToolDef(
    name="vault_delete",
    title="Delete files",
    description=(
        "Delete up to 200 indexed items. mode defaults to 'trash', which moves them to "
        "the app-managed trash on the same volume and is fully recoverable with "
        "vault_trash_restore. mode='permanent' erases them from disk and REQUIRES "
        "confirm=true. Prefer trash. Never delete more than the user asked for."),
    input_schema=schema({
        "uids": {"type": "array", "items": {"type": "string", "pattern": UID_PATTERN},
                 "minItems": 1, "maxItems": MAX_BATCH},
        "mode": {"enum": ["trash", "permanent"], "default": "trash"},
        "confirm": {"type": "boolean", "default": False,
                    "description": "Mandatory (true) for mode='permanent'."},
    }, ["uids"]),
    output_schema=schema(_BATCH_OUT, ["requested", "affected", "failed", "results"]),
    annotations=WRITE_DESTRUCTIVE,
    handler="vault_delete",
    mutating=True,
    audited=True,
)

VAULT_TRASH_LIST = ToolDef(
    name="vault_trash_list",
    title="List the trash",
    description="List items currently in the vault trash, newest first, with the "
                "original path and the date they will be purged.",
    input_schema=schema({"limit": limit(50, 200), "offset": OFFSET}),
    output_schema=schema({
        "total": INT, "returned": INT, "offset": INT,
        "items": {"type": "array", "items": item_schema({
            "id": INT, "uid": NSTR, "kind": NSTR, "original_path": NSTR,
            "trash_path": NSTR, "size_bytes": INT, "deleted_at": NINT,
            "purge_after": NINT})},
        "truncated": TRUNCATED,
    }, ["total", "returned", "items"]),
    annotations=READ,
    handler="vault_trash_list",
)

VAULT_TRASH_RESTORE = ToolDef(
    name="vault_trash_restore",
    title="Restore from trash",
    description="Restore trashed items to their original location and re-attach their "
                "database rows. Ids come from vault_trash_list.",
    input_schema=schema({
        "ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                "minItems": 1, "maxItems": MAX_BATCH},
        "on_conflict": {"enum": ["fail", "keep_both", "overwrite"],
                        "default": "keep_both"},
    }, ["ids"]),
    output_schema=schema(_BATCH_OUT, ["requested", "affected", "failed", "results"]),
    annotations=WRITE_SAFE,
    handler="vault_trash_restore",
    mutating=True,
    audited=True,
)

VAULT_TRASH_EMPTY = ToolDef(
    name="vault_trash_empty",
    title="Empty the trash",
    description=(
        "Permanently erase trashed items. This cannot be undone, so confirm=true is "
        "mandatory. Pass ids to purge specific entries, older_than_days to purge by "
        "age, or neither to purge everything."),
    input_schema=schema({
        "ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                "maxItems": MAX_BATCH},
        "older_than_days": {"type": "integer", "minimum": 0, "maximum": 3650},
        "confirm": {"type": "boolean",
                    "description": "Must be true; there is no recovery."},
    }, ["confirm"]),
    output_schema=schema({
        "removed": INT, "files_removed": INT, "audit_id": NINT, "truncated": TRUNCATED,
    }, ["removed"]),
    annotations=WRITE_DESTRUCTIVE,
    handler="vault_trash_empty",
    mutating=True,
    audited=True,
)

VAULT_CREATE_FOLDER = ToolDef(
    name="vault_create_folder",
    title="Create a folder",
    description="Create a folder inside a configured root, so vault_move has somewhere "
                "to put things. Nested paths are created in one call.",
    input_schema=schema({
        "root_id": {"type": "integer", "minimum": 1},
        "folder": {"type": "string", "minLength": 1, "maxLength": 1024,
                   "description": "Path relative to the root, '/' separated."},
    }, ["root_id", "folder"]),
    output_schema=schema({
        "root_id": INT, "folder": STR, "abs_path": STR, "audit_id": NINT,
        "truncated": TRUNCATED,
    }, ["root_id", "folder", "abs_path"]),
    annotations=WRITE_SAFE,
    handler="vault_create_folder",
    mutating=True,
    audited=True,
)

VAULT_ASSIGN_TAGS = ToolDef(
    name="vault_assign_tags",
    title="Assign tags",
    description="Add and/or remove tags on up to 200 assets. Metadata only - no file on "
                "disk is touched.",
    input_schema=schema({
        "uids": {"type": "array", "items": {"type": "string", "pattern": UID_PATTERN},
                 "minItems": 1, "maxItems": MAX_BATCH},
        "add": {"type": "array", "items": {"type": "string", "maxLength": 120},
                "maxItems": 50},
        "remove": {"type": "array", "items": {"type": "string", "maxLength": 120},
                   "maxItems": 50},
    }, ["uids"]),
    output_schema=schema({
        "requested": INT, "updated": INT, "added": STRS, "removed": STRS,
        "audit_id": NINT, "truncated": TRUNCATED,
    }, ["requested", "updated"]),
    annotations=WRITE_SAFE,
    handler="vault_assign_tags",
    mutating=True,
    audited=True,
)

# =============================================================================
# REQUIREMENTS_R2 C9 - workflow "Enable"
# =============================================================================
_ENABLE_DESTINATION = item_schema({
    "category": NSTR, "root_id": NINT, "root_label": NSTR, "directory": NSTR,
    "abs_path": NSTR, "filename": NSTR,
})
_ENABLE_SOURCE = item_schema({
    "url": NSTR, "host": NSTR, "provider": NSTR, "size": INT, "sha256": NSTR,
    "hash_available": BOOL, "declared_directory": NSTR, "notes": STRS,
})

ENABLE_WORKFLOW_PLAN = ToolDef(
    name="enable_workflow_plan",
    title="Plan what a workflow needs",
    description=(
        "The dependency report for one workflow: every missing model with the ComfyUI "
        "folder it belongs in, every missing node package with the repository the "
        "ComfyUI-Manager registry names for it, the total download size, and the free "
        "space on each target drive. Nothing is downloaded. Returns a short-lived "
        "plan_token that enable_workflow_fetch requires - present this report to the "
        "user and let them choose item_ids before calling it."),
    input_schema=schema({
        "workflow_uid": {"type": "string", "pattern": "^workflow:[0-9]+$",
                         "description": "e.g. 'workflow:12'."},
    }, ["workflow_uid"]),
    output_schema=schema({
        "workflow": item_schema({"uid": STR, "id": INT, "name": NSTR,
                                 "is_runnable": BOOL, "origin": NSTR}),
        "summary": item_schema({
            "total": INT, "satisfied": INT, "missing_models": INT,
            "missing_node_packages": INT, "fetchable": INT, "not_fetchable": INT,
            "download_bytes": INT, "items_with_unknown_size": INT}),
        "space": item_schema({
            "sufficient": BOOL, "shortfall_bytes": INT, "download_bytes": INT,
            "margin_pct": INT,
            "volumes": {"type": "array", "items": item_schema({
                "directory": NSTR, "root_id": NINT, "root_label": NSTR,
                "download_bytes": INT, "required_bytes": INT, "free_bytes": INT,
                "total_bytes": INT, "sufficient": BOOL, "used_pct": NNUM})}}),
        "models": {"type": "array", "items": item_schema({
            "item_id": STR, "kind": STR, "ref_name": STR, "occurrences": INT,
            "category": NSTR, "status": STR, "reason": NSTR,
            "via": {"type": "array", "items": item_schema(
                {"class": NSTR, "input": NSTR})},
            "suggestions": {"type": "array", "items": item_schema(
                {"uid": NSTR, "name": NSTR, "score": NNUM})},
            "destination": {"anyOf": [_ENABLE_DESTINATION, {"type": "null"}]},
            "source": {"anyOf": [_ENABLE_SOURCE, {"type": "null"}]}})},
        "node_packages": {"type": "array", "items": item_schema({
            "item_id": STR, "kind": STR, "ref_name": STR, "repo_url": NSTR,
            "host": NSTR, "class_types": STRS, "class_count": INT, "status": STR,
            "reason": NSTR, "manual_steps": STRS, "never_runs": STRS,
            # The immutable commit the plan pins, and the per-item safety notes
            # (level green|yellow|red) the UI renders beside it.
            "revision": NSTR,
            "safety": {"type": "array", "items": item_schema(
                {"level": STR, "code": STR, "message": STR})},
            "destination": {"anyOf": [_ENABLE_DESTINATION, {"type": "null"}]}})},
        "plan_token": STR,
        "plan_expires_in_ms": INT,
        "plan_items": INT,
        "policy": item_schema({
            "model_hosts": STRS, "git_hosts": STRS,
            # Not used by this tool, but ``hosts.describe()`` is spread whole
            # into the policy block, so the key has to be declared.
            "release_hosts": STRS,
            "scheme": STR,
            "max_redirects": INT, "never_runs": STRS,
            "on_conflict_allowed": STRS, "git_available": BOOL}),
        "generated_at": INT,
        "truncated": TRUNCATED,
    }, ["workflow", "summary", "space", "plan_token"]),
    annotations=READ,
    handler="enable_workflow_plan",
)

ENABLE_WORKFLOW_FETCH = ToolDef(
    name="enable_workflow_fetch",
    title="Fetch a workflow's missing resources",
    description=(
        "Download the items the user selected from enable_workflow_plan into the "
        "correct ComfyUI folders. Requires the plan_token from that report, the "
        "item_ids the user picked, and confirm=true - there is no 'fetch everything' "
        "shorthand and a stale plan is refused. Only Civitai and Hugging Face may "
        "serve models and only registry-declared repositories may be cloned; every "
        "redirect is re-checked. Node packages are cloned, never installed: no pip "
        "install, no requirements.txt, no install.py is ever run. Runs in the "
        "background; poll with enable_workflow_plan or the REST status endpoint."),
    input_schema=schema({
        "workflow_uid": {"type": "string", "pattern": "^workflow:[0-9]+$"},
        "plan_token": {"type": "string", "minLength": 8, "maxLength": 256,
                       "description": "Exactly as returned by enable_workflow_plan."},
        "item_ids": {"type": "array", "items": {"type": "string", "maxLength": 64},
                     "minItems": 1, "maxItems": MAX_BATCH,
                     "description": "item_id values the user chose from the report."},
        "confirm": {"type": "boolean",
                    "description": "Must be true. Nothing downloads implicitly."},
        "on_conflict": {"enum": ["fail", "skip", "keep_both"], "default": "fail",
                        "description": "'overwrite' does not exist on this path."},
    }, ["workflow_uid", "plan_token", "item_ids", "confirm"]),
    output_schema=schema({
        "batch_id": STR, "workflow_id": INT, "queued": INT, "bytes_total": INT,
        "items": {"type": "array", "items": item_schema({
            "item_id": STR, "kind": STR, "ref_name": STR, "host": NSTR,
            "target_abs_path": NSTR, "size": INT})},
        "stream": STR, "started_at": INT, "scan_job_id": NINT,
        "audit_id": NINT, "truncated": TRUNCATED,
    }, ["batch_id", "queued", "bytes_total"]),
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
    handler="enable_workflow_fetch",
    mutating=True,
    audited=True,
)


TOOLS: tuple[ToolDef, ...] = (
    VAULT_SEARCH,
    LIST_MODELS,
    GET_MODEL,
    LIST_NODE_PACKAGES,
    LIST_NODE_CLASSES,
    GET_NODE_CLASS,
    LIST_WORKFLOWS,
    INSPECT_WORKFLOW,
    FIND_MODEL_USAGE,
    QUERY_OUTPUTS,
    VAULT_STATS,
    GET_INDEX_STATUS,
    VAULT_REINDEX,
    VAULT_HASH_ENQUEUE,
    VAULT_HASH_CANCEL,
    VAULT_EMBEDDINGS_REBUILD,
    VAULT_RENAME,
    VAULT_MOVE,
    VAULT_DELETE,
    VAULT_TRASH_LIST,
    VAULT_TRASH_RESTORE,
    VAULT_TRASH_EMPTY,
    VAULT_CREATE_FOLDER,
    VAULT_ASSIGN_TAGS,
    ENABLE_WORKFLOW_PLAN,
    ENABLE_WORKFLOW_FETCH,
)

BY_NAME: dict[str, ToolDef] = {t.name: t for t in TOOLS}

#: Plausible-but-wrong tool names -> the tool that actually does the job, so an
#: agent that guessed a name gets pointed at the right one instead of a bare
#: "unknown tool".  Every value here MUST be a real entry in BY_NAME.
ALIAS_HINTS = {
    "hash_models": "vault_hash_enqueue",
    "start_hashing": "vault_hash_enqueue",
    "vault_hash_start": "vault_hash_enqueue",
    "vault_hash_stop": "vault_hash_cancel",
    "cancel_hashing": "vault_hash_cancel",
    "rebuild_embeddings": "vault_embeddings_rebuild",
    "vault_embeddings": "vault_embeddings_rebuild",
    "vault_scan": "vault_reindex",
    "reindex": "vault_reindex",
    "delete_model": "vault_delete",
    "rename_file": "vault_rename",
    "move_file": "vault_move",
    "search": "vault_search",
    "get_stats": "vault_stats",
    "install_missing_models": "enable_workflow_plan",
    "download_model": "enable_workflow_plan",
    "enable_workflow": "enable_workflow_plan",
    "vault_enable_workflow": "enable_workflow_plan",
    "install_node_package": "enable_workflow_plan",
}
