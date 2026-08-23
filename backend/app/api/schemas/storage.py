"""Storage & maintenance (API_CONTRACT 18, REQUIREMENTS_R2 C10)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, MetaInfo, PageInfo, Strict

SortKey = Literal["reclaim", "size", "age", "name", "score"]
ItemKind = Literal["model", "output"]
ReasonCode = Literal["unused", "duplicate", "superseded", "stale", "large",
                     "integrity", "orphan_output", "non_media", "protected"]


class VolumeUsage(Lenient):
    total_bytes: int | None = None
    free_bytes: int | None = None
    used_bytes: int | None = None
    used_pct: float | None = None
    available: bool = True
    error: str | None = None


class StorageRoot(Lenient):
    id: int
    kind: str
    path: str
    label: str
    category: str | None = None
    is_default: bool = False
    source: str = "config"
    configured: bool = True
    retired: bool = False
    exists: bool = True
    indexed: bool = False


class Volume(VolumeUsage):
    key: str
    mount: str
    roots: list[StorageRoot] = []


class FootprintBucket(Lenient):
    key: str
    label: str
    bytes: int
    files: int = 0
    dirs: list[str] = []
    measured: bool = True
    truncated: bool = False
    indexed_bytes: int | None = None
    indexed_count: int | None = None


class VaultFootprint(Lenient):
    key: str = "vault"
    label: str
    bytes: int
    files: int = 0
    detail: dict[str, Any] = {}
    outside_comfyui: bool = True


class Footprint(Lenient):
    total_bytes: int
    buckets: list[FootprintBucket]
    vault: VaultFootprint
    measured_at: int
    elapsed_ms: int | None = None
    truncated: bool = False


class TrashFootprint(Lenient):
    count: int = 0
    bytes: int = 0
    bytes_on_disk: int = 0
    next_purge_at: int | None = None
    retention_days: int = 30
    directories: list[str] = []
    endpoint: str = "/api/v1/fileops/trash"


class ReclaimGroup(Lenient):
    key: str
    label: str
    count: int
    bytes: int
    confidence: Literal["measured", "inferred"]
    reason: ReasonCode | None = None
    exact_count: int | None = None
    unprotected_count: int | None = None
    unprotected_bytes: int | None = None


class Reclaim(Lenient):
    stale_days: int
    groups: list[ReclaimGroup]


class IndexCoverage(Lenient):
    models: dict[str, int]
    outputs: dict[str, int]
    node_packages: dict[str, int]
    workflows: dict[str, int]
    hashed_files: int
    duplicate_detection: Literal["exact", "partial", "heuristic"]


class StorageSummary(Lenient):
    generated_at: int
    configured: bool
    comfyui_path: str | None = None
    footprint: Footprint
    volumes: list[Volume]
    primary_volume: Volume | None = None
    trash: TrashFootprint
    reclaim: Reclaim
    index: IndexCoverage
    detail_endpoints: dict[str, str] = {}


class CandidateReason(Lenient):
    code: ReasonCode
    label: str
    confidence: Literal["measured", "inferred"]
    weight: int
    method: str | None = None


class Candidate(Lenient):
    uid: str
    kind: ItemKind
    id: int
    name: str | None = None
    filename: str | None = None
    ext: str | None = None
    category: str | None = None
    role: str | None = None
    media_kind: str | None = None
    folder: str = ""
    rel_path: str | None = None
    abs_path: str | None = None
    root_id: int | None = None
    size: int
    modified_at: int
    created_at: int
    age_days: int
    counts: dict[str, int] = {}
    hash_state: str | None = None
    reclaim_score: int
    confidence: Literal["measured", "inferred"]
    reasons: list[CandidateReason] = []
    protected: bool = False
    duplicate_group: str | None = None
    thumbnail_url: str | None = None


class CandidateList(Lenient):
    items: list[Candidate]
    page: PageInfo
    meta: MetaInfo | None = None


class DuplicateMember(Lenient):
    uid: str
    name: str | None = None
    category: str | None = None
    size: int
    abs_path: str | None = None
    protected: bool = False


class DuplicateGroup(Lenient):
    key: str
    method: Literal["sha256", "name+size", "name across roots"]
    confidence: Literal["measured", "inferred"]
    count: int
    bytes: int
    reclaimable_bytes: int
    suggested_keep_uid: str
    items: list[DuplicateMember]


class DuplicateList(Lenient):
    items: list[DuplicateGroup]
    page: PageInfo
    meta: MetaInfo | None = None


class RootsReport(Lenient):
    items: list[dict[str, Any]]
    retention_policy: Literal["retain", "prune"] = "retain"
    retention_note: str
    retired_roots: int = 0
    retired_bytes: int = 0


class EstimateRequest(Strict):
    uids: list[str] = Field(min_length=1, max_length=1000)


class EstimateResponse(Lenient):
    requested: int
    resolved: int
    by_kind: dict[str, int]
    bytes: int
    unknown_uids: list[str] = []
    protected_uids: list[str] = []
    protected_count: int = 0


class CleanupRequest(Strict):
    """Nothing is inferred: the selection is always explicit (C10.4)."""

    uids: list[str] = Field(min_length=1, max_length=200)
    mode: Literal["trash", "permanent"] = "trash"
    confirm: bool = False


class CleanupResponse(Lenient):
    ok: bool
    mode: str
    requested: int
    deleted: int
    failed: int
    freed_bytes: int
    estimated_bytes: int
    trash_ids: list[int] = []
    protected_count: int = 0
    recoverable: bool = True
    results: list[dict[str, Any]] = []
