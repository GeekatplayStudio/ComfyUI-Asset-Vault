"""Workflows: list, detail, graph, dependencies (API_CONTRACT 5)."""

from __future__ import annotations

from typing import Any

from .common import GroupInfo, Lenient, MetaInfo, PageInfo


class WorkflowCounts(Lenient):
    nodes: int = 0
    links: int = 0
    groups: int = 0
    missing_nodes: int = 0
    missing_models: int = 0


class WorkflowItem(Lenient):
    uid: str
    id: int
    name: str | None = None
    rel_path: str | None = None
    folder: str | None = None
    root_id: int | None = None
    source: str | None = None
    format: str | None = None
    title: str | None = None
    description: str | None = None
    description_source: str | None = None
    capability_tags: list[str] = []
    base_model: str | None = None
    modality: str | None = None
    counts: WorkflowCounts | None = None
    is_runnable: bool | None = None
    has_subgraphs: bool | None = None
    #: How many subgraph definitions the file declares.  Nodes that
    #: instantiate one are internal references, not dependencies.
    subgraph_count: int | None = None
    prompt_summary: str | None = None
    size: int | None = None
    modified_at: int | None = None
    thumbnail_url: str | None = None
    counts_outputs: int | None = None
    missing: bool | None = None


class WorkflowList(Lenient):
    items: list[WorkflowItem]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class WorkflowDetail(WorkflowItem):
    node_breakdown: list[dict[str, Any]] = []
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    unresolved_inputs: int | None = None
    graph_available: bool | None = None
    graph_truncated: bool | None = None
    abs_path: str | None = None
    schema_version: str | None = None
    author: str | None = None
    outputs_recent: list[dict[str, Any]] = []
    actions: dict[str, Any] | None = None


class DependencySummary(Lenient):
    total: int = 0
    satisfied: int = 0
    missing: int = 0
    ambiguous: int = 0


class WorkflowDependencies(Lenient):
    summary: DependencySummary
    models: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    embeddings: list[dict[str, Any]] = []
    input_files: list[dict[str, Any]] = []


class GraphTooLarge(Lenient):
    """413 body for graphs above the 32 MB inline cap."""

    download_url: str
    size: int | None = None
