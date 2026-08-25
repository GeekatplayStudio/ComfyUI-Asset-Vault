"""System, config, wizard, stats, health, roots (API_CONTRACT 1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator

from ...core.urlsafe import UrlRejected, check_local_url
from .common import Lenient, Strict


def _validated_ollama_url(value: str | None, info: ValidationInfo) -> str | None:
    """SECURITY_REVIEW S-08: the one caller-supplied outbound address.

    ``/ai/describe`` sends an asset fact sheet - a workflow's positive prompt
    included - to whatever this names, so anything that is not loopback or a
    private-network address is a 422 rather than a silent egress.
    """
    if value is None:
        return None
    field = getattr(info, "field_name", None) or "ollama_url"
    try:
        return check_local_url(value, field=field)
    except UrlRejected as exc:
        raise ValueError(str(exc)) from exc


class Features(Lenient):
    smart_search: bool
    civitai: bool
    ollama: bool
    mcp: bool
    video_thumbnails: bool


class SystemInfo(Lenient):
    app: str
    version: str
    author: str
    api_version: int
    schema_version: int
    python: str
    platform: str
    features: Features


class RootItem(Lenient):
    id: int
    kind: str
    path: str
    label: str
    available: bool
    is_default: bool
    source: str
    category: str | None = None


class SystemConfig(Lenient):
    comfyui_path: str | None
    path_exists: bool
    is_configured: bool
    auto_reindex: bool
    watch_enabled: bool
    online_enabled: bool
    civitai_enabled: bool
    civitai_api_key_set: bool
    ollama_enabled: bool
    ollama_url: str
    ollama_model: str
    smart_search_enabled: bool
    smart_search_min_score: float
    app_update_check_enabled: bool
    app_update_auto_download: bool
    hash_concurrency: int
    hash_throttle_mbps: int
    thumb_cache_max_mb: int
    thumb_video_ffmpeg: bool
    page_size_default: int
    trash_mode: str
    trash_retention_days: int
    read_held_extra_paths: bool
    mcp_read_only: bool
    extra_workflow_dirs: list[str]
    roots: list[RootItem]
    roots_changed: bool | None = None


class ConfigPatch(Strict):
    """Any subset of the writable keys.  ``civitai_api_key`` is write-only."""

    comfyui_path: str | None = None
    auto_reindex: bool | None = None
    watch_enabled: bool | None = None
    online_enabled: bool | None = None
    civitai_enabled: bool | None = None
    civitai_api_key: str | None = None
    ollama_enabled: bool | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
    smart_search_enabled: bool | None = None
    # How close a semantic hit must be to the query. Higher = stricter.
    smart_search_min_score: float | None = Field(default=None, ge=0.05, le=0.9)
    hash_concurrency: int | None = Field(default=None, ge=1, le=8)
    hash_throttle_mbps: int | None = Field(default=None, ge=0, le=10_000)
    thumb_cache_max_mb: int | None = Field(default=None, ge=64, le=1_000_000)
    thumb_video_ffmpeg: bool | None = None
    page_size_default: int | None = Field(default=None, ge=1, le=500)
    trash_mode: Literal["trash", "permanent"] | None = None
    trash_retention_days: int | None = Field(default=None, ge=0, le=3650)
    read_held_extra_paths: bool | None = None
    # DECISIONS C5 rail 6: full MCP file-operation access is the shipped default;
    # this is the switch that puts the MCP surface back to read-only.
    mcp_read_only: bool | None = None
    extra_workflow_dirs: list[str] | None = None
    app_update_check_enabled: bool | None = None
    # Downloading a new copy of the app is opt-in and defaults off.
    app_update_auto_download: bool | None = None
    app_update_skipped_version: str | None = Field(default=None, max_length=40)

    _check_ollama_url = field_validator("ollama_url")(_validated_ollama_url)


class AppUpdatePending(Lenient):
    """A staged release waiting for the next launch to apply it."""

    version: str
    tag: str | None = None
    from_version: str | None = None
    sha256: str | None = None
    verified: bool = False
    staged_at: int | None = None
    files: int = 0
    notes: str | None = None
    html_url: str | None = None


class AppUpdateStatus(Lenient):
    current_version: str
    latest_version: str | None = None
    has_update: bool = False
    #: disabled | offline | current | update_available | error | unknown
    state: str
    reason: str | None = None
    notes: str | None = None
    published_at: str | None = None
    download_bytes: int = 0
    #: Whether the release published a checksum at all.  Integrity, not authorship.
    checksum_published: bool = False
    downloadable: bool | None = None
    releases_url: str
    repository: str
    check_enabled: bool = True
    auto_download: bool = False
    online_enabled: bool = False
    last_check: int = 0
    skipped_version: str | None = None
    pending: AppUpdatePending | None = None


class AppUpdateDownloadResponse(Lenient):
    ok: bool
    pending: AppUpdatePending | None = None
    restart_required: bool = True


class AppUpdateDiscardResponse(Lenient):
    discarded: bool


class ValidatePathRequest(Strict):
    path: str = Field(min_length=1, max_length=4096)


class PathSignals(Lenient):
    has_models: bool
    has_custom_nodes: bool
    has_output: bool
    has_input: bool
    has_user_workflows: bool
    has_root_workflows: bool
    has_main_py: bool
    comfyui_version: str | None = None


class PathPreview(Lenient):
    model_files: int
    model_bytes: int
    custom_node_packages: int
    workflows: int
    outputs: int
    inputs: int
    truncated: bool | None = None


class ExtraModelPaths(Lenient):
    present: bool
    held_present: bool
    roots: list[Any] = []


class ValidatePathResponse(Lenient):
    valid: bool
    normalized: str
    exists: bool
    is_comfyui_root: bool
    signals: PathSignals
    extra_model_paths: ExtraModelPaths
    preview: PathPreview
    warnings: list[str] = []
    reason: str | None = None


class WizardCompleteRequest(Strict):
    comfyui_path: str = Field(min_length=1, max_length=4096)
    online_enabled: bool = True
    auto_reindex: bool = True
    smart_search_enabled: bool = False
    ollama_enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    start_scan: bool = True

    _check_ollama_url = field_validator("ollama_url")(_validated_ollama_url)


class WizardCompleteResponse(Lenient):
    is_configured: bool
    job_id: int | None = None
    scan_started: bool


class CategoryStat(Lenient):
    category: str | None
    count: int
    bytes: int


class BaseModelStat(Lenient):
    base_model: str | None
    count: int


class LastScan(Lenient):
    job_id: int | None = None
    finished_at: int | None = None
    duration_ms: int | None = None
    errors: int | None = None


class VaultStats(Lenient):
    models: int
    model_files: int
    models_bytes: int
    models_hashed: int
    node_packages: int
    node_classes: int
    official_node_classes: int
    workflows: int
    workflows_broken: int
    outputs: int
    outputs_bytes: int
    inputs: int
    embedded: int
    integrity_issues: int
    by_category: list[CategoryStat]
    by_base_model: list[BaseModelStat]
    last_scan: LastScan | None = None


class HealthCheck(Lenient):
    id: str
    status: Literal["ok", "warn", "error"]
    message: str | None = None
    action: str | None = None
    count: int | None = None
    items: list[dict[str, Any]] | None = None


class HealthReport(Lenient):
    status: Literal["ok", "degraded", "error"]
    checks: list[HealthCheck]


class RootsList(Lenient):
    items: list[RootItem]


class RootCreate(Strict):
    path: str = Field(min_length=1, max_length=4096)
    kind: Literal["extra_models", "extra_workflows"]
    label: str | None = Field(default=None, max_length=200)


class RootDeleted(Lenient):
    deleted: bool
    id: int


class ThumbsGcRequest(Strict):
    max_mb: int = Field(default=2048, ge=0, le=1_000_000)


class ThumbsGcResponse(Lenient):
    deleted: int
    freed_bytes: int
    remaining_bytes: int


class OllamaTestRequest(Strict):
    url: str | None = Field(default=None, max_length=2048)

    _check_url = field_validator("url")(_validated_ollama_url)


class OllamaTestResponse(Lenient):
    available: bool
    models: list[str] = []
    latency_ms: int | None = None
    reason: str | None = None


class Pong(Lenient):
    pong: bool
    t: int
