"""Embeddings and hashing job control (API_CONTRACT 8-9)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Lenient, Strict

# --- embeddings ---------------------------------------------------------------

class DownloadProgress(Lenient):
    bytes_done: int = 0
    bytes_total: int = 0
    percent: float = 0.0


class EmbedIndexState(Lenient):
    embedded: int = 0
    pending: int = 0
    stale: int = 0
    last_built_at: int | None = None


class OnnxRuntimeInfo(Lenient):
    installed: bool
    version: str | None = None
    providers: list[str] = []


class EmbeddingsStatus(Lenient):
    state: str
    model_id: str
    dim: int
    install_dir: str
    download: DownloadProgress
    index: EmbedIndexState
    reason: str | None = None
    onnxruntime: OnnxRuntimeInfo


class EmbeddingsEnableRequest(Strict):
    source: Literal["auto", "local"] = "auto"


class EmbeddingsEnableResponse(Lenient):
    state: str
    bytes_total: int | None = None


class EmbeddingsDisableResponse(Lenient):
    state: str


class EmbeddingsRebuildRequest(Strict):
    kinds: list[str] | None = None
    force: bool = False


class EmbeddingsRebuildResponse(Lenient):
    job_id: str
    pending: int = 0


# --- hashing ------------------------------------------------------------------

class HashEnqueueRequest(Strict):
    scope: Literal["all", "category", "folder", "ids", "unhashed"] = "unhashed"
    category: str | None = Field(default=None, max_length=200)
    folder: str | None = Field(default=None, max_length=1024)
    root_id: int | None = None
    uids: list[str] | None = None
    priority: int = Field(default=5, ge=0, le=100)
    skip_hashed: bool = True


class HashEnqueueResponse(Lenient):
    batch_id: str
    queued: int
    skipped: int = 0
    bytes_total: int = 0
    eta_ms: int | None = None


class HashCancelRequest(Strict):
    batch_id: str | None = None
    uids: list[str] | None = None


class HashCancelResponse(Lenient):
    cancelled: int
    running_stopped: int = 0


class HashQueueCounts(Lenient):
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0


class HashByteCounts(Lenient):
    done: int = 0
    total: int = 0
    percent: float = 0.0


class HashRunningItem(Lenient):
    uid: str | None = None
    filename: str | None = None
    size: int | None = None
    bytes_done: int | None = None
    percent: float | None = None
    mbps: float | None = None


class HashFailure(Lenient):
    uid: str | None = None
    filename: str | None = None
    code: str | None = None
    message: str | None = None
    attempts: int | None = None
    will_retry: bool | None = None


class HashStatus(Lenient):
    active: bool
    concurrency: int
    throttle_mbps: int
    queue: HashQueueCounts
    bytes: HashByteCounts
    throughput_mbps: float = 0.0
    eta_ms: int | None = None
    running: list[HashRunningItem] = []
    recent_failures: list[HashFailure] = []


class HashSettingsRequest(Strict):
    concurrency: int | None = Field(default=None, ge=1, le=8)
    throttle_mbps: int | None = Field(default=None, ge=0, le=10_000)


class HashSettingsResponse(Lenient):
    concurrency: int
    throttle_mbps: int
