"""ComfyUI version, updater and official templates (API_CONTRACT 19, C8)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, PageInfo, Strict


class GitInfo(Lenient):
    present: bool = False
    branch: str | None = None
    commit: str | None = None
    remote: str | None = None
    shallow: bool = False
    worktree: bool = False
    error: str | None = None


class Updater(Lenient):
    id: str
    kind: Literal["batch", "git"]
    label: str
    path: str | None = None
    working_dir: str | None = None
    command: list[str] = []
    available: bool = True
    recommended: bool = False
    note: str | None = None


class RunningProbe(Lenient):
    running: bool = False
    ports: list[int] = []
    method: str | None = None
    confidence: Literal["measured", "inferred"] = "inferred"
    note: str | None = None


class UpdateStatus(Lenient):
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    running: bool = False
    updater: str | None = None
    path: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    exit_code: int | None = None
    error: str | None = None
    duration_ms: int | None = None
    lines: int | None = None
    version_after: str | None = None
    restart_required: bool | None = None
    note: str | None = None


class ComfyUIInfo(Lenient):
    configured: bool
    comfyui_path: str | None = None
    install_parent: str | None = None
    version: str | None = None
    version_source: str | None = None
    flavour: Literal["portable", "git", "desktop", "manual", "unknown"] = "unknown"
    flavour_evidence: list[str] = []
    python_home: str | None = None
    git: GitInfo | None = None
    packages: dict[str, str] = {}
    updaters: list[Updater] = []
    recommended_updater: str | None = None
    running: RunningProbe | None = None
    update_status: UpdateStatus | None = None


class LatestVersion(Lenient):
    installed: str | None = None
    installed_source: str | None = None
    latest: str | None = None
    state: Literal["current", "behind", "ahead", "unknown"] = "unknown"
    checked_at: int | None = None
    source: str | None = None
    cached: bool = False
    reason: str | None = None
    hint: str | None = None
    release_url: str | None = None
    release_notes: str | None = None


class UpdatePlan(Lenient):
    """What a confirmation dialog must show before anything runs (C8.3)."""

    updater: str
    label: str
    path: str
    working_dir: str | None = None
    command: list[str]
    confirm_path: str
    running: RunningProbe
    can_run: bool
    blocked_reason: str | None = None
    warnings: list[str] = []
    alternatives: list[Updater] = []


class UpdateRunRequest(Strict):
    updater: str | None = None
    #: Must equal the ``confirm_path`` from ``GET /comfyui/update/plan``.
    confirm_path: str = Field(min_length=1, max_length=4096)


class UpdateRunResponse(Lenient):
    started: bool
    updater: str
    label: str
    path: str
    working_dir: str | None = None
    stream: str
    started_at: int


class TemplateItem(Lenient):
    id: str
    title: str
    bundle: str
    filename: str
    size: int
    path: str
    origin: Literal["official_template"] = "official_template"
    origin_label: str = "official template"
    template_version: str | None = None


class TemplateCatalogue(Lenient):
    available: bool
    reason: str | None = None
    package_version: str | None = None
    path: str | None = None
    bundles: list[dict[str, Any]] = []
    items: list[TemplateItem] = []
    total: int = 0
    note: str | None = None


class WorkflowOriginGroup(Lenient):
    origin: Literal["user", "bundled", "official_template"]
    label: str
    package: str | None = None
    count: int
    runnable: int = 0
    broken: int = 0
    #: True for the official-template row: listed here, but not a vault row.
    catalogued_only: bool | None = None


class WorkflowOrigins(Lenient):
    #: Indexed vault workflows only.  Official templates are catalogued, not
    #: indexed, so they are counted in ``catalogued_total`` instead.
    total: int
    indexed_total: int | None = None
    catalogued_total: int | None = None
    groups: list[WorkflowOriginGroup]
    official_templates: TemplateCatalogue
    page: PageInfo | None = None
