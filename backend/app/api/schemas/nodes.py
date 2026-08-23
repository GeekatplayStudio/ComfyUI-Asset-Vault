"""Node packages and node classes (API_CONTRACT 4)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import GroupInfo, Lenient, MetaInfo, PageInfo, Strict


class Extraction(Lenient):
    status: str | None = None
    strategies: list[str] = []
    confidence: str | None = None
    notes: str | None = None


class RepoRef(Lenient):
    url: str | None = None
    suspect: bool | None = None
    branch: str | None = None
    commit: str | None = None
    commit_at: int | None = None


class UpdateState(Lenient):
    state: str | None = None
    has_update: bool | None = None
    commits_behind: int | None = None
    checked_at: int | None = None


class DepsInfo(Lenient):
    count: int = 0
    satisfied: bool | None = None
    missing: list[str] = []


class NodePackageItem(Lenient):
    uid: str
    id: int
    folder_name: str | None = None
    display_name: str | None = None
    author: str | None = None
    publisher_id: str | None = None
    description: str | None = None
    is_official: bool | None = None
    enabled: bool | None = None
    is_single_file: bool | None = None
    class_count: int = 0
    extraction: Extraction | None = None
    repo: RepoRef | None = None
    update: UpdateState | None = None
    version: str | None = None
    deps: DepsInfo | None = None
    size: int | None = None
    file_count: int | None = None
    counts: dict[str, Any] | None = None
    thumbnail_url: str | None = None
    missing: bool | None = None


class NodePackageList(Lenient):
    items: list[NodePackageItem]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class NodePackageDetail(NodePackageItem):
    long_description: str | None = None
    license: str | None = None
    homepage_url: str | None = None
    icon_url: str | None = None
    abs_path: str | None = None
    python_deps: list[str] = []
    has_web_directory: bool | None = None
    class_categories: list[dict[str, Any]] = []
    top_classes: list[dict[str, Any]] = []
    source_breakdown: dict[str, Any] | None = None
    disabled_reason: str | None = None
    comfyui_version: str | None = None
    actions: dict[str, Any] | None = None


class NodeClassPackageRef(Lenient):
    uid: str | None = None
    name: str | None = None
    official: bool | None = None


class NodeClassItem(Lenient):
    uid: str
    id: int
    node_id: str | None = None
    display_name: str | None = None
    class_name: str | None = None
    category: str | None = None
    description: str | None = None
    package: NodeClassPackageRef | None = None
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    output_node: bool | None = None
    flags: dict[str, Any] | None = None
    confidence: str | None = None
    source: dict[str, Any] | None = None
    counts: dict[str, Any] | None = None


class NodeClassList(Lenient):
    items: list[NodeClassItem]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class NodeClassDetail(NodeClassItem):
    workflows_using: list[dict[str, Any]] = []
    input_types_json: str | None = None


class CheckUpdateResponse(Lenient):
    state: str
    reason: str | None = None


class CheckUpdatesRequest(Strict):
    ids: list[int] | None = None


class CheckUpdatesResponse(Lenient):
    job_id: str
    queued: int


class UpdateStatusResponse(Lenient):
    states: list[dict[str, Any]] = Field(default_factory=list)
    pending: int = 0
    with_update: int = 0
    checked: int = 0
    total: int = 0
