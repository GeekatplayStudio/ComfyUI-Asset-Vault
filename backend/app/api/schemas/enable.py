"""Workflow Enable payloads - API_CONTRACT 20 (REQUIREMENTS_R2 C9)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import Lenient, Strict


class EnableDestination(Lenient):
    category: str
    root_id: int
    root_label: str | None = None
    directory: str
    abs_path: str
    filename: str


class EnableSource(Lenient):
    url: str
    host: str
    provider: str = Field(description="workflow_manifest | vault_cache")
    size: int = 0
    sha256: str | None = None
    hash_available: bool = False
    declared_directory: str | None = None
    notes: list[str] = []


class EnableModelItem(Lenient):
    item_id: str
    kind: str = "model"
    ref_name: str
    occurrences: int = 1
    via: list[dict[str, Any]] = []
    category: str | None = None
    destination: EnableDestination | None = None
    source: EnableSource | None = None
    status: str = Field(description="fetchable | already_present | no_source | blocked")
    reason: str | None = None
    suggestions: list[dict[str, Any]] = []


class EnableNodeItem(Lenient):
    item_id: str
    kind: str = "node_package"
    ref_name: str
    repo_url: str | None = None
    host: str | None = None
    class_types: list[str] = []
    class_count: int = 0
    destination: EnableDestination | None = None
    status: str
    reason: str | None = None
    manual_steps: list[str] = []
    never_runs: list[str] = []
    revision: str | None = None
    safety: list[dict[str, Any]] = []


class EnableVolume(Lenient):
    directory: str
    root_id: int | None = None
    root_label: str | None = None
    download_bytes: int = 0
    required_bytes: int = 0
    free_bytes: int = 0
    total_bytes: int = 0
    sufficient: bool = True
    used_pct: float | None = None


class EnableSpace(Lenient):
    sufficient: bool
    shortfall_bytes: int
    download_bytes: int
    margin_pct: int
    volumes: list[EnableVolume] = []


class EnablePlan(Lenient):
    """The dependency report.  Nothing downloads until the user acts on this."""

    workflow: dict[str, Any]
    summary: dict[str, Any]
    space: EnableSpace
    models: list[EnableModelItem] = []
    node_packages: list[EnableNodeItem] = []
    plan_token: str
    plan_expires_in_ms: int
    plan_items: int
    policy: dict[str, Any]
    generated_at: int


class EnableFetchRequest(Strict):
    plan_token: str = Field(min_length=8, max_length=256,
                            description="Exactly as returned by GET .../enable/plan.")
    item_ids: list[str] = Field(min_length=1, max_length=200,
                                description="The items the user selected. No wildcard, "
                                            "no 'everything' shorthand.")
    confirm: bool = Field(description="Must be true. Nothing downloads implicitly.")
    on_conflict: str = Field(default="fail", pattern="^(fail|skip|keep_both)$",
                             description="'overwrite' is not offered.")


class EnableFetchResponse(Lenient):
    batch_id: str
    workflow_id: int
    queued: int
    bytes_total: int
    items: list[dict[str, Any]] = []
    stream: str
    started_at: int
    scan_job_id: int | None = None


class EnableStatus(Lenient):
    running: bool
    states: dict[str, int] = {}
    queued: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    active: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    git_available: bool = False


class EnableCancelRequest(Strict):
    batch_id: str | None = Field(default=None, max_length=64,
                                 description="Omit to cancel every queued fetch.")


class EnableCancelResponse(Lenient):
    cancelled: int
    batch_id: str | None = None


class EnableRecheck(Lenient):
    workflow: dict[str, Any]
    is_runnable: bool
    is_runnable_recorded: bool
    missing_models: list[str] = []
    missing_node_classes: list[str] = []
    counts: dict[str, int] = {}
    scan: dict[str, Any] = {}
    stale: bool = False
    message: str
    checked_at: int


class EnableQuarantine(Lenient):
    items: list[dict[str, Any]] = []
    total: int = 0
    bytes: int = 0
