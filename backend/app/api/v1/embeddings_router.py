"""``/api/v1/embeddings`` - the local ONNX embedder (API_CONTRACT 8, DECISIONS C2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request

from ...core.errors import FeatureUnavailable
from ...jobs.embed_service import get_embed_service
from ..deps import sse_response, sse_stream
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.jobs import (
    EmbeddingsDisableResponse,
    EmbeddingsEnableRequest,
    EmbeddingsEnableResponse,
    EmbeddingsRebuildRequest,
    EmbeddingsRebuildResponse,
    EmbeddingsStatus,
)

router = APIRouter(prefix="/embeddings", tags=["Embeddings"])

#: Free-text service reasons are mapped onto the frozen contract vocabulary.
STATE_REASON = {
    "not_installed": "embedding_model_not_installed",
    "downloading": "embedding_model_downloading",
    "unavailable": "onnxruntime_missing",
}


def _onnxruntime_info() -> dict:
    try:
        import onnxruntime as ort
    except ImportError:
        return {"installed": False, "version": None, "providers": []}
    try:
        providers = list(ort.get_available_providers())
    except Exception:  # noqa: BLE001 - provider probing must never fail a status call
        providers = []
    return {"installed": True, "version": getattr(ort, "__version__", None),
            "providers": providers}


def contract_reason(status: dict) -> str | None:
    state = str(status.get("state") or "")
    if state == "ready":
        return None if int(status.get("embedded") or 0) else "index_building"
    return STATE_REASON.get(state, "embedding_model_not_installed")


def _shape_status(service) -> dict:
    raw = service.status()
    download = raw.get("download") or {}
    total = int(download.get("bytes_total") or 0)
    done = int(download.get("bytes_done") or 0)
    return {
        "state": raw.get("state"),
        "model_id": raw.get("model_id"),
        "dim": int(raw.get("dim") or 0),
        "install_dir": raw.get("path"),
        "download": {"bytes_done": done, "bytes_total": total,
                     "percent": round(done / total * 100, 1) if total else 0.0},
        "index": {"embedded": int(raw.get("embedded") or 0),
                  "pending": max(0, int(raw.get("documents") or 0)
                                 - int(raw.get("embedded") or 0)),
                  "stale": int(raw.get("queued") or 0),
                  "last_built_at": None},
        "reason": contract_reason(raw),
        "onnxruntime": _onnxruntime_info(),
    }


@router.get("/status", response_model=EmbeddingsStatus, responses=BASE_ERRORS,
            summary="Model install state and index coverage")
def embeddings_status() -> dict:
    return _shape_status(get_embed_service())


@router.post("/enable", status_code=202, response_model=EmbeddingsEnableResponse,
             responses={**error_responses("FEATURE_UNAVAILABLE", "UPSTREAM_UNAVAILABLE"),
                        **MUTATION_ERRORS},
             summary="Download (or adopt) the MiniLM INT8 model")
async def enable_embeddings(body: EmbeddingsEnableRequest) -> dict:
    service = get_embed_service()
    if not _onnxruntime_info()["installed"]:
        raise ApiError("FEATURE_UNAVAILABLE",
                       "onnxruntime is not installed; smart search cannot run.",
                       details={"reason": "onnxruntime_missing"})
    try:
        raw = await service.enable(body.source)
    except FeatureUnavailable as exc:
        raise ApiError("FEATURE_UNAVAILABLE", exc.message, details=exc.details) from exc
    except OSError as exc:
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       f"The model could not be downloaded: {exc}",
                       details={"reason": "download_failed"}, retryable=True) from exc
    return {"state": raw.get("state"),
            "bytes_total": int((raw.get("download") or {}).get("bytes_total") or 0)}


@router.post("/disable", response_model=EmbeddingsDisableResponse,
             responses=MUTATION_ERRORS,
             summary="Turn smart search off; vectors are kept unless purge=true")
def disable_embeddings(purge: bool = Query(False)) -> dict:
    get_embed_service().disable(purge=purge)
    return {"state": "disabled"}


@router.post("/rebuild", status_code=202, response_model=EmbeddingsRebuildResponse,
             responses={**error_responses("FEATURE_UNAVAILABLE"), **MUTATION_ERRORS},
             summary="Recompute the embedding index")
def rebuild_embeddings(body: EmbeddingsRebuildRequest) -> dict:
    service = get_embed_service()
    state = service.rebuild(body.kinds, body.force)
    if state == "unavailable":
        raise ApiError("FEATURE_UNAVAILABLE",
                       "The embedding model is not installed.",
                       details={"reason": contract_reason(service.status())})
    status = _shape_status(service)
    return {"job_id": f"embed-{uuid.uuid4().hex[:6]}",
            "pending": int(status["index"]["pending"])}


@router.get("/stream",
            responses={200: {"description": "text/event-stream: embed_progress, done"},
                       **BASE_ERRORS},
            summary="Server-Sent Events for embedding index progress")
async def embeddings_stream(request: Request):
    return sse_response(sse_stream(get_embed_service().subscribe(), request))
