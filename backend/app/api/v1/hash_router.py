"""``/api/v1/hash`` - the opt-in, resumable, background hashing queue
(API_CONTRACT 9, DECISIONS C1)."""

from __future__ import annotations

import os

from fastapi import APIRouter, Request

from ...core import config_service
from ...jobs.hash_service import get_hash_service
from ..deps import check_uids, require_stream_capacity, sse_response, sse_stream
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS
from ..schemas.jobs import (
    HashCancelRequest,
    HashCancelResponse,
    HashEnqueueRequest,
    HashEnqueueResponse,
    HashSettingsRequest,
    HashSettingsResponse,
    HashStatus,
)

router = APIRouter(prefix="/hash", tags=["Hashing"])

#: Contract scope -> the vocabulary ``HashService._scope_where`` understands.
SCOPE_MAP = {"all": "all", "unhashed": "unhashed_only"}


@router.post("/enqueue", status_code=202, response_model=HashEnqueueResponse,
             responses=MUTATION_ERRORS,
             summary="Queue full-file SHA-256 / AutoV2 work")
def enqueue_hash(body: HashEnqueueRequest) -> dict:
    uids: list[str] = []
    if body.scope == "ids":
        uids = check_uids(body.uids, param="uids", kinds=("model",))
        scope = "ids"
    elif body.scope == "category":
        if not body.category:
            raise ApiError("VALIDATION_ERROR", "scope='category' needs a category.",
                           field_errors=[{"field": "category",
                                          "message": "required when scope='category'"}])
        scope = f"category:{body.category}"
    elif body.scope == "folder":
        if not body.folder:
            raise ApiError("VALIDATION_ERROR", "scope='folder' needs a folder.",
                           field_errors=[{"field": "folder",
                                          "message": "required when scope='folder'"}])
        scope = f"folder:{body.folder}"
    else:
        scope = SCOPE_MAP[body.scope]

    result = get_hash_service().enqueue(scope, uids=uids or None,
                                        priority=body.priority,
                                        root_id=body.root_id)
    return {"batch_id": result.get("batch_id"), "queued": int(result.get("queued") or 0),
            "skipped": 0, "bytes_total": int(result.get("bytes_total") or 0),
            "eta_ms": result.get("eta_ms")}


@router.post("/cancel", status_code=202, response_model=HashCancelResponse,
             responses=MUTATION_ERRORS,
             summary="Cancel a batch, some uids, or everything")
def cancel_hash(body: HashCancelRequest) -> dict:
    uids = check_uids(body.uids, param="uids", kinds=("model",)) if body.uids else None
    service = get_hash_service()
    running_before = len(service.status().get("active") or [])
    result = service.cancel(body.batch_id, uids)
    return {"cancelled": int(result.get("cancelled") or 0),
            "running_stopped": running_before}


def _shape_status(raw: dict) -> dict:
    states = raw.get("states") or {}
    total = int(raw.get("bytes_total") or 0)
    done = int(raw.get("bytes_done") or 0)
    running = []
    for item in raw.get("active") or []:
        size = int(item.get("size") or 0)
        bytes_done = int(item.get("bytes_done") or 0)
        running.append({
            "uid": None,
            "filename": os.path.basename(str(item.get("path") or "")),
            "size": size, "bytes_done": bytes_done,
            "percent": round(bytes_done / size * 100, 1) if size else 0.0,
            "mbps": None,
        })
    return {
        "active": bool(raw.get("running")),
        "concurrency": int(raw.get("concurrency") or 0),
        "throttle_mbps": int(raw.get("throttle_mbps") or 0),
        "queue": {"queued": int(states.get("queued", 0)),
                  "running": int(states.get("running", 0)),
                  "done": int(states.get("done", 0)),
                  "failed": int(states.get("failed", 0)),
                  "cancelled": int(states.get("cancelled", 0))},
        "bytes": {"done": done, "total": total,
                  "percent": round(done / total * 100, 1) if total else 0.0},
        "throughput_mbps": float(raw.get("mbps") or 0.0),
        "eta_ms": raw.get("eta_ms"),
        "running": running,
        "recent_failures": [],
    }


@router.get("/status", response_model=HashStatus, responses=BASE_ERRORS,
            summary="Queue depth, throughput and ETA")
def hash_status() -> dict:
    return _shape_status(get_hash_service().status())


@router.get("/stream",
            responses={200: {"description": "text/event-stream: hash_progress, "
                                            "hash_item, done, heartbeat"},
                       **BASE_ERRORS},
            summary="Server-Sent Events for hashing progress")
async def hash_stream(request: Request):
    service = get_hash_service()
    require_stream_capacity(service.bus)
    return sse_response(sse_stream(service.subscribe(), request))


@router.post("/settings", response_model=HashSettingsResponse,
             responses=MUTATION_ERRORS,
             summary="Change concurrency / throttle without a restart")
def hash_settings(body: HashSettingsRequest) -> dict:
    patch = body.model_dump(exclude_unset=True)
    cfg = config_service.set_config(patch) if patch else config_service.get_config()
    return {"concurrency": cfg.hash_concurrency,
            "throttle_mbps": cfg.hash_throttle_mbps}
