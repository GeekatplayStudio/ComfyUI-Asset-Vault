"""``/api/v1/ai`` - optional Ollama enrichment (API_CONTRACT 13).

Ollama being off or unreachable is a normal state: ``/status`` is always a 200,
and ``/describe`` answers ``503 FEATURE_UNAVAILABLE`` rather than a 5xx.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request

from ...services.ollama_service import ollama_service
from ...services.queries import models_query, nodes_query, tags_query, workflows_query
from ..deps import check_uid, sse_frame, sse_response
from ..middleware import ApiError
from ..schemas.common import BASE_ERRORS, MUTATION_ERRORS, error_responses
from ..schemas.library import AiDescribeRequest, AiDescribeResponse, AiStatus

router = APIRouter(prefix="/ai", tags=["AI enrichment"])

TASK_KINDS = {
    "workflow_summary": ("workflow",),
    "model_usage_notes": ("model",),
    "update_benefits": ("model", "node_package"),
    "node_package_summary": ("node_package",),
}


@router.get("/status", response_model=AiStatus, responses=BASE_ERRORS,
            summary="Ollama reachability (never an error)")
async def ai_status() -> dict:
    status = await ollama_service.status()
    return {"enabled": bool(status.get("enabled")),
            "available": bool(status.get("available")),
            "url": str(status.get("url") or ""),
            "models": list(status.get("models") or []),
            "reason": status.get("reason")}


def _facts(uid: str) -> tuple[str, str, str]:
    """(kind, display name, fact sheet) built from indexed data only."""
    kind, _sep, num = uid.partition(":")
    row_id = int(num)
    if kind == "workflow":
        item = workflows_query.get_workflow(row_id)
        if item is None:
            raise ApiError("NOT_FOUND", f"{uid} does not exist.", details={"uid": uid})
        classes = ", ".join(str(n.get("class_type")) for n in
                            (item.get("node_breakdown") or [])[:24])
        return kind, str(item.get("name") or uid), (
            f"base model: {item.get('base_model')}\n"
            f"modality: {item.get('modality')}\n"
            f"nodes: {(item.get('counts') or {}).get('nodes')}\n"
            f"missing nodes: {(item.get('counts') or {}).get('missing_nodes')}\n"
            f"missing models: {(item.get('counts') or {}).get('missing_models')}\n"
            f"positive prompt: {item.get('positive_prompt')}\n"
            f"node classes: {classes}\n")
    if kind == "model":
        item = models_query.get_model(row_id)
        if item is None:
            raise ApiError("NOT_FOUND", f"{uid} does not exist.", details={"uid": uid})
        return kind, str(item.get("name") or uid), (
            f"category: {item.get('category')}\nrole: {item.get('role')}\n"
            f"base model: {(item.get('base_model') or {}).get('family')}\n"
            f"architecture: {item.get('architecture')}\n"
            f"precision: {item.get('precision')}\n"
            f"parameters: {(item.get('params') or {}).get('display')}\n"
            f"used by {(item.get('counts') or {}).get('workflows')} workflow(s)\n")
    item = nodes_query.get_node_package(row_id)
    if item is None:
        raise ApiError("NOT_FOUND", f"{uid} does not exist.", details={"uid": uid})
    categories = ", ".join(str(c.get("category")) for c in
                           (item.get("class_categories") or [])[:12])
    return kind, str(item.get("display_name") or uid), (
        f"author: {item.get('author')}\nclasses: {item.get('class_count')}\n"
        f"description: {item.get('description')}\ncategories: {categories}\n")


async def _generate(uid: str, task: str, model: str | None) -> dict:
    kind, name, facts = _facts(uid)
    if task == "update_benefits":
        return await ollama_service.summarize_update(name, facts, "", model=model)
    return await ollama_service.describe_asset(kind.replace("_", " "), name, facts,
                                               model=model)


def _persist(uid: str, task: str, text: str) -> None:
    """Only workflows expose a writable description column today."""
    if task == "workflow_summary" and uid.startswith("workflow:"):
        try:
            tags_query.patch_asset(uid, {"description": text,
                                         "description_source": "ollama"})
        except Exception:  # noqa: BLE001 - persistence is best effort
            return


async def _token_stream(request: Request, uid: str, task: str,
                        model: str | None) -> AsyncIterator[bytes]:
    started = time.perf_counter()
    result = await _generate(uid, task, model)
    if not result.get("ok"):
        yield sse_frame("error", {"code": "FEATURE_UNAVAILABLE",
                                  "message": result.get("reason")})
        yield sse_frame("done", {"uid": uid, "task": task})
        return
    text = str(result.get("text") or "")
    _persist(uid, task, text)
    if not await request.is_disconnected():
        yield sse_frame("token", {"text": text})
    yield sse_frame("done", {"uid": uid, "task": task, "model": result.get("model"),
                             "elapsed_ms": int((time.perf_counter() - started) * 1000)})


@router.post("/describe", response_model=None,
             responses={200: {"model": AiDescribeResponse},
                        **error_responses("NOT_FOUND", "FEATURE_UNAVAILABLE"),
                        **MUTATION_ERRORS},
             summary="Ask the local Ollama model to describe an indexed asset")
async def ai_describe(request: Request, body: AiDescribeRequest):
    uid = check_uid(body.uid, kinds=TASK_KINDS[body.task])
    if not ollama_service.enabled:
        raise ApiError("FEATURE_UNAVAILABLE",
                       "Ollama is disabled in Settings.",
                       details={"reason": "ollama_disabled"})
    if body.stream:
        return sse_response(_token_stream(request, uid, body.task, body.model))

    started = time.perf_counter()
    result = await _generate(uid, body.task, body.model)
    if not result.get("ok"):
        raise ApiError("FEATURE_UNAVAILABLE",
                       str(result.get("reason") or "Ollama is unreachable."),
                       details={"reason": "ollama_unreachable"})
    text = str(result.get("text") or "")
    _persist(uid, body.task, text)
    return {"uid": uid, "task": body.task, "text": text,
            "model": result.get("model"), "cached": False,
            "elapsed_ms": int((time.perf_counter() - started) * 1000)}
