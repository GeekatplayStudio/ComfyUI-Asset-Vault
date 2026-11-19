"""``/api/v1/advisor`` - System Advisor (API_CONTRACT 22).

What this machine can run, how safely, and where the model information comes
from.  Presentation only: every figure is produced in ``app/advisor``.

Two rules hold on every route here:

* hardware figures are **measured**; requirements, verdicts, speed classes and
  safety scores are **estimates** and carry ``"inferred": true``;
* no route accepts a URL.  A model is named by its catalog id, a pin by an
  ``owner/name`` repository id, and the link check only ever requests a URL
  already stored in the catalog.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...advisor import fit, safe_config, service, updater
from ..deps import Page, multi_csv_param, page_params
from ..schemas.advisor import (
    AdvisorSettings,
    AdvisorSettingsPatch,
    CatalogEntry,
    CatalogList,
    HardwareProfile,
    InstalledDetail,
    InstalledList,
    LiveSample,
    Overview,
    PinDeleted,
    PinRequest,
    PinResponse,
    Recommendations,
    SafeConfig,
    SourcesReport,
    UpdateRequest,
    UpdateStatus,
    VerifyLinkRequest,
    VerifyLinkResponse,
)
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses

router = APIRouter(prefix="/advisor", tags=["Advisor"])

SOURCE_IDS = ("huggingface", "civitai", "comfy_manager", "openmodeldb", "ollama")


@router.get("/overview", response_model=Overview, responses=BASE_ERRORS,
            summary="Machine class, memory budget and verdict totals in one call")
def overview() -> dict:
    return service.overview()


@router.get("/hardware", response_model=HardwareProfile, responses=BASE_ERRORS,
            summary="Full hardware profile: CPU, memory, GPUs, volumes, budget")
def hardware(
    refresh: bool = Query(False, description="Re-probe now instead of using "
                                             "the cached profile."),
) -> dict:
    return service.hardware_report(refresh=refresh)


@router.get("/live", response_model=LiveSample, responses=BASE_ERRORS,
            summary="One live resource reading for the dashboard")
def live(
    history: bool = Query(False, description="Include the recent history the "
                                             "sparklines draw."),
) -> dict:
    return service.live(history=history)


@router.get("/catalog", response_model=CatalogList, responses=BASE_ERRORS,
            summary="Every known model, assessed for this machine")
def catalog_list(
    page: Page = Depends(page_params),
    kind: list[str] | None = Query(None, description="Model types (CSV or repeated)."),
    verdict: list[str] | None = Query(None, description="optimal,good,max,offload,"
                                                         "cpu,base,no,unknown"),
    source: list[str] | None = Query(None, description="huggingface,civitai,"
                                                        "comfy_manager,openmodeldb,ollama"),
    q: str | None = Query(None, max_length=120),
    runs_only: bool = Query(False, description="Hide models that do not fit."),
    new_only: bool = Query(False, description="Only models first seen in the last week."),
    min_security: int | None = Query(None, ge=0, le=100),
    sort: str = Query(service.DEFAULT_SORT,
                      description="recommended | name | -downloads | -updated | "
                                  "need | -need | security | -security | size | -size"),
) -> dict:
    return service.list_entries(
        kinds=multi_csv_param("kind", kind, fit.KINDS),
        verdicts=multi_csv_param("verdict", verdict, fit.VERDICT_ORDER),
        source_ids=multi_csv_param("source", source, SOURCE_IDS),
        q=q, runs_only=runs_only, new_only=new_only, min_security=min_security,
        sort=sort, limit=page.limit, offset=page.offset)


@router.get("/catalog/entry", response_model=CatalogEntry,
            responses={**error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="Everything known about one model, variant by variant")
def catalog_entry(
    id: str = Query(..., min_length=3, max_length=240,
                    description="Catalog id, e.g. hf:owner/name."),
) -> dict:
    return service.entry_detail(id)


@router.post("/catalog/verify-link", response_model=VerifyLinkResponse,
             responses={**error_responses("NOT_FOUND", "FEATURE_UNAVAILABLE"),
                        **MUTATION_ERRORS},
             summary="Ask the host whether a stored download link is still live")
def verify_link(body: VerifyLinkRequest) -> dict:
    return service.verify_link(body.id, body.variant)


@router.get("/recommendations", response_model=Recommendations, responses=BASE_ERRORS,
            summary="Per model type: the optimum picks and the maximum that runs")
def recommendations(
    kind: list[str] | None = Query(None, description="Model types (CSV or repeated)."),
    per_kind: int = Query(service.RECOMMEND_PER_KIND, ge=1, le=20),
) -> dict:
    return service.recommendations(kinds=multi_csv_param("kind", kind, fit.KINDS),
                                   per_kind=per_kind)


@router.get("/installed", response_model=InstalledList, responses=BASE_ERRORS,
            summary="The models already in the vault, assessed for this machine")
def installed(
    page: Page = Depends(page_params),
    kind: list[str] | None = Query(None, description="Model types (CSV or repeated)."),
    verdict: list[str] | None = Query(None),
    q: str | None = Query(None, max_length=120),
    sort: str = Query("-need", description="need | -need | size | -size | "
                                           "security | -security | name"),
) -> dict:
    return service.installed(
        kinds=multi_csv_param("kind", kind, fit.KINDS),
        verdicts=multi_csv_param("verdict", verdict, fit.VERDICT_ORDER),
        q=q, sort=sort, limit=page.limit, offset=page.offset)


@router.get("/installed/{model_id}", response_model=InstalledDetail,
            responses={**error_responses("NOT_FOUND"), **BASE_ERRORS},
            summary="One indexed model: requirement, verdict and safety factors")
def installed_detail(model_id: int) -> dict:
    return service.installed_detail(model_id)


@router.get("/safe-config", response_model=SafeConfig, responses=BASE_ERRORS,
            summary="Launch flags and environment values that keep headroom")
def get_safe_config() -> dict:
    profile, budget, _prefs = service.context()
    return safe_config.build(profile, budget)


@router.get("/settings", response_model=AdvisorSettings, responses=BASE_ERRORS,
            summary="Headroom, selected model types and the auto-refresh switch")
def get_settings() -> dict:
    return service.settings()


@router.patch("/settings", response_model=AdvisorSettings, responses=MUTATION_ERRORS,
              summary="Change any subset of the advisor settings")
def patch_settings(body: AdvisorSettingsPatch) -> dict:
    return service.update_settings(body.model_dump(exclude_unset=True))


@router.get("/sources", response_model=SourcesReport, responses=BASE_ERRORS,
            summary="Where model information comes from, and when each source last answered")
def sources() -> dict:
    return service.sources_report()


@router.post("/sources/pins", response_model=PinResponse, responses=MUTATION_ERRORS,
             summary="Remember a Hugging Face repository for every future refresh")
def add_pin(body: PinRequest) -> dict:
    return updater.add_pin(body.repo, body.kind)


@router.delete("/sources/pins", response_model=PinDeleted, responses=MUTATION_ERRORS,
               summary="Forget a pinned repository")
def remove_pin(
    repo: str = Query(..., min_length=3, max_length=240),
) -> dict:
    return {"removed": updater.remove_pin(repo), "repo": repo}


@router.get("/update/status", response_model=UpdateStatus, responses=BASE_ERRORS,
            summary="Catalog age and the progress of a running refresh")
def update_status() -> dict:
    return updater.status()


@router.post("/update", response_model=UpdateStatus, status_code=202,
             responses={**error_responses("FEATURE_UNAVAILABLE", "CONFLICT"),
                        **MUTATION_ERRORS},
             summary="Refresh the model catalog from its sources, in the background")
def update_start(body: UpdateRequest | None = None) -> dict:
    body = body or UpdateRequest()
    if body.auto:
        # The interface asks on the owner's behalf when the Advisor opens; the
        # same guards as the startup timer decide whether anything runs.
        outcome = updater.maybe_auto_update()
        return {**updater.status(), "auto_outcome": outcome}
    return updater.start(trigger="manual", kinds=list(body.kinds or []) or None,
                         only=list(body.sources or []) or None)


@router.post("/update/cancel", response_model=UpdateStatus, responses=MUTATION_ERRORS,
             summary="Stop a running refresh; the previous catalog is kept")
def update_cancel() -> dict:
    return updater.cancel()
