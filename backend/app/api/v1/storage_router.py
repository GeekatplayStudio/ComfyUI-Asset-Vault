"""``/api/v1/storage`` - Storage & Maintenance (API_CONTRACT 18, C10).

Shaped for progressive disclosure (C11): ``/summary`` is one small payload that
answers "where did my terabyte go", and every number in it has a paged endpoint
behind it.  No route here returns an unbounded list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...core.errors import AppError
from ...services import storage_service
from ...services.queries import storage_query
from ..deps import Page, check_uids, multi_csv_param, page_params
from ..middleware import ApiError, normalize_code
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.storage import (
    CandidateList,
    CleanupRequest,
    CleanupResponse,
    DuplicateList,
    EstimateRequest,
    EstimateResponse,
    RootsReport,
    StorageSummary,
)

router = APIRouter(prefix="/storage", tags=["Storage"])

CLEANUP_KINDS = ("model", "output")


def _reraise(exc: AppError) -> None:
    raise ApiError(normalize_code(exc.code), exc.message,
                   details=exc.details) from exc


@router.get("/summary", response_model=StorageSummary, responses=BASE_ERRORS,
            summary="Space breakdown, per-volume free space, and reclaim headlines")
def summary(
    stale_days: int = Query(180, ge=1, le=3650,
                            description="Age threshold for the stale buckets."),
    refresh: bool = Query(False,
                          description="Re-walk the install instead of using the "
                                      "cached footprint."),
) -> dict:
    return storage_service.summary(stale_days=stale_days, refresh=refresh)


@router.get("/candidates", response_model=CandidateList, responses=BASE_ERRORS,
            summary="Largest / oldest / most reclaimable items, one paged table")
def candidates(
    page: Page = Depends(page_params),
    sort: str = Query(storage_query.DEFAULT_SORT,
                      description="reclaim | size | age | name. "
                                  "'reclaim' is the combined score; 'size' and "
                                  "'age' are first-class keys (C10.5)."),
    # Every multi-value filter accepts BOTH `?kind=model,output` and
    # `?kind=model&kind=output`.  A bare `str` would bind only the last
    # repetition and drop the rest without saying so.
    kind: list[str] | None = Query(None, description="model,output (CSV or repeated)"),
    reason: list[str] | None = Query(None,
                                     description="unused,duplicate,superseded,stale,"
                                                 "large,integrity,orphan_output,"
                                                 "non_media,protected "
                                                 "(CSV or repeated)"),
    category: list[str] | None = Query(None, description="model categories"),
    role: list[str] | None = Query(None, description="model roles"),
    media_kind: list[str] | None = Query(None, description="output media kinds"),
    root_id: list[str] | None = Query(None, description="root ids"),
    folder: str | None = Query(None, max_length=1024),
    q: str | None = Query(None, max_length=120),
    min_size: int | None = Query(None, ge=0),
    max_size: int | None = Query(None, ge=0),
    older_than_days: int | None = Query(None, ge=0, le=36500),
    stale_days: int = Query(180, ge=1, le=3650),
    include_protected: bool = Query(
        True, description="Favourites and 4+ ratings are always shown by default, "
                          "flagged rather than hidden."),
) -> dict:
    roots = multi_csv_param("root_id", root_id) or []
    for value in roots:
        if not str(value).lstrip("-").isdigit():
            raise ApiError("VALIDATION_ERROR", f"root_id: '{value}' is not an id",
                           field_errors=[{"field": "root_id",
                                          "message": "must be an integer id"}])
    filters = {
        "kind": multi_csv_param("kind", kind, storage_query.KINDS),
        "reason": multi_csv_param("reason", reason, storage_query.REASONS),
        "category": multi_csv_param("category", category),
        "role": multi_csv_param("role", role),
        "media_kind": multi_csv_param("media_kind", media_kind),
        "root_id": [int(r) for r in roots] or None,
        "folder": folder, "q": q, "min_size": min_size, "max_size": max_size,
        "older_than_days": older_than_days, "stale_days": stale_days,
        "include_protected": include_protected,
    }
    try:
        result = storage_query.candidates(filters, sort=sort, limit=page.limit,
                                          offset=page.offset)
    except AppError as exc:
        _reraise(exc)
        raise
    return result.as_dict()


@router.get("/duplicates", response_model=DuplicateList, responses=BASE_ERRORS,
            summary="Duplicate sets, each naming the method that found it")
def duplicates(
    page: Page = Depends(page_params),
    method: str | None = Query(
        None, description="sha256 | name+size | name across roots. "
                          "Only sha256 groups are exact; the rest are candidates."),
) -> dict:
    return storage_query.duplicate_groups(limit=page.limit, offset=page.offset,
                                          method=method).as_dict()


@router.get("/roots", response_model=RootsReport, responses=BASE_ERRORS,
            summary="Per-root volume, indexed contents, and retired-root retention")
def roots() -> dict:
    return storage_service.roots_report()


@router.post("/estimate", response_model=EstimateResponse,
             responses={**error_responses("VALIDATION_ERROR"), **MUTATION_ERRORS},
             summary="Exact byte total for a selection, before anything is deleted")
def estimate(body: EstimateRequest) -> dict:
    uids = check_uids(body.uids, kinds=CLEANUP_KINDS)
    return storage_service.estimate(uids)


@router.post("/cleanup", response_model=CleanupResponse,
             responses={**error_responses("NOT_FOUND", "PATH_NOT_ALLOWED",
                                          "FILE_LOCKED", "PAYLOAD_TOO_LARGE"),
                        **MUTATION_ERRORS},
             summary="Delete an explicit selection - trash-backed unless confirmed")
def cleanup(body: CleanupRequest) -> dict:
    uids = check_uids(body.uids, kinds=CLEANUP_KINDS)
    try:
        return storage_service.cleanup(uids, mode=body.mode, confirm=body.confirm)
    except AppError as exc:
        _reraise(exc)
        raise
