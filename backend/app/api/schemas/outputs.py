"""Generated outputs (API_CONTRACT 6)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import GroupInfo, Lenient, MetaInfo, PageInfo, Strict


class OutputItem(Lenient):
    uid: str
    id: int
    filename: str | None = None
    ext: str | None = None
    media_kind: str | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    size: int | None = None
    created_at: int | None = None
    modified_at: int | None = None
    folder: str | None = None
    rel_path: str | None = None
    root_id: int | None = None
    has_metadata: bool | None = None
    metadata_format: str | None = None
    positive_prompt: str | None = None
    model_name: str | None = None
    model_uid: str | None = None
    workflow_uid: str | None = None
    seed: str | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    favorite: bool | None = None
    user_rating: int | None = None
    album_id: int | None = None
    color_label: str | None = None
    tags: list[str] = []
    thumbnail_url: str | None = None
    raw_url: str | None = None
    download_url: str | None = None
    missing: bool | None = None


class OutputList(Lenient):
    items: list[OutputItem]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class OutputDetail(OutputItem):
    negative_prompt: str | None = None
    denoise: float | None = None
    provenance: dict[str, Any] = {}
    node_count: int | None = None
    unresolved_inputs: int | None = None
    loras: list[dict[str, Any]] = []
    all_models: list[dict[str, Any]] = []
    workflow_hash: str | None = None
    abs_path: str | None = None
    color_mode: str | None = None
    has_alpha: bool | None = None
    frame_count: int | None = None
    graph_available: bool | None = None
    user_notes: str | None = None
    siblings: list[dict[str, Any]] = []
    exif: dict[str, Any] = {}
    actions: dict[str, Any] | None = None


class ExtractWorkflowRequest(Strict):
    root_id: int
    folder: str = Field(default="", max_length=1024)
    name: str = Field(min_length=1, max_length=200)


class ExtractWorkflowResponse(Lenient):
    uid: str
    abs_path: str
