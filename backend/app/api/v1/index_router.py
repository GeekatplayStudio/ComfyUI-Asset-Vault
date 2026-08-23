"""``/api/v1/index`` - job control, history and the SSE progress stream
(API_CONTRACT 2, ARCHITECTURE 2.3)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request

from ...core import db as dbmod
from ...core import progress
from ...core.errors import ConflictError, NotConfigured
from ...indexing.service import PHASES, get_indexer
from ..deps import Page, page_params, require_stream_capacity, sse_response, sse_stream
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.indexing import (
    IndexCancelRequest,
    IndexCancelResponse,
    IndexStartRequest,
    IndexStartResponse,
    IndexStatus,
    JobHistory,
    ScanErrorList,
)

router = APIRouter(prefix="/index", tags=["Indexing"])

JOB_HISTORY_CAP = 1000
ERROR_SCAN_CAP = 5000


@router.post("/start", status_code=202, response_model=IndexStartResponse,
             responses={**error_responses("JOB_ALREADY_RUNNING", "NOT_CONFIGURED"),
                        **MUTATION_ERRORS},
             summary="Start a scan (never awaited inside the request)")
def start_index(body: IndexStartRequest) -> dict:
    bad = [p for p in (body.phases or []) if p not in PHASES]
    if bad:
        raise ApiError("VALIDATION_ERROR", f"Unknown phase(s): {', '.join(bad)}",
                       field_errors=[{"field": "phases",
                                      "message": f"must be a subset of {'|'.join(PHASES)}"}])
    try:
        job_id = get_indexer().start(
            mode=body.mode, phases=body.phases, root_ids=body.root_ids,
            force=body.force, enrich_online=body.enrich_online, trigger="user")
    except ConflictError as exc:
        raise ApiError("JOB_ALREADY_RUNNING", exc.message, details=exc.details) from exc
    except NotConfigured as exc:
        raise ApiError("NOT_CONFIGURED", exc.message, details=exc.details) from exc
    return {"job_id": job_id, "mode": body.mode, "started_at": dbmod.now_ms()}


@router.post("/cancel", status_code=202, response_model=IndexCancelResponse,
             responses={**error_responses("JOB_NOT_FOUND"), **MUTATION_ERRORS},
             summary="Cancel the active scan")
def cancel_index(body: IndexCancelRequest) -> dict:
    result = get_indexer().cancel(body.job_id)
    if not result.get("cancelled"):
        raise ApiError("JOB_NOT_FOUND", "No scan is currently running.",
                       details={"job_id": body.job_id,
                                "reason": result.get("reason")})
    return {"job_id": result.get("job_id"), "status": "cancelling"}


def _shape_status(state: dict, history: list[dict]) -> dict:
    active = bool(state.get("running"))
    job = None
    if active:
        phase = state.get("phase")
        done = int(state.get("items_done") or 0)
        total = int(state.get("items_total") or 0)
        elapsed = int(state.get("elapsed_ms") or 0)
        job = {
            "id": int(state.get("job_id") or 0),
            "mode": state.get("kind") or "incremental",
            "status": state.get("status") or "running",
            "trigger": state.get("trigger"),
            "phase": phase,
            "phase_index": (PHASES.index(phase) + 1) if phase in PHASES else None,
            "phase_count": len(PHASES),
            "items_done": done,
            "items_total": total,
            "items_skipped": int(state.get("items_skipped") or 0),
            "error_count": int(state.get("errors") or 0),
            "rate_per_sec": progress.rate_per_s(done, max(elapsed, 1) / 1000),
            "eta_ms": progress.eta_ms(done, total, max(elapsed, 1) / 1000),
            "current": state.get("current"),
            "started_at": state.get("started_at"),
            "elapsed_ms": elapsed,
        }
    last = next((row for row in history if row.get("status") == "completed"), None)
    completed = None
    if last is not None:
        try:
            stats = json.loads(last.get("stats_json") or "{}")
        except (TypeError, ValueError):
            stats = {}
        completed = {"id": int(last["id"]), "finished_at": last.get("finished_at"),
                     "duration_ms": last.get("duration_ms"), "stats": stats}
    return {"active": active, "job": job, "last_completed": completed}


@router.get("/status", response_model=IndexStatus, responses=BASE_ERRORS,
            summary="Poll fallback for the SSE stream")
def index_status() -> dict:
    indexer = get_indexer()
    return _shape_status(indexer.status(), indexer.jobs(limit=25, offset=0))


@router.get("/stream",
            responses={200: {"description": "text/event-stream: phase, progress, item, "
                                            "error, done, heartbeat, overflow"},
                       **BASE_ERRORS},
            summary="Server-Sent Events for scan progress")
async def index_stream(request: Request, job_id: int | None = Query(None)):
    indexer = get_indexer()
    require_stream_capacity(indexer.bus)
    return sse_response(sse_stream(indexer.subscribe(), request, job_id=job_id,
                                   close_on_done=job_id is not None))


@router.get("/jobs", response_model=JobHistory, responses=BASE_ERRORS,
            summary="Scan job history")
def index_jobs(page: Page = Depends(page_params)) -> dict:
    rows = get_indexer().jobs(limit=JOB_HISTORY_CAP, offset=0)
    total = len(rows)
    window = rows[page.offset:page.offset + page.limit]
    items = []
    for row in window:
        try:
            stats = json.loads(row.get("stats_json") or "{}")
        except (TypeError, ValueError):
            stats = {}
        items.append({
            "id": int(row["id"]), "kind": row.get("kind"), "status": row.get("status"),
            "phase": row.get("phase"), "trigger": row.get("trigger"),
            "items_total": row.get("items_total"), "items_done": row.get("items_done"),
            "items_skipped": row.get("items_skipped"),
            "error_count": row.get("error_count"),
            "started_at": row.get("started_at"), "finished_at": row.get("finished_at"),
            "duration_ms": row.get("duration_ms"), "stats": stats,
        })
    return {"items": items,
            "page": {"limit": page.limit, "offset": page.offset, "total": total,
                     "returned": len(items),
                     "has_more": page.offset + len(items) < total}}


@router.get("/errors", response_model=ScanErrorList, responses=BASE_ERRORS,
            summary="Per-item scan errors with a code histogram")
def index_errors(page: Page = Depends(page_params),
                 job_id: int | None = Query(None),
                 code: str | None = Query(None, max_length=60),
                 kind: str | None = Query(None, max_length=40)) -> dict:
    rows = get_indexer().job_errors(job_id, limit=ERROR_SCAN_CAP, offset=0)
    if code:
        rows = [r for r in rows if r.get("code") == code]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    summary: dict[str, int] = {}
    for row in rows:
        key = str(row.get("code") or "UNKNOWN")
        summary[key] = summary.get(key, 0) + 1
    window = rows[page.offset:page.offset + page.limit]
    items = [{"id": int(r["id"]), "job_id": r.get("job_id"), "phase": r.get("phase"),
              "kind": r.get("kind"), "path": r.get("abs_path"), "code": r.get("code"),
              "message": r.get("message"), "created_at": r.get("created_at")}
             for r in window]
    return {"items": items,
            "page": {"limit": page.limit, "offset": page.offset, "total": len(rows),
                     "returned": len(items),
                     "has_more": page.offset + len(items) < len(rows)},
            "summary": summary}
