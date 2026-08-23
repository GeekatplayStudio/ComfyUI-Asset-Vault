"""Indexing job control and history (API_CONTRACT 2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import Lenient, PageInfo, Strict


class IndexStartRequest(Strict):
    mode: Literal["full", "incremental", "targeted"] = "incremental"
    phases: list[str] | None = None
    root_ids: list[int] | None = None
    force: bool = False
    enrich_online: bool = True


class IndexStartResponse(Lenient):
    job_id: int
    mode: str
    started_at: int


class IndexCancelRequest(Strict):
    job_id: int | None = None


class IndexCancelResponse(Lenient):
    job_id: int | None
    status: str


class ActiveJob(Lenient):
    id: int
    mode: str
    status: str
    trigger: str | None = None
    phase: str | None = None
    phase_index: int | None = None
    phase_count: int | None = None
    items_done: int = 0
    items_total: int = 0
    items_skipped: int = 0
    error_count: int = 0
    rate_per_sec: float | None = None
    eta_ms: int | None = None
    current: str | None = None
    started_at: int | None = None
    elapsed_ms: int | None = None


class CompletedJob(Lenient):
    id: int
    finished_at: int | None = None
    duration_ms: int | None = None
    stats: dict[str, Any] = {}


class IndexStatus(Lenient):
    active: bool
    job: ActiveJob | None = None
    last_completed: CompletedJob | None = None


class JobHistoryItem(Lenient):
    id: int
    kind: str | None = None
    status: str | None = None
    phase: str | None = None
    trigger: str | None = None
    items_total: int | None = None
    items_done: int | None = None
    items_skipped: int | None = None
    error_count: int | None = None
    started_at: int | None = None
    finished_at: int | None = None
    duration_ms: int | None = None
    stats: dict[str, Any] | None = None


class JobHistory(Lenient):
    items: list[JobHistoryItem]
    page: PageInfo


class ScanErrorItem(Lenient):
    id: int
    job_id: int | None = None
    phase: str | None = None
    kind: str | None = None
    path: str | None = None
    code: str | None = None
    message: str | None = None
    created_at: int | None = None


class ScanErrorList(Lenient):
    items: list[ScanErrorItem]
    page: PageInfo
    summary: dict[str, int] = Field(default_factory=dict)
