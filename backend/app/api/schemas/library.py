"""Albums, tags and AI enrichment (API_CONTRACT 12-13)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, PageInfo, Strict

# --- albums -------------------------------------------------------------------

class AlbumNode(Lenient):
    id: int
    uid: str | None = None
    name: str
    kind: str
    scope: str
    icon: str | None = None
    color: str | None = None
    parent_id: int | None = None
    sort_order: int = 0
    item_count: int = 0
    query: dict[str, Any] | None = None
    editable: bool = True
    children: list[dict[str, Any]] = []


class AlbumTree(Lenient):
    scope: str
    nodes: list[AlbumNode]


class AlbumCreate(Strict):
    name: str = Field(min_length=1, max_length=200)
    scope: Literal["models", "nodes", "workflows", "outputs", "all"] = "all"
    kind: Literal["folder", "smart", "manual"] = "manual"
    parent_id: int | None = None
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=40)
    query_json: dict[str, Any] | None = None


class AlbumPatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    icon: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, max_length=40)
    parent_id: int | None = None
    sort_order: int | None = None
    query_json: dict[str, Any] | None = None


class AlbumDeleted(Lenient):
    deleted: bool
    id: int


class AlbumItemsRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=500)


class AlbumItemsAdded(Lenient):
    added: int


class AlbumItemsRemoved(Lenient):
    removed: int


# --- tags ---------------------------------------------------------------------

class TagItem(Lenient):
    id: int
    name: str
    color: str | None = None
    source: str | None = None
    count: int = 0


class TagList(Lenient):
    items: list[TagItem]
    page: PageInfo


class TagCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=40)


class TagPatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=40)


class TagDeleted(Lenient):
    deleted: bool
    id: int


class TagAssignRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=500)
    add: list[str] | None = None
    remove: list[str] | None = None


class TagAssignResponse(Lenient):
    updated: int
    uids: list[str] = []


# --- AI (Ollama) --------------------------------------------------------------

class AiStatus(Lenient):
    enabled: bool
    available: bool
    url: str
    models: list[str] = []
    reason: str | None = None


class AiDescribeRequest(Strict):
    uid: str
    task: Literal["workflow_summary", "model_usage_notes", "update_benefits",
                  "node_package_summary"]
    model: str | None = Field(default=None, max_length=200)
    stream: bool = False


class AiDescribeResponse(Lenient):
    uid: str
    task: str
    text: str | None = None
    model: str | None = None
    cached: bool = False
    elapsed_ms: int = 0
