"""ComfyUI version, updater and official templates (API_CONTRACT 19, C8)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

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
    #: Set when the entry is listed but refused, e.g.
    #: ``cmd_metacharacter_in_path`` (SECURITY_REVIEW S-19).
    unsafe_reason: str | None = None
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


class Launcher(Lenient):
    """A discovered way to start ComfyUI - never a hard-coded one."""

    id: str
    kind: Literal["batch", "python"]
    label: str
    path: str | None = None
    working_dir: str | None = None
    command: list[str] = []
    port: int | None = None
    port_source: str | None = None
    #: What this script showed to be accepted as a launcher at all (S-21).
    evidence: list[str] = []
    available: bool = True
    #: Set when the entry is listed but refused, e.g.
    #: ``cmd_metacharacter_in_path`` (S-19).
    unsafe_reason: str | None = None
    recommended: bool = False
    note: str | None = None


class LaunchStatus(Lenient):
    status: Literal["idle", "starting", "ready", "failed"] = "idle"
    running: bool = False
    launcher: str | None = None
    path: str | None = None
    port: int | None = None
    url: str | None = None
    pid: int | None = None
    ready: bool = False
    started_at: int | None = None
    finished_at: int | None = None
    elapsed_ms: int | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    error: str | None = None
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
    launchers: list[Launcher] = []
    recommended_launcher: str | None = None
    running: RunningProbe | None = None
    update_status: UpdateStatus | None = None
    launch_status: LaunchStatus | None = None


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


class DeepLink(Lenient):
    """Whether this ComfyUI frontend can be told to open this file by URL."""

    supported: bool = False
    reason: str | None = None
    explanation: str | None = None
    params: dict[str, str] | None = None
    query: str | None = None
    source: str | None = None
    template: str | None = None
    verified_against: str | None = None


class WorkflowCopyPlan(Lenient):
    possible: bool = False
    needed: bool = False
    reason: str | None = None
    destination: str | None = None
    target_dir: str | None = None
    exists: bool = False
    #: Always false: this frontend has no user-workflow deep link to create.
    creates_deep_link: bool = False
    note: str | None = None


class OpenWorkflowPlan(Lenient):
    """What would happen, shown before anything happens.

    ``copy_plan`` is serialised as ``copy``: the wire name reads best in the
    payload, and the Python name has to avoid shadowing ``BaseModel.copy``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    uid: str
    workflow_id: int
    name: str | None = None
    abs_path: str | None = None
    rel_path: str | None = None
    origin: str | None = None
    origin_package: str | None = None
    origin_label: str | None = None
    running: RunningProbe
    port: int
    port_reason: str | None = None
    url: str
    open_method: Literal["deep_link", "manual"]
    deep_link: DeepLink
    manual_hint: str | None = None
    launcher: Launcher | None = None
    launcher_confirm_path: str | None = None
    launcher_alternatives: list[Launcher] = []
    launcher_error: str | None = None
    copy_plan: WorkflowCopyPlan = Field(alias="copy")
    steps: list[str] = []
    needs_start: bool = False
    can_open: bool = True
    blocked_reason: str | None = None
    frontend_version: str | None = None
    comfyui_version: str | None = None


class OpenWorkflowRequest(Strict):
    uid: str = Field(min_length=3, max_length=64)
    launcher: str | None = None
    #: Starting a program is its own question with its own answer.
    start: bool = False
    #: Must equal ``launcher_confirm_path`` from the plan when ``start`` is true.
    confirm_launcher_path: str | None = Field(default=None, max_length=4096)
    #: Writing into the ComfyUI install is its own question too.
    copy_to_user_workflows: bool = False
    #: Must equal ``copy.destination`` from the plan when the copy is requested.
    confirm_copy_destination: str | None = Field(default=None, max_length=4096)


class OpenWorkflowResponse(Lenient):
    uid: str
    name: str | None = None
    url: str
    open_method: Literal["deep_link", "manual"]
    deep_link: DeepLink
    manual_hint: str | None = None
    port: int
    copied: bool = False
    copy_destination: str | None = None
    copy_note: str | None = None
    started: bool = False
    already_running: bool = False
    ready: bool = False
    launcher: str | None = None
    launcher_path: str | None = None
    stream: str | None = None
    timeout_s: int | None = None
    started_at: int | None = None
    note: str | None = None


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
