"""Model list / detail / facets / groups / usage (API_CONTRACT 3)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .common import GroupInfo, Lenient, MetaInfo, PageInfo, Strict, TreeNode


class BaseModelRef(Lenient):
    family: str | None = None
    variant: str | None = None
    confidence: float | None = None
    source: str | None = None


class ParamCounts(Lenient):
    primary: int | None = None
    total: int | None = None
    display: str | None = None


class HashInfo(Lenient):
    state: str
    autov2: str | None = None
    sha256: str | None = None


class CivitaiRef(Lenient):
    state: str | None = None
    model_id: int | None = None
    url: str | None = None
    has_update: bool | None = None


class ModelCounts(Lenient):
    workflows: int = 0
    outputs: int = 0


class ModelItem(Lenient):
    uid: str
    id: int
    name: str | None = None
    filename: str | None = None
    ext: str | None = None
    category: str | None = None
    role: str | None = None
    base_model: BaseModelRef | None = None
    modality: str | None = None
    architecture: str | None = None
    precision: str | None = None
    quantization: str | None = None
    params: ParamCounts | None = None
    is_bundled: bool | None = None
    is_adapter: bool | None = None
    size: int | None = None
    modified_at: int | None = None
    folder: str | None = None
    root_id: int | None = None
    rel_path: str | None = None
    abs_path: str | None = None
    hash: HashInfo | None = None
    integrity: str | None = None
    civitai: CivitaiRef | None = None
    thumbnail_url: str | None = None
    favorite: bool | None = None
    user_rating: int | None = None
    color_label: str | None = None
    tags: list[str] = []
    counts: ModelCounts | None = None
    missing: bool | None = None


class ModelList(Lenient):
    items: list[ModelItem]
    page: PageInfo
    groups: list[GroupInfo] | None = None
    meta: MetaInfo | None = None


class ModelActions(Lenient):
    can_hash: bool
    can_rename: bool
    can_move: bool
    can_delete: bool
    can_refresh_metadata: bool
    refresh_blocked_reason: str | None = None


class ModelDetail(ModelItem):
    technical: dict[str, Any] | None = None
    build_spec: dict[str, Any] | None = None
    files: list[dict[str, Any]] = []
    update: dict[str, Any] | None = None
    description: dict[str, Any] | None = None
    usage_notes: str | None = None
    trigger_words: list[str] = []
    recommended_settings: dict[str, Any] | None = None
    download: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    user_notes: str | None = None
    actions: ModelActions | None = None


class FacetValue(Lenient):
    value: Any = None
    label: str | None = None
    count: int = 0


class RangeFacet(Lenient):
    min: int | None = None
    max: int | None = None
    total: int | None = None


class ModelFacets(Lenient):
    category: list[FacetValue] = []
    base_model: list[FacetValue] = []
    role: list[FacetValue] = []
    precision: list[FacetValue] = []
    modality: list[FacetValue] = []
    hash_state: list[FacetValue] = []
    integrity: list[FacetValue] = []
    root: list[FacetValue] = []
    tags: list[FacetValue] = []
    size: RangeFacet | None = None
    date: RangeFacet | None = None


class ModelGroups(Lenient):
    group: str
    nodes: list[TreeNode]


class ModelUsage(Lenient):
    workflows: list[dict[str, Any]] = []
    outputs: dict[str, Any] = {}
    page: PageInfo


class RefreshMetadataRequest(Strict):
    force: bool = False


class RefreshMetadataResponse(Lenient):
    state: str


class AssetPatch(Strict):
    """Shared user-metadata patch for models and outputs."""

    favorite: bool | None = None
    user_rating: int | None = Field(default=None, ge=0, le=5)
    user_notes: str | None = Field(default=None, max_length=20_000)
    tags: list[str] | None = None
    album_id: int | None = None
    color_label: str | None = Field(default=None, max_length=40)


class BulkPatchRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=200)
    patch: AssetPatch


class BulkPatchResult(Lenient):
    uid: str
    ok: bool
    error: str | None = None
    message: str | None = None


class BulkPatchResponse(Lenient):
    updated: int
    results: list[BulkPatchResult]
