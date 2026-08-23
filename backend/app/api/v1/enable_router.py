"""``/api/v1/workflows/{id}/enable`` and ``/api/v1/enable`` - API_CONTRACT 20.

The contract is deliberately two-step and deliberately awkward, for the same
reason the C8 updater's is: a confirmation that does not name what will happen
is not a confirmation.

1. ``GET .../enable/plan`` returns the dependency report - every missing model
   with its derived destination, every missing node package with its repository,
   the total download size, and the free space on each target volume.  It also
   returns a short-lived ``plan_token``.
2. ``POST .../enable/fetch`` accepts that token, the ids the user selected, and
   ``confirm: true``.  Anything else - a stale token, an id that was not in the
   plan, a missing confirmation - is a 422 and issues no request.

No route here accepts a URL or a filesystem path.  Destinations are derived
server-side from the node input that referenced the file.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...core.errors import AppError
from ...enable import download as enable_download
from ...enable.service import build_report, get_enable_service
from ...enable.service import recheck as enable_recheck
from ..deps import require_stream_capacity, sse_response, sse_stream
from ..middleware import ApiError, normalize_code
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.enable import (
    EnableCancelRequest,
    EnableCancelResponse,
    EnableFetchRequest,
    EnableFetchResponse,
    EnablePlan,
    EnableQuarantine,
    EnableRecheck,
    EnableStatus,
)

workflow_router = APIRouter(prefix="/workflows", tags=["Workflows"])
router = APIRouter(prefix="/enable", tags=["Workflows"])


def _reraise(exc: AppError):
    raise ApiError(normalize_code(exc.code), exc.message,
                   details=exc.details) from exc


@workflow_router.get(
    "/{workflow_id}/enable/plan", response_model=EnablePlan,
    responses={**error_responses("NOT_FOUND", "NOT_CONFIGURED"), **BASE_ERRORS},
    summary="Dependency report: what is missing, where it would go, how big it is")
def enable_plan(
    workflow_id: int,
    on_conflict: str = Query("fail", pattern="^(fail|skip|keep_both)$",
                             description="Recorded in the plan; 'overwrite' does "
                                         "not exist on this path."),
) -> dict:
    try:
        return build_report(workflow_id, on_conflict=on_conflict)
    except AppError as exc:
        _reraise(exc)
        raise


@workflow_router.post(
    "/{workflow_id}/enable/fetch", status_code=202, response_model=EnableFetchResponse,
    responses={**error_responses("NOT_FOUND", "CONFLICT", "NOT_CONFIGURED",
                                 "INSUFFICIENT_SPACE", "VALIDATION_ERROR"),
               **MUTATION_ERRORS},
    summary="Fetch the confirmed items (202; progress on /enable/stream)")
def enable_fetch(workflow_id: int, body: EnableFetchRequest) -> dict:
    try:
        return get_enable_service().fetch(
            workflow_id, plan_token=body.plan_token, item_ids=list(body.item_ids),
            confirm=bool(body.confirm), on_conflict=body.on_conflict, trigger="api")
    except AppError as exc:
        _reraise(exc)
        raise


@workflow_router.post(
    "/{workflow_id}/enable/recheck", response_model=EnableRecheck,
    responses={**error_responses("NOT_FOUND"), **MUTATION_ERRORS},
    summary="Is this workflow runnable now? (C9.8)")
def enable_recheck_route(workflow_id: int) -> dict:
    try:
        return enable_recheck(workflow_id)
    except AppError as exc:
        _reraise(exc)
        raise


@router.get("/status", response_model=EnableStatus, responses=BASE_ERRORS,
            summary="State of the current or last Enable fetch")
def enable_status(
    batch_id: str | None = Query(None, max_length=64),
    workflow_id: int | None = Query(None, ge=1),
) -> dict:
    return get_enable_service().status(batch_id=batch_id, workflow_id=workflow_id)


@router.post("/cancel", response_model=EnableCancelResponse,
             responses={**error_responses("VALIDATION_ERROR"), **MUTATION_ERRORS},
             summary="Stop the fetch; a partial file is kept so it can resume")
def enable_cancel(body: EnableCancelRequest) -> dict:
    return get_enable_service().cancel(batch_id=body.batch_id)


@router.get("/stream", include_in_schema=True, responses=BASE_ERRORS,
            summary="Live fetch progress (SSE): phase, progress, item, done")
async def enable_stream(request: Request):
    service = get_enable_service()
    require_stream_capacity(service.bus)
    return sse_response(sse_stream(service.subscribe(), request))


@router.get("/quarantine", response_model=EnableQuarantine, responses=BASE_ERRORS,
            summary="Downloads that failed verification and were never placed")
def enable_quarantine() -> dict:
    items = enable_download.quarantine_list()
    return {"items": items, "total": len(items),
            "bytes": sum(int(i.get("bytes") or 0) for i in items)}
